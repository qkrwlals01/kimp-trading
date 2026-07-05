"""
플로팅 그리드 튜닝 분석기
실행: python tune.py [days=7]

출력:
  - 터미널: 코인별 김프 통계 + spacing/n_slots 추천
  - kimp_tune_YYYYMMDD.png: 시계열 + 분포 차트
"""

import sys, os, requests, time, statistics
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 7

KST       = timezone(timedelta(hours=9))
now_utc   = datetime.now(timezone.utc)
start_utc = now_utc - timedelta(days=DAYS)

# 현재 설정값 (비교용)
CURRENT_SPACING = 0.2
CURRENT_SLOTS   = 5
CURRENT_CAPITAL = 10_000_000   # 코인당 업비트 자본
FEE_RATE        = 0.0018       # 총 수수료율 (업비트 0.1% + 비트겟 0.08%)

COINS = {
    "BTC":  {"upbit": "KRW-BTC",  "bitget": "BTCUSDT"},
    "ETH":  {"upbit": "KRW-ETH",  "bitget": "ETHUSDT"},
    "XRP":  {"upbit": "KRW-XRP",  "bitget": "XRPUSDT"},
    "SOL":  {"upbit": "KRW-SOL",  "bitget": "SOLUSDT"},
    "AVAX": {"upbit": "KRW-AVAX", "bitget": "AVAXUSDT"},
    "LINK": {"upbit": "KRW-LINK", "bitget": "LINKUSDT"},
    "BCH":  {"upbit": "KRW-BCH",  "bitget": "BCHUSDT"},
    "SUI":  {"upbit": "KRW-SUI",  "bitget": "SUIUSDT"},
    "UNI":  {"upbit": "KRW-UNI",  "bitget": "UNIUSDT"},
    "TAO":  {"upbit": "KRW-TAO",  "bitget": "TAOUSDT"},
}


# ── 데이터 수집 ───────────────────────────────────────────────────

def fetch_upbit(market: str) -> dict:
    url    = "https://api.upbit.com/v1/candles/minutes/60"
    result = {}
    to_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    for _ in range(4):
        resp = requests.get(url, params={"market": market, "to": to_str, "count": 200}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        for c in data:
            dt = datetime.strptime(c["candle_date_time_utc"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            result[dt] = float(c["trade_price"])
        oldest = datetime.strptime(data[-1]["candle_date_time_utc"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        if oldest <= start_utc:
            break
        to_str = oldest.strftime("%Y-%m-%dT%H:%M:%SZ")
        time.sleep(0.2)
    return {dt: px for dt, px in result.items() if dt >= start_utc}


def fetch_bitget(symbol: str) -> dict:
    url    = "https://api.bitget.com/api/v2/mix/market/candles"
    result = {}
    end_ms = int(now_utc.timestamp() * 1000)
    for _ in range(4):
        start_ms = max(int(start_utc.timestamp() * 1000), end_ms - 200 * 3_600_000)
        params   = {"symbol": symbol, "productType": "USDT-FUTURES",
                    "granularity": "1H", "startTime": str(start_ms),
                    "endTime": str(end_ms), "limit": "200"}
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "00000" or not data.get("data"):
            break
        for c in data["data"]:
            dt = datetime.fromtimestamp(int(c[0]) / 1000, tz=timezone.utc)
            result[dt] = float(c[4])
        oldest_ms = int(data["data"][0][0])
        if oldest_ms <= int(start_utc.timestamp() * 1000):
            break
        end_ms = oldest_ms
        time.sleep(0.2)
    return {dt: px for dt, px in result.items() if dt >= start_utc}


def fetch_usd_krw() -> float:
    for url in [
        "https://quotation-api-cdn.dunamu.com/v1/forex/recent?codes=FRX.KRWUSD",
        "https://crix-api-endpoint.upbit.com/v1/forex/recent?codes=FRX.KRWUSD",
    ]:
        try:
            data = requests.get(url, timeout=5).json()
            if isinstance(data, list) and "basePrice" in data[0]:
                return float(data[0]["basePrice"])
        except Exception:
            continue
    raise RuntimeError("환율 API 실패")


# ── 플로팅 그리드 시뮬레이션 ─────────────────────────────────────

def simulate(series: list, spacing: float, n_slots: int, capital: int) -> dict:
    """
    1시간봉 김프 시계열로 플로팅 그리드 시뮬레이션.
    - 빈 슬롯 생기면 즉시 현재 김프로 진입
    - 기존 슬롯과 spacing/2 이내 중복 진입 방지
    - 목표 달성(entry + spacing) 시 익절
    """
    cpg = capital / n_slots
    fee = FEE_RATE * cpg

    slots = []   # [(entry_premium, target_premium)]
    trades = 0
    net_total = 0.0
    gross_total = 0.0

    for _, prem in series:
        # 익절 체크
        exited = [s for s in slots if prem >= s[1]]
        for s in exited:
            gross = spacing / 100 * cpg
            net_total += gross - fee
            gross_total += gross
            trades += 1
            slots.remove(s)

        # 진입 체크
        while len(slots) < n_slots:
            overlap = any(abs(s[0] - prem) < spacing * 0.5 for s in slots)
            if overlap:
                break
            slots.append((prem, prem + spacing))

    return {
        "trades":      trades,
        "net_total":   net_total,
        "gross_total": gross_total,
        "fee_total":   fee * trades,
        "net_per":     net_total / trades if trades else 0,
        "daily_trades": trades / DAYS,
    }


# ── 통계 계산 ────────────────────────────────────────────────────

def calc_stats(series: list) -> dict:
    prems = [p for _, p in series]
    s = sorted(prems)
    n = len(s)
    def pct(p): return s[max(0, int(n * p / 100) - 1)]

    # 일간 변동폭
    by_day = {}
    for dt, p in series:
        day = dt.astimezone(KST).date()
        by_day.setdefault(day, []).append(p)
    ranges = [max(v) - min(v) for v in by_day.values() if len(v) >= 4]
    daily_range = statistics.mean(ranges) if ranges else 0

    return {
        "n":           n,
        "mean":        statistics.mean(prems),
        "std":         statistics.stdev(prems) if n > 1 else 0,
        "min":         s[0],
        "max":         s[-1],
        "p10":         pct(10),
        "p25":         pct(25),
        "p50":         pct(50),
        "p75":         pct(75),
        "p90":         pct(90),
        "daily_range": daily_range,
    }


def recommend_spacing(daily_range: float) -> float:
    """
    하루 평균 변동폭 기준 spacing 추천.
    슬롯당 하루 2~4회 순환 목표 → daily_range / 6 정도
    최소 손익분기(0.19%) 이상, 0.05% 단위 반올림
    """
    raw = daily_range / 6
    raw = max(0.20, raw)
    return round(raw * 20) / 20   # 0.05% 단위


def recommend_slots(daily_range: float, spacing: float) -> int:
    """김프 일간 변동폭을 spacing으로 커버할 슬롯 수 (3~8 범위)"""
    raw = round(daily_range / spacing)
    return max(3, min(8, raw))


# ── 메인 ────────────────────────────────────────────────────────

print(f"\n조회 기간: 최근 {DAYS}일  ({start_utc.strftime('%Y-%m-%d')} ~ 오늘)")
print("환율 조회 중...")
usd_krw = fetch_usd_krw()
print(f"  USD/KRW = {usd_krw:,.0f}\n")

results = {}

for coin, cfg in COINS.items():
    print(f"[{coin}] 수집 중...", end=" ", flush=True)
    try:
        upbit_d  = fetch_upbit(cfg["upbit"])
        bitget_d = fetch_bitget(cfg["bitget"])
        series = []
        for dt in sorted(upbit_d):
            if dt in bitget_d:
                up = upbit_d[dt]
                bg = bitget_d[dt] * usd_krw
                series.append((dt, (up - bg) / bg * 100))
        if len(series) < 24:
            print("데이터 부족 — 건너뜀")
            continue
        results[coin] = {"series": series, "stats": calc_stats(series)}
        print(f"{len(series)}개 포인트")
    except Exception as e:
        print(f"오류: {e}")

DIVIDER = "=" * 70
print(f"\n{DIVIDER}")
print(f"  플로팅 그리드 튜닝 결과  |  최근 {DAYS}일  |  현재 spacing={CURRENT_SPACING}%  슬롯={CURRENT_SLOTS}개")
print(DIVIDER)

# 현재 설정 시뮬 vs 추천 설정 시뮬
summary_rows = []

for coin, data in results.items():
    series = data["series"]
    st     = data["stats"]

    rec_sp  = recommend_spacing(st["daily_range"])
    rec_sl  = recommend_slots(st["daily_range"], rec_sp)

    cur_sim = simulate(series, CURRENT_SPACING, CURRENT_SLOTS, CURRENT_CAPITAL)
    rec_sim = simulate(series, rec_sp, rec_sl, CURRENT_CAPITAL)

    data["rec_spacing"] = rec_sp
    data["rec_slots"]   = rec_sl
    data["cur_sim"]     = cur_sim
    data["rec_sim"]     = rec_sim

    summary_rows.append((coin, st, cur_sim, rec_sim, rec_sp, rec_sl))

    print(f"\n[{coin}]")
    print(f"  김프: 평균 {st['mean']:+.2f}%  표준편차 {st['std']:.2f}%p  "
          f"일간변동폭 {st['daily_range']:.2f}%p")
    print(f"  범위: {st['min']:+.2f}% ~ {st['max']:+.2f}%  "
          f"(p10={st['p10']:+.2f}%  p50={st['p50']:+.2f}%  p90={st['p90']:+.2f}%)")
    print(f"  {'':20} {'현재 설정':>18}   {'추천 설정':>18}")
    print(f"  {'spacing':20} {CURRENT_SPACING:>17.2f}%   {rec_sp:>17.2f}%")
    print(f"  {'n_slots':20} {CURRENT_SLOTS:>18}   {rec_sl:>18}")
    print(f"  {'시뮬 거래건수/일':20} {cur_sim['daily_trades']:>17.1f}건   {rec_sim['daily_trades']:>17.1f}건")
    print(f"  {'시뮬 순수익({DAYS}일)':20} {cur_sim['net_total']/10000:>+16.2f}만원   {rec_sim['net_total']/10000:>+16.2f}만원")
    print(f"  {'건당 순수익':20} {cur_sim['net_per']:>+17.0f}원   {rec_sim['net_per']:>+17.0f}원")

# 전체 합산
print(f"\n{DIVIDER}")
print(f"  {'':20} {'현재 설정 합산':>18}   {'추천 설정 합산':>18}")
cur_total = sum(r[2]["net_total"] for r in summary_rows)
rec_total = sum(r[3]["net_total"] for r in summary_rows)
cur_day   = sum(r[2]["daily_trades"] for r in summary_rows)
rec_day   = sum(r[3]["daily_trades"] for r in summary_rows)
print(f"  {'일 거래건수':20} {cur_day:>17.1f}건   {rec_day:>17.1f}건")
print(f"  {'순수익({DAYS}일)':20} {cur_total/10000:>+16.2f}만원   {rec_total/10000:>+16.2f}만원")
print(f"  {'월 환산 수익률':20} {cur_total/DAYS*30/100_000_000*100:>16.2f}%   {rec_total/DAYS*30/100_000_000*100:>16.2f}%")
print(DIVIDER)
print("  주의: 1시간봉 시뮬이므로 실제 10초 폴링 대비 거래건수 과소 추정됩니다.")
print("        실제 수익은 시뮬의 5~10배 이상일 수 있습니다.")
print(DIVIDER)


# ── 차트 ────────────────────────────────────────────────────────

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import matplotlib.gridspec as gridspec
    from matplotlib import rcParams

    rcParams["font.family"] = ["Malgun Gothic", "AppleGothic", "DejaVu Sans"]
    rcParams["axes.unicode_minus"] = False

    n = len(results)
    fig = plt.figure(figsize=(16, 4 * n))
    fig.suptitle(
        f"플로팅 그리드 튜닝  |  최근 {DAYS}일  |  USD/KRW {usd_krw:,.0f}",
        fontsize=13, fontweight="bold", y=1.01
    )
    gs_outer = gridspec.GridSpec(n, 1, figure=fig, hspace=0.6)

    for row, (coin, data) in enumerate(results.items()):
        series  = data["series"]
        st      = data["stats"]
        rec_sp  = data["rec_spacing"]
        rec_sl  = data["rec_slots"]
        cur_sim = data["cur_sim"]
        rec_sim = data["rec_sim"]

        times = [dt.astimezone(KST) for dt, _ in series]
        prems = [p for _, p in series]

        gs_inner = gridspec.GridSpecFromSubplotSpec(1, 2, subplot_spec=gs_outer[row],
                                                    width_ratios=[3, 1], wspace=0.05)
        ax_ts   = fig.add_subplot(gs_inner[0])
        ax_hist = fig.add_subplot(gs_inner[1], sharey=ax_ts)

        # 시계열
        ax_ts.axhline(0, color="#999", lw=0.8, ls="--")
        ax_ts.plot(times, prems, color="#e67e22", lw=1.2, zorder=4)
        ax_ts.fill_between(times, prems, 0,
                           where=[p >= 0 for p in prems], alpha=0.15, color="#e74c3c")
        ax_ts.fill_between(times, prems, 0,
                           where=[p < 0 for p in prems], alpha=0.15, color="#3498db")

        # 퍼센타일 밴드
        ax_ts.axhspan(st["p10"], st["p90"], alpha=0.06, color="#888", label="p10~p90 범위")

        # p50 중앙선
        ax_ts.axhline(st["p50"], color="#888", lw=0.8, ls="-.", alpha=0.7, label=f"p50 {st['p50']:+.2f}%")

        title = (f"[{coin}]  평균 {st['mean']:+.2f}%  std {st['std']:.2f}%p  "
                 f"일간변동 {st['daily_range']:.2f}%p  |  "
                 f"현재 {cur_sim['daily_trades']:.1f}건/일 {cur_sim['net_total']/10000:+.1f}만원({DAYS}일)  "
                 f"→  추천 spacing={rec_sp}% slots={rec_sl} "
                 f"{rec_sim['daily_trades']:.1f}건/일 {rec_sim['net_total']/10000:+.1f}만원")
        ax_ts.set_title(title, fontsize=9, loc="left")
        ax_ts.set_ylabel("김치 프리미엄 (%)", fontsize=9)
        ax_ts.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d", tz=KST))
        ax_ts.xaxis.set_major_locator(mdates.DayLocator(tz=KST))
        ax_ts.tick_params(axis="x", rotation=30, labelsize=8)
        ax_ts.tick_params(axis="y", labelsize=8)
        ax_ts.legend(fontsize=7, loc="upper left")
        ax_ts.set_facecolor("#f8f9fa")
        ax_ts.grid(axis="y", alpha=0.3)

        # 히스토그램
        ax_hist.hist(prems, bins=40, orientation="horizontal",
                     color="#e67e22", alpha=0.7, edgecolor="none")
        ax_hist.axhline(st["p10"], color="#aaa", lw=1.0, ls="-.")
        ax_hist.axhline(st["p90"], color="#aaa", lw=1.0, ls="-.")
        ax_hist.set_xlabel("빈도", fontsize=8)
        ax_hist.tick_params(labelleft=False, labelsize=8)
        ax_hist.set_facecolor("#f8f9fa")

        info = (f"추천\nspacing {rec_sp:.2f}%\nslots   {rec_sl}개\n"
                f"{rec_sim['daily_trades']:.1f}건/일\n{rec_sim['net_per']:+.0f}원/건")
        ax_hist.text(0.97, 0.03, info, transform=ax_hist.transAxes,
                     fontsize=8, va="bottom", ha="right",
                     bbox=dict(boxstyle="round,pad=0.4", fc="white", ec="#2ecc71", alpha=0.9))

    date_str = datetime.now(KST).strftime("%Y%m%d")
    out_path = os.path.join(os.path.dirname(__file__), f"kimp_tune_{date_str}.png")
    plt.savefig(out_path, dpi=130, bbox_inches="tight")
    print(f"\n차트 저장: {out_path}")

except ImportError:
    print("\nmatplotlib 없음 — 차트 생략 (pip install matplotlib)")
