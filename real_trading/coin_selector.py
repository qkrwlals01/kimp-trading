"""
코인 선정기 — 호가 로그 기반 자동 선정
────────────────────────────────────────────────────────────────────
입력: real_trading/logs/quotes/ 의 호가 로그 (quote_logger.py 가 기록)

스냅샷 한 번으로 코인을 고르면 틀린다. 2026-08 에 LINK 는 단발 측정으로 통과했다가
반복 측정에서 통과율 17% 로 떨어졌고, DOGE 는 가격이 100원을 넘으면서 1틱 비용이
0.1% → 0.75% 로 뛰었다. 그래서 며칠치 실제 호가로 판정하고 주기적으로 다시 돌린다.

1) 탈락 조건 — 하나라도 걸리면 제외
   데이터 부족   예상 기록 수의 50% 미만
   호가 비용     즉시왕복비용(진입김프 − 청산김프) 중앙값이 순마진 초과
   괴리          최근 1시간 김프가 다른 코인 중앙값과 --div-max(%p) 넘게 차이
                 (입출금 중단 등으로 김프가 벌어진 코인은 괴리가 풀릴 때 물린다)
   청산 위험     24시간 최대 상승폭이 --rise-max(%) 초과 (5배 숏은 약 16% 급등에 청산)
   기대수익      아래 시뮬레이션 결과가 0 이하

2) 순위 — 슬롯 1개를 실제 호가로 굴려 본 하루 기대수익
   진입: 진입김프가 최근 --window 시간 분포의 하위 --q 이하일 때
         (시장가: 업비트 매도호가에 사고 비트겟 매수호가에 숏 = entry_kimp)
   청산: 청산김프가 진입가 + spacing 이상이거나 --hold 시간 경과 시
         (시장가: 업비트 매수호가에 팔고 비트겟 매도호가에 숏청산 = exit_kimp)
   비용: 수수료(시장가 0.18%, 지정가 0.14%) + 보유 중 펀딩비(양수면 숏이 수취)
   --maker 는 양쪽 모두 중간가에 체결된다고 본다. 스프레드를 내지도 벌지도 않는
   중립 가정이며, 실제 지정가는 미체결·역선택이 있어 이보다 나쁠 수 있다.
   지정가여도 호가 비용 탈락 조건은 그대로 적용한다 (한쪽 체결 시 시장가 보정 비용).

   ⚠ 마지막 체결가로 만든 데이터에 돌리면 체결가가 호가 사이를 튀는 탓에 스프레드
     넓은 코인이 가짜로 1위가 된다. 반드시 quote_logger 의 실제 호가 로그로 판정할 것.

   슬롯 1개 기준이라 여러 슬롯을 돌릴 때보다 체결 수는 적게 나온다.
   절대 수익 추정이 아니라 코인 간 비교용이다.

실행:
      python -m real_trading.coin_selector                    # 최근 7일, 시장가
      python -m real_trading.coin_selector --maker --top 5
      python -m real_trading.coin_selector --days 14 --q 0.1 --hold 72
      python -m real_trading.coin_selector --dir 다른/폴더 --no-save

결과는 real_trading/logs/selection/selection_YYYYMMDD_HHMM.json 에 저장된다.
"""

import sys, os, csv, glob, gzip, json, bisect, argparse, statistics
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
QUOTES_DIR = os.path.join(BASE, "logs", "quotes")
SELECT_DIR = os.path.join(BASE, "logs", "selection")

FEE_MARKET = 0.18      # 업비트 0.05%×2 + 비트겟 taker 0.04%×2
FEE_MAKER = 0.14       # 업비트 0.05%×2 + 비트겟 maker 0.02%×2


# ── 적재 ──────────────────────────────────────────────────────────

def _open(p):
    return gzip.open(p, "rt", encoding="utf-8") if p.endswith(".gz") else open(p, encoding="utf-8")


def load(qdir: str, days: int):
    """최근 days 일치 로그. {coin: [(t, entry, exit, mid, funding, bg_mid, up_bid, up_ask), ...]}"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d")
    files = sorted(f for f in glob.glob(os.path.join(qdir, "quotes_*.csv*"))
                   if os.path.basename(f)[7:15] >= cutoff)
    data, tcache = defaultdict(list), {}
    for f in files:
        with _open(f) as fh:
            for r in csv.DictReader(fh):
                try:
                    s = r["ts"]
                    t = tcache.get(s)
                    if t is None:
                        t = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ") \
                                    .replace(tzinfo=timezone.utc).timestamp()
                        tcache[s] = t
                    data[r["coin"]].append((
                        t, float(r["entry_kimp"]), float(r["exit_kimp"]), float(r["mid_kimp"]),
                        float(r["funding"]),
                        (float(r["bg_bid"]) + float(r["bg_ask"])) / 2,
                        float(r["up_bid"]), float(r["up_ask"]),
                    ))
                except (ValueError, KeyError, TypeError):
                    continue
    for c in data:
        data[c].sort()
    return data, files


# ── 지표 ──────────────────────────────────────────────────────────

def divergence(data: dict) -> dict:
    """코인별 (최근 1시간 괴리 중앙값, 최근 24시간 최대 |괴리|). 괴리 = 내 김프 − 전 코인 중앙값"""
    by_t = defaultdict(list)
    for c, rows in data.items():
        for r in rows:
            by_t[r[0]].append(r[3])
    med = {t: statistics.median(v) for t, v in by_t.items() if len(v) >= 3}
    out = {}
    for c, rows in data.items():
        if not rows:
            continue
        end = rows[-1][0]
        d1 = [r[3] - med[r[0]] for r in rows if r[0] in med and r[0] >= end - 3600]
        d24 = [abs(r[3] - med[r[0]]) for r in rows if r[0] in med and r[0] >= end - 86400]
        out[c] = (statistics.median(d1) if d1 else 0.0, max(d24) if d24 else 0.0)
    return out


def max_rise(rows: list, horizon_s: float) -> float:
    """임의의 horizon 구간 안에서 비트겟 가격의 최대 상승률(%). 숏 청산 위험 지표."""
    best, dq = 0.0, deque()            # dq: 구간 내 최저가 후보 (단조 증가)
    for r in rows:
        t, bg_mid = r[0], r[5]
        while dq and dq[0][0] < t - horizon_s:
            dq.popleft()
        while dq and dq[-1][1] >= bg_mid:
            dq.pop()
        dq.append((t, bg_mid))
        lo = dq[0][1]
        if lo > 0:
            best = max(best, (bg_mid / lo - 1) * 100)
    return best


def simulate(rows: list, q: float, window_s: float, spacing: float,
             hold_s: float, fee: float, maker: bool, interval: float) -> dict:
    """슬롯 1개를 실제 호가로 굴린다. 반환: 거래 목록 요약."""
    win, srt = deque(), []
    min_n = max(30, int(window_s / interval * 0.25))   # 창의 25% 이상 쌓여야 분위수 판단
    pos = None                                     # [진입가, 진입시각, 누적펀딩%]
    trades, last_t = [], None

    for t, en, ex, mid, fr, *_ in rows:
        px_in = mid if maker else en
        px_out = mid if maker else ex

        win.append((t, px_in))
        bisect.insort(srt, px_in)
        while win and win[0][0] < t - window_s:
            _, old = win.popleft()
            del srt[bisect.bisect_left(srt, old)]

        if pos is not None and last_t is not None:
            pos[2] += fr * 100 * (t - last_t) / (8 * 3600)     # 펀딩은 8시간 단위 비율
        last_t = t

        if pos is not None:
            gain = px_out - pos[0]
            if gain >= spacing or t - pos[1] >= hold_s:
                trades.append((gain - fee + pos[2], gain >= spacing, t - pos[1]))
                pos = None
            continue

        if len(srt) >= min_n and px_in <= srt[int(q * (len(srt) - 1))]:
            pos = [px_in, t, 0.0]

    unreal = (rows[-1][3 if maker else 2] - pos[0]) if (pos and rows) else 0.0
    return {"trades": trades, "open_unreal": unreal, "open": pos is not None}


# ── 판정 ──────────────────────────────────────────────────────────

def evaluate(data: dict, args) -> list:
    fee = FEE_MAKER if args.maker else FEE_MARKET
    margin = args.spacing - fee
    div = divergence(data)
    span_all = max(r[-1][0] for r in data.values() if r) - min(r[0][0] for r in data.values() if r)
    expected = span_all / args.interval + 1

    out = []
    for c, rows in sorted(data.items()):
        if len(rows) < 2:
            continue
        span = rows[-1][0] - rows[0][0]
        days = max(span / 86400, 1e-9)
        cost = statistics.median(r[1] - r[2] for r in rows)
        spread_up = [(r[7] - r[6]) / ((r[7] + r[6]) / 2) * 100 for r in rows if r[6] > 0]
        tick = min(spread_up) if spread_up else float("nan")     # 가장 좁은 스프레드 ≈ 1틱
        funds = [r[4] for r in rows]
        sim = simulate(rows, args.q, args.window * 3600, args.spacing,
                       args.hold * 3600, fee, args.maker, args.interval)
        tr = sim["trades"]
        total = sum(x[0] for x in tr)
        d1, d24 = div.get(c, (0.0, 0.0))

        m = {
            "coin": c, "rows": len(rows), "coverage": len(rows) / expected,
            "cost_med": cost, "tick_pct": tick,
            "div_1h": d1, "div_24h_max": d24,
            "rise_24h_max": max_rise(rows, 86400),
            "funding_mean_8h": statistics.mean(funds) * 100,
            "funding_pos_share": sum(f > 0 for f in funds) / len(funds),
            "n_trades": len(tr), "n_tp": sum(1 for x in tr if x[1]),
            "avg_hold_h": statistics.mean(x[2] for x in tr) / 3600 if tr else 0.0,
            "net_per_trade": total / len(tr) if tr else 0.0,
            "net_per_day": total / days,
            "open_unreal": sim["open_unreal"],
        }

        reasons, notes = [], []
        if m["coverage"] < 0.5:
            reasons.append("데이터 부족")
        # 지정가도 스프레드 조건을 면제하지 않는다. 한쪽만 체결되면 반대쪽은 시장가로
        # 맞춰야 하고, 그때 스프레드가 순마진보다 넓으면 한 번에 그 거래 이익이 사라진다.
        if cost > margin:
            reasons.append(f"비용 {cost:.3f}%>{margin:.2f}%")
        if abs(d1) > args.div_max:
            reasons.append(f"괴리 {d1:+.2f}%p")
        elif d24 > args.div_max * 1.5:
            notes.append(f"24h 괴리 {d24:.2f}%p")
        if m["rise_24h_max"] > args.rise_max:
            reasons.append(f"급등 {m['rise_24h_max']:.1f}%")
        # 거래가 0회인 것과 기대수익이 음수인 것은 다르다. 전자를 후자로 표시하면
        # 데이터가 모자랄 뿐인 코인을 나쁜 코인으로 오해하게 된다.
        need_h = args.window * 0.25 + 1                  # 첫 진입 판단이 가능해지는 시간
        span_h = span / 3600
        if span_h < need_h:
            reasons.append(f"판단 보류: 데이터 {span_h:.1f}h (최소 {need_h:.0f}h)")
        elif not tr:
            reasons.append("거래 없음 (진입 조건 미충족)")
        elif m["net_per_day"] <= 0:
            reasons.append("기대수익≤0")
        if m["funding_pos_share"] < 0.5:
            notes.append("펀딩 음수 잦음")
        m["excluded"], m["notes"] = reasons, notes
        out.append(m)

    out.sort(key=lambda m: (bool(m["excluded"]), -m["net_per_day"]))
    return out


def report(res: list, args, files: list, span_h: float):
    mode = "지정가(중간가 체결 가정)" if args.maker else "시장가(호가 체결)"
    fee = FEE_MAKER if args.maker else FEE_MARKET
    print("=" * 112)
    print(f"  코인 선정 — {mode}  spacing {args.spacing}%  수수료 {fee}%  "
          f"진입 하위 {args.q*100:.0f}% (창 {args.window}h)  보유한도 {args.hold}h")
    print(f"  데이터 {len(files)}개 파일, {span_h:.1f}시간  |  슬롯 {args.slot:,}원 기준")
    if span_h < args.window + 48:
        print(f"  ⚠ 데이터가 {span_h:.1f}시간뿐이라 순위 신뢰도가 낮습니다 (권장: 3일 이상, 가급적 7일)")
    print("=" * 112)
    print(f"  {'코인':6}{'비용':>8}{'1틱':>7}{'괴리1h':>8}{'급등24h':>8}{'펀딩/8h':>9}"
          f"{'거래':>5}{'익절':>5}{'보유h':>7}{'건당':>9}{'일%':>8}{'일원':>8}  판정")
    print("  " + "-" * 108)
    for m in res:
        verdict = "제외: " + ", ".join(m["excluded"]) if m["excluded"] else "통과"
        if m["notes"]:
            verdict += "  (주의: " + ", ".join(m["notes"]) + ")"
        print(f"  {m['coin']:6}{m['cost_med']:>7.3f}%{m['tick_pct']:>6.3f}%{m['div_1h']:>+7.2f}p"
              f"{m['rise_24h_max']:>7.1f}%{m['funding_mean_8h']:>8.4f}%"
              f"{m['n_trades']:>5}{m['n_tp']:>5}{m['avg_hold_h']:>7.1f}"
              f"{m['net_per_trade']:>+8.3f}%{m['net_per_day']:>+7.3f}%"
              f"{m['net_per_day']/100*args.slot:>+8,.0f}  {verdict}")
    print("  " + "-" * 108)
    picks = [m["coin"] for m in res if not m["excluded"]][:args.top]
    pending = all(any(r.startswith("판단 보류") for r in m["excluded"]) for m in res)
    if pending:
        print(f"  추천 보류 — 데이터가 더 쌓여야 판정할 수 있습니다 "
              f"(첫 판정 최소 {args.window*0.25+1:.0f}시간, 신뢰할 순위는 3~7일)")
    else:
        print(f"  추천 {len(picks)}개: {picks if picks else '없음'}")
    print("  비용=즉시왕복 호가비용 중앙값  1틱=업비트 최소 스프레드  괴리=전 코인 중앙값 대비 김프 차이")
    print("  일%·일원 = 슬롯 1개를 굴린 하루 기대수익 (수수료·펀딩 반영). 코인 간 비교용")
    print("=" * 112)
    return picks


def parse_args(argv=None):
    """명령줄 옵션. 다른 도구가 기본값을 그대로 쓰려면 parse_args([]) 로 부른다."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=QUOTES_DIR, help="호가 로그 폴더")
    ap.add_argument("--days", type=int, default=7, help="최근 며칠치를 볼지")
    ap.add_argument("--interval", type=int, default=10, help="로거 기록 간격(초)")
    ap.add_argument("--spacing", type=float, default=0.3, help="익절 폭(%%)")
    ap.add_argument("--q", type=float, default=0.2, help="진입 분위수 (0.2 = 하위 20%%)")
    ap.add_argument("--window", type=float, default=24, help="분위수 이동창(시간)")
    ap.add_argument("--hold", type=float, default=72, help="보유 한도(시간). 초과 시 청산")
    ap.add_argument("--maker", action="store_true", help="지정가 가정")
    ap.add_argument("--div-max", type=float, default=1.0, help="괴리 제외 기준(%%p)")
    ap.add_argument("--rise-max", type=float, default=12.0, help="24h 급등 제외 기준(%%)")
    ap.add_argument("--slot", type=int, default=333_333, help="슬롯 자본(원) — 표시용")
    ap.add_argument("--top", type=int, default=5, help="추천 개수")
    ap.add_argument("--no-save", action="store_true", help="결과 JSON 저장 안 함")
    return ap.parse_args(argv)


def main() -> int:
    args = parse_args()

    data, files = load(args.dir, args.days)
    if not data:
        print(f"호가 로그 없음: {args.dir}")
        return 1
    t0 = min(r[0][0] for r in data.values() if r)
    t1 = max(r[-1][0] for r in data.values() if r)
    res = evaluate(data, args)
    picks = report(res, args, files, (t1 - t0) / 3600)

    if not args.no_save:
        os.makedirs(SELECT_DIR, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        p = os.path.join(SELECT_DIR, f"selection_{stamp}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"created_utc": stamp, "params": vars(args), "picks": picks,
                       "span_hours": (t1 - t0) / 3600, "coins": res},
                      f, ensure_ascii=False, indent=2)
        print(f"  저장: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
