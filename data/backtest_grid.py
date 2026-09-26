"""
그리드 방식 김치 프리미엄 백테스트
N개의 그리드 슬롯이 프리미엄 구간별로 독립적으로 진입/청산
단일 임계값 전략과 성과 비교
"""

import matplotlib
matplotlib.use("Agg")
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import platform

if platform.system() == "Darwin":
    plt.rcParams["font.family"] = "AppleGothic"
plt.rcParams["axes.unicode_minus"] = False

# ── 기본 파라미터 ─────────────────────────────────────────────────
UPBIT_TOTAL      = 90_000_000    # 업비트 총 투자금 (원)
TOTAL_CAPITAL    = 100_000_000   # 전체 자본 (원)

UPBIT_FEE        = 0.0005
BITGET_FEE       = 0.0006
SLIPPAGE         = 0.0005        # 편도 슬리피지
FUND_RATE_PER_8H = 0.00010       # +0.01%/8h (숏 수취)

# ── 그리드 파라미터 ───────────────────────────────────────────────
N_GRIDS          = 10
GRID_BOTTOM      = 0.0           # 최하단 진입 프리미엄 (%)
GRID_TOP         = 3.0           # 최상단 그리드 레벨 (%)
GRID_SPACING     = (GRID_TOP - GRID_BOTTOM) / N_GRIDS   # 0.3%
CAPITAL_PER_GRID = UPBIT_TOTAL / N_GRIDS                # 900만원/슬롯

GRID_LEVELS = np.array([GRID_BOTTOM + k * GRID_SPACING for k in range(N_GRIDS)])
EXIT_LEVELS  = GRID_LEVELS + GRID_SPACING


# ── 그리드 백테스트 ───────────────────────────────────────────────
def run_grid_backtest(prem, u_px, b_px, fx, ts):
    """
    각 그리드 슬롯이 독립적으로 진입/청산하는 시뮬레이션
    슬롯 k:  프리미엄 <= GRID_LEVELS[k] 이면 진입
             프리미엄 >= EXIT_LEVELS[k]  이면 청산
    """
    n = len(prem)
    trades = []
    # 슬롯 상태: None(빈 슬롯) 또는 진입 정보 dict
    slots = [None] * N_GRIDS

    for i in range(n):
        p = prem[i]
        for k in range(N_GRIDS):
            if slots[k] is None:
                if p <= GRID_LEVELS[k]:
                    slots[k] = {
                        "ts":  ts[i],
                        "upx": u_px[i],
                        "bpx": b_px[i],
                        "fx":  fx[i],
                        "ep":  p,
                    }
            else:
                if p >= EXIT_LEVELS[k]:
                    s = slots[k]
                    btc_qty   = CAPITAL_PER_GRID / s["upx"]
                    short_qty = CAPITAL_PER_GRID / (s["bpx"] * s["fx"])

                    upbit_pnl  = btc_qty   * (u_px[i] - s["upx"])
                    bitget_pnl = short_qty * (s["bpx"] - b_px[i]) * fx[i]
                    gross_pnl  = upbit_pnl + bitget_pnl

                    hold_h  = (ts[i] - s["ts"]) / 3600
                    fee     = (UPBIT_FEE * 2 + BITGET_FEE * 2) * CAPITAL_PER_GRID
                    slip    = SLIPPAGE * 2 * CAPITAL_PER_GRID
                    funding = (hold_h / 8) * FUND_RATE_PER_8H * CAPITAL_PER_GRID
                    net_pnl = gross_pnl - fee - slip + funding

                    trades.append({
                        "grid_k":        k,
                        "grid_level":    GRID_LEVELS[k],
                        "entry_dt":      pd.Timestamp(s["ts"], unit="s", tz="UTC"),
                        "exit_dt":       pd.Timestamp(ts[i],   unit="s", tz="UTC"),
                        "entry_premium": s["ep"],
                        "exit_premium":  p,
                        "hold_hours":    hold_h,
                        "gross_pnl":     gross_pnl,
                        "net_pnl":       net_pnl,
                    })
                    slots[k] = None

    return pd.DataFrame(trades)


# ── 단일 임계값 백테스트 (비교용) ─────────────────────────────────
def run_single_backtest(prem, u_px, b_px, fx, ts, entry_thresh=0.0, exit_thresh=2.0):
    """원래 방식: 하나의 포지션, 진입 0% / 청산 2%"""
    capital = UPBIT_TOTAL
    ei_list, xi_list = [], []
    i, n = 0, len(prem)
    while i < n:
        if prem[i] <= entry_thresh:
            j = i + 1
            while j < n and prem[j] < exit_thresh:
                j += 1
            if j < n:
                ei_list.append(i); xi_list.append(j)
                i = j + 1; continue
            else:
                break
        i += 1
    if not ei_list:
        return pd.DataFrame()

    ei = np.array(ei_list); xi = np.array(xi_list)
    btc_qty   = capital / u_px[ei]
    short_qty = capital / (b_px[ei] * fx[ei])

    upbit_pnl  = btc_qty   * (u_px[xi] - u_px[ei])
    bitget_pnl = short_qty * (b_px[ei] - b_px[xi]) * fx[xi]
    gross_pnl  = upbit_pnl + bitget_pnl

    hold_h  = (ts[xi] - ts[ei]) / 3600
    fee     = (UPBIT_FEE * 2 + BITGET_FEE * 2) * capital
    slip    = SLIPPAGE * 2 * capital
    funding = (hold_h / 8) * FUND_RATE_PER_8H * capital
    net_pnl = gross_pnl - fee - slip + funding

    return pd.DataFrame({
        "entry_dt":  pd.to_datetime(ts[ei], unit="s", utc=True),
        "exit_dt":   pd.to_datetime(ts[xi], unit="s", utc=True),
        "hold_hours": hold_h,
        "gross_pnl": gross_pnl,
        "net_pnl":   net_pnl,
    })


# ── CAGR ─────────────────────────────────────────────────────────
def cagr(total_pnl, start_dt, end_dt):
    years = (end_dt - start_dt).days / 365.25
    return ((1 + total_pnl / TOTAL_CAPITAL) ** (1 / years) - 1) * 100


# ── 최대 낙폭 ─────────────────────────────────────────────────────
def max_drawdown(cum_pnl):
    peak = np.maximum.accumulate(cum_pnl)
    dd   = (cum_pnl - peak) / (TOTAL_CAPITAL + peak)
    return dd.min() * 100


# ── 메인 ─────────────────────────────────────────────────────────
df = pd.read_csv("data/kimp_history_hourly.csv", parse_dates=["datetime"])
df = df.sort_values("datetime").reset_index(drop=True)
df["usd_krw"] = df["usd_krw"].ffill()
df["ts"]      = df["datetime"].astype("int64") // 10**9

prem = df["premium_pct"].values
u_px = df["upbit_close"].values
b_px = df["binance_close"].values
fx   = df["usd_krw"].values
ts   = df["ts"].values.astype(float)

start_dt = df["datetime"].min()
end_dt   = df["datetime"].max()
years    = (end_dt - start_dt).days / 365.25

print(f"분석 기간: {start_dt.date()} ~ {end_dt.date()}  ({years:.1f}년)")
print(f"전체 자본: {TOTAL_CAPITAL//10000}만원  |  그리드 {N_GRIDS}개  |  간격 {GRID_SPACING:.2f}%\n")

# 그리드 백테스트 실행
print("그리드 백테스트 실행 중...", end=" ", flush=True)
grid_trades = run_grid_backtest(prem, u_px, b_px, fx, ts)
print(f"완료 ({len(grid_trades)}건)")

# 단일 임계값 백테스트 (비교용)
single_trades = run_single_backtest(prem, u_px, b_px, fx, ts, 0.0, 2.0)

# ── 결과 요약 ─────────────────────────────────────────────────────
print("\n" + "=" * 68)
print(f"{'':20s} | {'그리드 (10슬롯)':>16} | {'단일 (0%→2%)':>14}")
print("=" * 68)

g_total  = grid_trades["net_pnl"].sum()
g_gross  = grid_trades["gross_pnl"].sum()
g_wins   = (grid_trades["net_pnl"] > 0).sum()
g_n      = len(grid_trades)
g_cagr   = cagr(g_total, start_dt, end_dt)
g_avg    = grid_trades["net_pnl"].mean()

s_total  = single_trades["net_pnl"].sum()
s_wins   = (single_trades["net_pnl"] > 0).sum()
s_n      = len(single_trades)
s_cagr   = cagr(s_total, start_dt, end_dt)
s_avg    = single_trades["net_pnl"].mean()

rows = [
    ("총 거래 수",     f"{g_n}회",                    f"{s_n}회"),
    ("승률",          f"{g_wins/g_n*100:.1f}%",       f"{s_wins/s_n*100:.1f}%"),
    ("평균 보유 시간", f"{grid_trades['hold_hours'].mean():.0f}h",
                      f"{single_trades['hold_hours'].mean():.0f}h"),
    ("총 순수익",      f"{g_total/10000:+.1f}만원",    f"{s_total/10000:+.1f}만원"),
    ("CAGR",          f"{g_cagr:+.1f}%",              f"{s_cagr:+.1f}%"),
    ("거래당 평균",    f"{g_avg/10000:+.3f}만원",      f"{s_avg/10000:+.3f}만원"),
]
for label, gv, sv in rows:
    print(f"  {label:<18} | {gv:>16} | {sv:>14}")
print("=" * 68)

# 연도별 수익
print("\n[ 연도별 순수익 (그리드) ]")
grid_trades["year"] = grid_trades["exit_dt"].dt.year
annual = grid_trades.groupby("year")["net_pnl"].agg(["sum", "count"])
for yr, row in annual.iterrows():
    pnl_m = row["sum"] / 10000
    ret   = row["sum"] / TOTAL_CAPITAL * 100
    print(f"  {yr}년: {pnl_m:>+7.1f}만원  ({ret:>+5.1f}%)  {row['count']:.0f}건")

# 그리드 레벨별 성과
print("\n[ 그리드 레벨별 성과 ]")
print(f"  {'레벨':>6} | {'거래수':>5} | {'총수익(만원)':>11} | {'승률':>6} | {'평균보유(h)':>10}")
for k in range(N_GRIDS):
    sub = grid_trades[grid_trades["grid_k"] == k]
    if len(sub) == 0:
        continue
    wins = (sub["net_pnl"] > 0).sum()
    print(f"  {GRID_LEVELS[k]:>5.1f}% | {len(sub):>5} | {sub['net_pnl'].sum()/10000:>+10.1f} | "
          f"{wins/len(sub)*100:>5.1f}% | {sub['hold_hours'].mean():>10.0f}")

# ── 시각화 ────────────────────────────────────────────────────────
fig = plt.figure(figsize=(20, 16))
gs  = gridspec.GridSpec(3, 3, figure=fig, hspace=0.5, wspace=0.35)

# 시계열 기준 누적 수익
grid_sorted  = grid_trades.sort_values("exit_dt")
single_sorted = single_trades.sort_values("exit_dt")

cum_grid   = grid_sorted["net_pnl"].cumsum().values / 10000
cum_single = single_sorted["net_pnl"].cumsum().values / 10000

# (1) 누적 손익 비교
ax1 = fig.add_subplot(gs[0, :])
ax1.plot(grid_sorted["exit_dt"],   cum_grid,
         color="#e74c3c", linewidth=2.0,
         label=f"그리드 10슬롯  ({cum_grid[-1]:+.1f}만원  CAGR {g_cagr:+.1f}%)")
ax1.plot(single_sorted["exit_dt"], cum_single,
         color="#3498db", linewidth=1.5, linestyle="--",
         label=f"단일 0%→2%  ({cum_single[-1]:+.1f}만원  CAGR {s_cagr:+.1f}%)")
ax1.axhline(0, color="black", linewidth=0.7)
ax1.fill_between(grid_sorted["exit_dt"], cum_grid, 0,
                 where=(cum_grid > 0), alpha=0.1, color="#e74c3c")
ax1.set_title(
    f"누적 순수익 비교: 그리드 vs 단일 임계값  |  "
    f"자본 {TOTAL_CAPITAL//10000}만원  |  {years:.1f}년",
    fontsize=13, fontweight="bold"
)
ax1.set_ylabel("누적 순수익 (만원)")
ax1.legend(fontsize=11)
ax1.xaxis.set_major_formatter(plt.matplotlib.dates.DateFormatter("%Y"))
ax1.xaxis.set_major_locator(plt.matplotlib.dates.YearLocator())
ax1.grid(axis="y", alpha=0.3)

# (2) 그리드 레벨별 총 수익 막대
ax2 = fig.add_subplot(gs[1, 0])
per_grid = grid_trades.groupby("grid_k")["net_pnl"].sum() / 10000
colors_bar = ["#27ae60" if v >= 0 else "#e74c3c" for v in per_grid.values]
ax2.bar(range(N_GRIDS), per_grid.values, color=colors_bar, edgecolor="white")
ax2.set_xticks(range(N_GRIDS))
ax2.set_xticklabels([f"{lv:.1f}%" for lv in GRID_LEVELS], rotation=45, fontsize=8)
ax2.set_ylabel("총 순수익 (만원)")
ax2.set_title("그리드 레벨별 총 수익")
ax2.axhline(0, color="black", linewidth=0.7)

# (3) 그리드 레벨별 거래 수
ax3 = fig.add_subplot(gs[1, 1])
per_grid_cnt = grid_trades.groupby("grid_k").size()
ax3.bar(range(N_GRIDS), per_grid_cnt.values, color="#3498db", edgecolor="white")
ax3.set_xticks(range(N_GRIDS))
ax3.set_xticklabels([f"{lv:.1f}%" for lv in GRID_LEVELS], rotation=45, fontsize=8)
ax3.set_ylabel("거래 수")
ax3.set_title("그리드 레벨별 거래 횟수")

# (4) 보유 시간 분포
ax4 = fig.add_subplot(gs[1, 2])
ax4.hist(grid_trades["hold_hours"], bins=50, color="#9b59b6", edgecolor="white", alpha=0.8)
ax4.axvline(grid_trades["hold_hours"].mean(), color="red", linestyle="--",
            label=f"평균 {grid_trades['hold_hours'].mean():.0f}h")
ax4.set_xlabel("보유 시간 (h)")
ax4.set_ylabel("거래 수")
ax4.set_title("보유 시간 분포")
ax4.legend()

# (5) 연도별 수익 그리드 vs 단일
ax5 = fig.add_subplot(gs[2, :2])
grid_annual  = grid_sorted.groupby(grid_sorted["exit_dt"].dt.year)["net_pnl"].sum() / 10000
single_annual = single_sorted.groupby(single_sorted["exit_dt"].dt.year)["net_pnl"].sum() / 10000
years_list = sorted(set(list(grid_annual.index) + list(single_annual.index)))
x = np.arange(len(years_list))
w = 0.35
g_vals = [grid_annual.get(y, 0) for y in years_list]
s_vals = [single_annual.get(y, 0) for y in years_list]
ax5.bar(x - w/2, g_vals, w, color="#e74c3c", label="그리드", alpha=0.85)
ax5.bar(x + w/2, s_vals, w, color="#3498db", label="단일",   alpha=0.85)
ax5.set_xticks(x)
ax5.set_xticklabels([str(y) for y in years_list], fontsize=9)
ax5.set_ylabel("연간 순수익 (만원)")
ax5.set_title("연도별 순수익 비교")
ax5.legend()
ax5.axhline(0, color="black", linewidth=0.7)

# (6) 슬롯 활성화 비율 타임라인 (월별 평균 동시 활성 슬롯 수)
ax6 = fig.add_subplot(gs[2, 2])
grid_trades["month"] = grid_trades["exit_dt"].dt.to_period("M")
monthly_trades = grid_trades.groupby("month").size()
ax6.plot(range(len(monthly_trades)), monthly_trades.values, color="#e67e22", linewidth=1.5)
ax6.set_ylabel("월 거래 수")
ax6.set_title("월별 거래 빈도")
ax6.set_xlabel("경과 월")
ax6.axhline(monthly_trades.mean(), color="red", linestyle="--",
            label=f"평균 {monthly_trades.mean():.1f}건/월")
ax6.legend(fontsize=9)

fig.suptitle(
    f"그리드 전략 백테스트  |  {N_GRIDS}슬롯  |  {GRID_BOTTOM}%~{GRID_TOP}%  |  간격 {GRID_SPACING:.2f}%  "
    f"|  슬롯당 {CAPITAL_PER_GRID//10000:.0f}만원",
    fontsize=13, fontweight="bold", y=1.01
)

out = "data/backtest_grid.png"
plt.savefig(out, dpi=150, bbox_inches="tight")
plt.close()
print(f"\n  저장: {out}")
