"""
리플레이 시뮬레이터 — 기록된 호가로 모의매매를 다시 돌린다
────────────────────────────────────────────────────────────────────
무엇을 하나:
      real_trading/quote_logger.py 가 기록한 호가 로그를 시간순으로 흘려보내면서,
      실제 모의매매 코드(paper_trading/paper_trader.py 의 PaperCoinGrid)를 그대로 돌린다.
      전략 로직을 따로 다시 짜지 않으므로, 같은 호가를 넣으면 모의매매와 같은 거래가 나온다.

      바꿔 끼우는 것은 세 가지뿐이다.
        시계       time.time() / datetime.now() 대신 호가 기록 시각
        거래 기록  trades_book.csv 대신 메모리에 모음
        로그       매 폴링 INFO 출력을 끄고, trading.log 파일 핸들러를 떼어낸다

왜 필요한가:
      모의매매는 한 번에 한 설정, 한 주에 한 번밖에 못 본다. 리플레이는 같은 기간 데이터로
      여러 설정을 몇 분 만에 비교한다. 그 전에 현행 설정을 재생해 실제 모의매매 기록과
      맞는지(--compare) 확인해서 시뮬레이터부터 검증한다.

실제 모의매매와 달라질 수 있는 이유:
      모의매매와 로거는 각자 10초마다 따로 조회한다. 같은 10초 안에서도 조회 시점과
      받아 온 호가가 다르므로 개별 거래는 어긋날 수 있다. 그래서 거래 수·코인별 손익·
      청산 사유 같은 합계와, 진입 시각·김프가 가까운 거래끼리의 일치율로 비교한다.

개선안을 시험할 때:
      paper_trading/variants.py 의 설정 문자열로 준다 (익절 환율 고정, 진입 필터, 투입 상한 등).
      개선안은 PaperCoinGrid 를 상속해 바뀐 규칙만 덮어쓴다.

손익 구성 (끝에 함께 출력):
      합계 = 환율 노출분 + 김프 움직임(중간가) + 펀딩 − 수수료 − 호가 비용
      환율 노출분 = Σ 열린 슬롯 금액 × 은행 환율 변화율. 헤지 포지션 손익 ≈ 금액 × (환율 변화율 + 김프 변화)
      이라서, 열어 둔 금액만큼 원/달러에 노출된다. 한국 가격이 환율을 늦게 따라가면 환율분과 김프분이
      반대 부호로 함께 커지므로, 짧은 기간에는 둘을 따로 해석하지 말고 '합계 − 환율분'으로 본다.
      환율은 5분 중앙값을 쓴다. 은행 환율 피드가 가끔 튀는데(9/30 08:26 KST 1,351.5→1,358.4→1,350.5, 70초),
      튀는 사이 봇이 슬롯을 열고 닫으면 노출분이 수만 원씩 부풀기 때문이다. 튐 때문에 생긴 거래 비용은
      수수료·호가비용에 그대로 남는다.

실행:
      python -m paper_trading.replay                                  # 전 기간, 현행 설정
      python -m paper_trading.replay --start 2026-09-27T07:00:59Z \\
             --compare paper_trading/logs/trades_book.csv             # 실제 기록과 대조
      python -m paper_trading.replay --time-stop 72                   # 설정만 바꿔 재생
      python -m paper_trading.replay --variant "tp=entry,entry_q=0.2" # 개선안
      python -m paper_trading.replay --dir 다른/폴더 --save 결과.csv
"""

import sys, os, csv, glob, gzip, bisect, argparse, logging, importlib, statistics
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUOTES_DIR = os.path.join(BASE, "real_trading", "logs", "quotes")
from paper_trading.paper_settings import PAPER_FEE_ROUND as FEE_RATE   # paper_trader._exit_slot 과 같은 왕복 수수료율


# ── 모의매매 코드를 재생용으로 불러오기 ───────────────────────────────

class _Clock:
    """재생 중의 '현재 시각'. paper_trader 의 time.time() 을 대신한다."""
    now = 0.0

    @staticmethod
    def time() -> float:
        return _Clock.now


class _ReplayDatetime(datetime):
    """datetime.now() 만 재생 시각으로 바꾼다. 나머지는 그대로."""
    @classmethod
    def now(cls, tz=None):
        return datetime.fromtimestamp(_Clock.now, tz=tz or timezone.utc)


def load_paper_trader():
    """paper_trader 를 불러와 시계·기록·로그만 재생용으로 바꾼다. 반환: (모듈, 거래 목록)"""
    pt = importlib.import_module("paper_trading.paper_trader")
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler):      # 재생 기록이 trading.log 에 섞이지 않게
            root.removeHandler(h)
            h.close()
    # 10초마다 찍던 INFO 와 가상손절 WARNING 을 끈다. 둘 다 재생 결과 요약에 나오고, 찍히면
    # 재생 시각이 아니라 지금 시각이 붙어 헷갈린다. 코드 오류(ERROR)는 계속 보이게 둔다.
    pt.logger.setLevel(logging.ERROR)
    trades = []
    pt.time = _Clock
    pt.datetime = _ReplayDatetime
    pt._append_trade = trades.append
    return pt, trades


# ── 호가 로그 ────────────────────────────────────────────────────────

def _open(p):
    return gzip.open(p, "rt", encoding="utf-8") if p.endswith(".gz") else open(p, encoding="utf-8")


def _ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()


def load_snapshots(qdir: str, coins_cfg: dict, start: float, end: float):
    """호가 로그를 시점별 묶음으로. [(t, up, bg, fx, usdt), ...]
    up/bg 는 모의매매가 거래소에서 받는 것과 같은 형식의 튜플이다."""
    keys = {c: (cfg["upbit_market"], cfg["bitget_symbol"]) for c, cfg in coins_cfg.items()}
    snaps, files = {}, sorted(glob.glob(os.path.join(qdir, "quotes_*.csv*")))
    for f in files:
        with _open(f) as fh:
            for r in csv.DictReader(fh):
                c = r["coin"]
                if c not in keys:
                    continue
                try:
                    t = _ts(r["ts"])
                    if (start and t < start) or (end and t > end):
                        continue
                    s = snaps.get(t)
                    if s is None:
                        usdt = float(r["usdt_bid"]) if r.get("usdt_bid") else None
                        s = snaps[t] = [t, {}, {}, float(r["fx"]), usdt]
                    mk, sym = keys[c]
                    s[1][mk] = (float(r["up_bid"]), float(r["up_ask"]),
                                float(r["up_bid_sz"]), float(r["up_ask_sz"]), int(r["up_ts"]))
                    s[2][sym] = (float(r["bg_bid"]), float(r["bg_ask"]),
                                 float(r["bg_bid_sz"]), float(r["bg_ask_sz"]), int(r["bg_ts"]),
                                 float(r["funding"] or 0))
                except (ValueError, KeyError, TypeError):
                    continue
    return [tuple(snaps[t]) for t in sorted(snaps)], files


# ── 재생 ─────────────────────────────────────────────────────────────

def make_grids(grid_cls, coins_cfg: dict) -> dict:
    """코인별 그리드. 코인끼리 공유하는 상태가 있는 개선안(투입 상한)은 make_grids 로 한꺼번에 만든다."""
    if hasattr(grid_cls, "make_grids"):
        return grid_cls.make_grids(coins_cfg)
    return {c: grid_cls(c, cfg) for c, cfg in coins_cfg.items()}


def mark_slot(g, s, q: dict, usdt_krw: float) -> tuple:
    """열린 슬롯을 지금 청산하면 받을 금액 — paper_trader._exit_slot 과 같은 식.
    반환: (순손익, 수수료, 호가비용)

    김프 차이 × 슬롯자본으로 근사하면 안 된다. 청산 김프를 지금 환율로 다시 계산하므로
    진입 뒤 환율이 움직인 만큼 틀린다 (실제 손익은 가격 비율과 USDT 환산으로 정해진다)."""
    gross = (s.coin_qty * (q["up_bid"] - s.upbit_entry_px)
             + s.short_qty * (s.bitget_entry_px - q["bg_ask"]) * usdt_krw)
    mid = (s.coin_qty * ((q["up_bid"] + q["up_ask"]) / 2 - s.upbit_entry_mid)
           + s.short_qty * (s.bitget_entry_mid - (q["bg_bid"] + q["bg_ask"]) / 2) * usdt_krw)
    fee = FEE_RATE * g.cpg
    return gross - fee + s.funding_krw, fee, mid - gross


def smooth_fx(snaps: list, n: int = 31) -> list:
    """시점별 환율의 직전 n개(10초 간격이면 5분) 중앙값. 환율 노출분 계산용 — 피드 튐 제거."""
    win, srt, out = [], [], []
    for x in snaps:
        win.append(x[3])
        bisect.insort(srt, x[3])
        if len(win) > n:
            del srt[bisect.bisect_left(srt, win.pop(0))]
        out.append(srt[len(srt) // 2])
    return out


def replay(pt, trades: list, grid_cls, coins_cfg: dict, snaps: list, history: list = ()) -> dict:
    grids = make_grids(grid_cls, coins_cfg)
    # 진입 필터처럼 과거 분포가 필요한 개선안은 시작 전 기록으로 미리 채운다 (거래는 안 함)
    for t, up, bg, fx, usdt in history:
        _Clock.now = t
        for g in grids.values():
            if hasattr(g, "warm"):
                g.warm(up, bg, fx)

    counter = [0]
    fxs = smooth_fx(snaps)
    fx_pnl, open_krw, krw_sum, krw_max = 0.0, 0.0, 0.0, 0.0
    for k, (t, up, bg, fx, usdt) in enumerate(snaps):
        _Clock.now = t
        if k:
            fx_pnl += open_krw * (fxs[k] / fxs[k - 1] - 1)   # 직전 시점부터 열려 있던 금액 × 환율 변화
        usdt_krw = usdt if usdt else fx           # 모의매매와 같은 폴백
        for c, g in grids.items():
            cfg = coins_cfg[c]
            # 호가가 빠진 시점: 실제 모의매매도 오류로 그 코인을 건너뛴다 → 조용히 건너뜀
            if cfg["upbit_market"] not in up or cfg["bitget_symbol"] not in bg:
                continue
            g.tick(up, bg, fx, usdt_krw, counter)
        open_krw = sum(g.cpg * g.active_count() for g in grids.values())
        krw_sum += open_krw
        krw_max = max(krw_max, open_krw)

    # 끝 시점에 열려 있는 슬롯 = 지금 청산하면 받을 금액 (실현 손익과 같은 식)
    open_slots, open_fee, open_spread, open_fund = {}, 0.0, 0.0, 0.0
    if snaps:
        t, up, bg, fx, usdt = snaps[-1]
        usdt_krw = usdt if usdt else fx
        for c, g in grids.items():
            act = [s for s in g.slots if s.active]
            if not act:
                continue
            try:
                q = g.quote(up, bg, fx)
            except (ValueError, KeyError):
                continue
            marks = [mark_slot(g, s, q, usdt_krw) for s in act]
            open_slots[c] = (len(act), sum(m[0] for m in marks), [round(s.entry_premium, 2) for s in act])
            open_fee += sum(m[1] for m in marks)
            open_spread += sum(m[2] for m in marks)
            open_fund += sum(s.funding_krw for s in act)
    n = max(len(snaps), 1)
    return {"trades": list(trades), "open": open_slots,
            "open_fee": open_fee, "open_spread": open_spread, "open_fund": open_fund,
            "fx_pnl": fx_pnl, "krw_avg": krw_sum / n, "krw_max": krw_max,
            "full": sum(g.cpg * g.n_slots for g in grids.values()),
            "fx0": snaps[0][3] if snaps else 0.0, "fx1": snaps[-1][3] if snaps else 0.0,
            "orders": sum(getattr(g, "orders", 0) for g in grids.values()),
            "thin": sum(getattr(g, "thin", 0) for g in grids.values())}


def run_variant(pt, trades: list, coins_cfg: dict, snaps: list,
                time_stop: float = None, grid_cls=None, history: list = ()) -> dict:
    """한 설정으로 재생. 이전 재생의 거래가 섞이지 않게 비우고, 바꾼 시간손절은 되돌린다."""
    trades.clear()
    old = pt.TIME_STOP_HOURS
    if time_stop is not None:
        pt.TIME_STOP_HOURS = time_stop
    try:
        return replay(pt, trades, grid_cls or pt.PaperCoinGrid, coins_cfg, snaps, history)
    finally:
        pt.TIME_STOP_HOURS = old


# ── 요약·대조 ────────────────────────────────────────────────────────

def _reason(r: dict, time_stop_h: float) -> str:
    if r.get("reason"):                          # 2026-10-06~ 기록은 청산 사유를 남긴다
        return r["reason"]
    if float(r["exit_premium"]) >= float(r["target_premium"]) - 1e-9:
        return "익절"
    if time_stop_h and float(r["hold_hours"]) >= time_stop_h - 0.02:
        return "시간손절"
    return "가상손절"


def summarize(rows: list, time_stop_h: float) -> dict:
    s = {"n": len(rows), "net": 0.0, "fee": 0.0, "fund": 0.0, "spread": 0.0,
         "reason": defaultdict(lambda: [0, 0.0]), "coin": defaultdict(lambda: [0, 0.0, 0.0, 0])}
    for r in rows:
        net = float(r["net_pnl"])
        rs = _reason(r, time_stop_h)
        s["net"] += net
        s["fee"] += float(r["fee_krw"])
        s["fund"] += float(r["funding_krw"])
        s["spread"] += float(r.get("spread_krw") or 0)
        s["reason"][rs][0] += 1
        s["reason"][rs][1] += net
        c = s["coin"][r["coin"]]
        c[0] += 1
        c[1] += net
        c[2] += float(r.get("spread_krw") or 0)
        c[3] += rs != "익절"
    return s


def _iso(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def match_trades(actual: list, sim: list, dt_s: float = 30, dp: float = 0.05) -> dict:
    """진입 시각이 dt_s 초 이내이고 진입 김프가 dp%p 이내인 거래끼리 짝짓는다."""
    by = defaultdict(list)
    for r in sim:
        by[r["coin"]].append([_iso(r["entry_dt"]), float(r["entry_premium"]), r, False])
    matched, diffs, same_reason = 0, [], 0
    for a in actual:
        ta, pa = _iso(a["entry_dt"]), float(a["entry_premium"])
        best = None
        for cand in by[a["coin"]]:
            if cand[3] or abs(cand[0] - ta) > dt_s or abs(cand[1] - pa) > dp:
                continue
            if best is None or abs(cand[0] - ta) < abs(best[0] - ta):
                best = cand
        if best:
            best[3] = True
            matched += 1
            diffs.append(float(best[2]["net_pnl"]) - float(a["net_pnl"]))
            same_reason += (float(a["exit_premium"]) >= float(a["target_premium"]) - 1e-9) == \
                           (float(best[2]["exit_premium"]) >= float(best[2]["target_premium"]) - 1e-9)
    return {"matched": matched, "actual": len(actual), "sim": len(sim),
            "net_diff_med": statistics.median(diffs) if diffs else 0.0,
            "same_reason": same_reason}


def print_summary(label: str, s: dict, open_slots: dict = None):
    print(f"  [{label}] 청산 {s['n']:,}건  순손익 {s['net']:+,.0f}원  "
          f"(수수료 {s['fee']:,.0f} / 펀딩 {s['fund']:+,.0f} / 호가비용 {s['spread']:,.0f})")
    for k in ("익절", "시간손절", "가상손절"):
        if k in s["reason"]:
            n, v = s["reason"][k]
            print(f"      {k:5} {n:>5,}건  {v:>+10,.0f}원  건당 {v/n:>+7,.0f}원")
    if open_slots is not None:
        n = sum(v[0] for v in open_slots.values())
        u = sum(v[1] for v in open_slots.values())
        print(f"      끝 시점 미청산 {n}개, 평가손익 {u:+,.0f}원")


def attribution(s: dict, res: dict) -> dict:
    """합계 = 환율 노출분 + 김프 움직임(중간가) + 펀딩 − 수수료 − 호가비용. 김프분은 나머지로 구한다."""
    unreal = sum(v[1] for v in res["open"].values())
    total = s["net"] + unreal
    fee, spread = s["fee"] + res["open_fee"], s["spread"] + res["open_spread"]
    fund, fx = s["fund"] + res["open_fund"], res["fx_pnl"]
    return {"total": total, "unreal": unreal, "fx": fx, "fee": fee, "spread": spread, "fund": fund,
            "kimp": total - fx - fund + fee + spread, "ex_fx": total - fx}


def print_attribution(s: dict, res: dict):
    a = attribution(s, res)
    print(f"      구성: 환율 노출 {a['fx']:+,.0f} / 김프(중간가) {a['kimp']:+,.0f} / 펀딩 {a['fund']:+,.0f}"
          f" / 수수료 {-a['fee']:+,.0f} / 호가비용 {-a['spread']:+,.0f}  → 환율 제외 {a['ex_fx']:+,.0f}원")
    if res["full"] and res["fx0"]:
        print(f"      평균 투입 {res['krw_avg']:,.0f}원 (최대 {res['full']:,.0f}원의 {res['krw_avg'] / res['full'] * 100:.0f}%)"
              f", 은행 환율 {res['fx0']:,.1f} → {res['fx1']:,.1f} ({(res['fx1'] / res['fx0'] - 1) * 100:+.2f}%)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=QUOTES_DIR, help="호가 로그 폴더")
    ap.add_argument("--start", default=None, help="재생 시작 (예: 2026-09-27T07:00:59Z)")
    ap.add_argument("--end", default=None, help="재생 끝")
    ap.add_argument("--coins", nargs="*", default=None, help="일부 코인만")
    ap.add_argument("--spacing", type=float, default=None, help="익절 폭(%%) 덮어쓰기")
    ap.add_argument("--slots", type=int, default=None, help="코인당 슬롯 수 덮어쓰기")
    ap.add_argument("--total", type=int, default=None, help="총 시드(원) 덮어쓰기")
    ap.add_argument("--time-stop", type=float, default=None, help="시간손절(시간) 덮어쓰기")
    ap.add_argument("--grid", default=None, help="개선안 클래스 '모듈:클래스' (PaperCoinGrid 상속)")
    ap.add_argument("--variant", default=None,
                    help="개선안 설정 문자열 (paper_trading/variants.py), 예: tp=entry,entry_q=0.2")
    ap.add_argument("--compare", default=None, help="실제 모의매매 기록 trades_book.csv 와 대조")
    ap.add_argument("--save", default=None, help="재생 거래를 CSV 로 저장")
    args = ap.parse_args()

    pt, trades = load_paper_trader()

    history_h = 0.0
    if args.variant is not None:
        # 개선안: 코인·자본·간격까지 설정 문자열 하나로 받는다 (옛 옵션과 섞으면 어느 쪽이 이기는지 헷갈림)
        if args.coins or args.spacing is not None or args.slots is not None or args.total is not None or args.grid:
            print("--variant 와 --coins/--spacing/--slots/--total/--grid 는 함께 못 씁니다. "
                  "설정 문자열 안에 coins=A+B, spacing=0.4 처럼 넣으세요.")
            return 1
        from paper_trading import variants as va
        from paper_trading.paper_settings import PAPER_TOTAL_KRW
        try:
            coins_cfg, tstop, grid_cls, _ = va.build(args.variant, pt.COINS, PAPER_TOTAL_KRW)
        except ValueError as e:
            print(f"개선안 설정 오류: {e}")
            return 1
        if tstop is not None:
            pt.TIME_STOP_HOURS = tstop
    else:
        # 설정: 기본은 모의매매 설정 그대로, 주어진 값만 덮어쓴다
        coins_cfg = {c: dict(cfg) for c, cfg in pt.COINS.items()
                     if not args.coins or c in [x.upper() for x in args.coins]}
        for cfg in coins_cfg.values():
            if args.spacing is not None:
                cfg["spacing"] = args.spacing
            if args.slots is not None:
                cfg["n_slots"] = args.slots
            if args.total is not None:
                lev = cfg["leverage"]
                cfg["upbit_capital"] = args.total * lev // (lev + 1) // len(coins_cfg)
        grid_cls = pt.PaperCoinGrid
        if args.grid:
            mod, cls = args.grid.split(":")
            grid_cls = getattr(importlib.import_module(mod), cls)
    if args.time_stop is not None:
        pt.TIME_STOP_HOURS = args.time_stop
    # 진입 필터가 켜진 설정이면 시작 전 창 길이만큼 기록을 미리 흘린다 (서버도 시작할 때 호가 기록으로 채운다)
    history_h = max((float(c.get("window", 24)) for c in coins_cfg.values() if c.get("entry_q") is not None),
                    default=0.0)

    actual = []
    if args.compare:
        with open(args.compare, encoding="utf-8") as f:
            actual = list(csv.DictReader(f))
    start = datetime.fromisoformat(args.start.replace("Z", "+00:00")).timestamp() if args.start else None
    if start is None and actual:
        start = min(_iso(r["entry_dt"]) for r in actual) - 1
        print(f"  ⚠ --start 미지정 — 실제 기록의 첫 진입 시각으로 시작합니다. 모의매매 시작 시각을 아시면 --start 로 주세요")
    end = datetime.fromisoformat(args.end.replace("Z", "+00:00")).timestamp() if args.end else None

    pre = history_h * 3600 if start else 0
    snaps, files = load_snapshots(args.dir, coins_cfg, start - pre if start else None, end)
    history = [x for x in snaps if start and x[0] < start]
    snaps = snaps[len(history):]
    if not snaps:
        print(f"재생할 호가가 없습니다: {args.dir}")
        return 1
    t0, t1 = snaps[0][0], snaps[-1][0]
    res = replay(pt, trades, grid_cls, coins_cfg, snaps, history)
    ts_h = pt.TIME_STOP_HOURS
    c0 = next(iter(coins_cfg.values()))

    print("=" * 100)
    print(f"  리플레이 — {datetime.fromtimestamp(t0, timezone.utc):%m-%d %H:%M} ~ "
          f"{datetime.fromtimestamp(t1, timezone.utc):%m-%d %H:%M} UTC ({(t1-t0)/3600:.1f}시간, 시점 {len(snaps):,}개)")
    print(f"  설정: {grid_cls.__name__}, 코인 {len(coins_cfg)}개, spacing {c0['spacing']}%, "
          f"슬롯 {c0['n_slots']}개, 슬롯당 {c0['upbit_capital']/c0['n_slots']:,.0f}원, "
          f"시간손절 {f'{ts_h:g}h' if ts_h and ts_h != float('inf') else '없음'}")
    if args.variant:
        print(f"  개선안: {args.variant}  (코인 {'+'.join(coins_cfg)}, 레버리지 {c0['leverage']}배)")
    print("=" * 100)
    if history_h and not history:
        print(f"  ⚠ --start 가 없어 진입 필터를 미리 채우지 못했습니다 — 처음 {history_h * 0.25:.0f}시간은 진입하지 않습니다")
    sim = summarize(res["trades"], ts_h)
    print_summary("재생", sim, res["open"])
    print_attribution(sim, res)
    if res["orders"] and res["thin"] / res["orders"] > 0.01:
        print(f"      ⚠ 주문 {res['orders']:,}건 중 {res['thin'] / res['orders'] * 100:.0f}% 가 최우선 호가 잔량보다 큼"
              " — 다음 호가까지 먹는 비용이 빠져 있음")

    if actual:
        act = [r for r in actual if t0 <= _iso(r["exit_dt"]) <= t1 and r["coin"] in coins_cfg]
        real = summarize(act, ts_h)
        print()
        print_summary("실제", real)
        m = match_trades(act, res["trades"])
        print()
        print(f"  대조: 실제 {m['actual']}건 중 {m['matched']}건이 재생 거래와 짝지어짐"
              f" ({m['matched']/max(m['actual'],1)*100:.0f}%, 진입 30초·김프 0.05%p 이내)")
        if m["matched"]:
            print(f"        짝지어진 거래의 청산 사유 일치 {m['same_reason']/m['matched']*100:.0f}%, "
                  f"순손익 차이 중앙값 {m['net_diff_med']:+,.0f}원")
        print()
        print(f"  {'코인':6}{'실제 건수':>9}{'재생 건수':>9}{'실제 순손익':>12}{'재생 순손익':>12}"
              f"{'실제 손절':>9}{'재생 손절':>9}")
        print("  " + "-" * 66)
        for c in coins_cfg:
            a, b = real["coin"].get(c, [0, 0, 0, 0]), sim["coin"].get(c, [0, 0, 0, 0])
            if a[0] or b[0]:
                print(f"  {c:6}{a[0]:>9}{b[0]:>9}{a[1]:>+12,.0f}{b[1]:>+12,.0f}{a[3]:>9}{b[3]:>9}")
        print("  " + "-" * 66)
    else:
        print()
        print(f"  {'코인':6}{'건수':>6}{'순손익':>11}{'호가비용':>10}{'손절':>6}{'미청산':>7}{'평가손익':>10}  미청산 진입김프")
        print("  " + "-" * 90)
        for c in coins_cfg:
            b = sim["coin"].get(c, [0, 0, 0, 0])
            o = res["open"].get(c, (0, 0.0, []))
            if b[0] or o[0]:
                print(f"  {c:6}{b[0]:>6}{b[1]:>+11,.0f}{b[2]:>10,.0f}{b[3]:>6}{o[0]:>7}{o[1]:>+10,.0f}  {o[2]}")
        print("  " + "-" * 90)

    if args.save:
        with open(args.save, "w", newline="", encoding="utf-8") as f:
            extra = [k for k in ("reason", "exit_fx") if k not in pt.TRADE_LOG_HEADER
                     and res["trades"] and k in res["trades"][0]]
            w = csv.DictWriter(f, fieldnames=pt.TRADE_LOG_HEADER + extra)
            w.writeheader()
            w.writerows(res["trades"])
        print(f"  저장: {args.save}")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
