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

개선안을 시험할 때 (나중):
      PaperCoinGrid 를 상속해 규칙을 바꾼 클래스를 만들고 --grid 모듈:클래스 로 넘긴다.
      전략 로직은 계속 모의매매 코드 한 곳에만 둔다.

실행:
      python -m paper_trading.replay                                  # 전 기간, 현행 설정
      python -m paper_trading.replay --start 2026-09-27T07:00:59Z \\
             --compare paper_trading/logs/trades_book.csv             # 실제 기록과 대조
      python -m paper_trading.replay --time-stop 72                   # 설정만 바꿔 재생
      python -m paper_trading.replay --dir 다른/폴더 --save 결과.csv
"""

import sys, os, csv, glob, gzip, argparse, logging, importlib, statistics
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUOTES_DIR = os.path.join(BASE, "real_trading", "logs", "quotes")
FEE_RATE = 0.0018     # paper_trader._exit_slot 의 수수료 (업비트 0.05%×2 + 비트겟 0.04%×2)


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
    pt.logger.setLevel(logging.WARNING)             # 10초마다 찍던 INFO 출력 끔
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

def replay(pt, trades: list, grid_cls, coins_cfg: dict, snaps: list) -> dict:
    grids = {c: grid_cls(c, cfg) for c, cfg in coins_cfg.items()}
    counter = [0]
    for t, up, bg, fx, usdt in snaps:
        _Clock.now = t
        usdt_krw = usdt if usdt else fx           # 모의매매와 같은 폴백
        for c, g in grids.items():
            cfg = coins_cfg[c]
            # 호가가 빠진 시점: 실제 모의매매도 오류로 그 코인을 건너뛴다 → 조용히 건너뜀
            if cfg["upbit_market"] not in up or cfg["bitget_symbol"] not in bg:
                continue
            g.tick(up, bg, fx, usdt_krw, counter)

    # 끝 시점에 열려 있는 슬롯의 평가손익 = 지금 청산하면 받을 금액
    # (청산 김프 − 진입 김프) × 슬롯자본 − 왕복 수수료 + 누적 펀딩. 실현 손익과 같은 기준.
    open_slots = {}
    if snaps:
        t, up, bg, fx, usdt = snaps[-1]
        for c, g in grids.items():
            act = [s for s in g.slots if s.active]
            if not act:
                continue
            try:
                q = g.quote(up, bg, fx)
            except ValueError:
                continue
            pnl = [(q["exit_pct"] - s.entry_premium) / 100 * g.cpg - FEE_RATE * g.cpg + s.funding_krw
                   for s in act]
            open_slots[c] = (len(act), sum(pnl), [round(s.entry_premium, 2) for s in act])
    return {"trades": list(trades), "open": open_slots}


def run_variant(pt, trades: list, coins_cfg: dict, snaps: list,
                time_stop: float = None, grid_cls=None) -> dict:
    """한 설정으로 재생. 이전 재생의 거래가 섞이지 않게 비우고, 바꾼 시간손절은 되돌린다."""
    trades.clear()
    old = pt.TIME_STOP_HOURS
    if time_stop is not None:
        pt.TIME_STOP_HOURS = time_stop
    try:
        return replay(pt, trades, grid_cls or pt.PaperCoinGrid, coins_cfg, snaps)
    finally:
        pt.TIME_STOP_HOURS = old


# ── 요약·대조 ────────────────────────────────────────────────────────

def _reason(r: dict, time_stop_h: float) -> str:
    if float(r["exit_premium"]) >= float(r["target_premium"]) - 1e-9:
        return "익절"
    if float(r["hold_hours"]) >= time_stop_h - 0.02:
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
    ap.add_argument("--compare", default=None, help="실제 모의매매 기록 trades_book.csv 와 대조")
    ap.add_argument("--save", default=None, help="재생 거래를 CSV 로 저장")
    args = ap.parse_args()

    pt, trades = load_paper_trader()

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
    if args.time_stop is not None:
        pt.TIME_STOP_HOURS = args.time_stop
    grid_cls = pt.PaperCoinGrid
    if args.grid:
        mod, cls = args.grid.split(":")
        grid_cls = getattr(importlib.import_module(mod), cls)

    actual = []
    if args.compare:
        with open(args.compare, encoding="utf-8") as f:
            actual = list(csv.DictReader(f))
    start = datetime.fromisoformat(args.start.replace("Z", "+00:00")).timestamp() if args.start else None
    if start is None and actual:
        start = min(_iso(r["entry_dt"]) for r in actual) - 1
        print(f"  ⚠ --start 미지정 — 실제 기록의 첫 진입 시각으로 시작합니다. 모의매매 시작 시각을 아시면 --start 로 주세요")
    end = datetime.fromisoformat(args.end.replace("Z", "+00:00")).timestamp() if args.end else None

    snaps, files = load_snapshots(args.dir, coins_cfg, start, end)
    if not snaps:
        print(f"재생할 호가가 없습니다: {args.dir}")
        return 1
    t0, t1 = snaps[0][0], snaps[-1][0]
    res = replay(pt, trades, grid_cls, coins_cfg, snaps)
    ts_h = pt.TIME_STOP_HOURS
    c0 = next(iter(coins_cfg.values()))

    print("=" * 100)
    print(f"  리플레이 — {datetime.fromtimestamp(t0, timezone.utc):%m-%d %H:%M} ~ "
          f"{datetime.fromtimestamp(t1, timezone.utc):%m-%d %H:%M} UTC ({(t1-t0)/3600:.1f}시간, 시점 {len(snaps):,}개)")
    print(f"  설정: {grid_cls.__name__}, 코인 {len(coins_cfg)}개, spacing {c0['spacing']}%, "
          f"슬롯 {c0['n_slots']}개, 슬롯당 {c0['upbit_capital']/c0['n_slots']:,.0f}원, 시간손절 {ts_h}h")
    print("=" * 100)
    sim = summarize(res["trades"], ts_h)
    print_summary("재생", sim, res["open"])

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
            w = csv.DictWriter(f, fieldnames=pt.TRADE_LOG_HEADER)
            w.writeheader()
            w.writerows(res["trades"])
        print(f"  저장: {args.save}")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
