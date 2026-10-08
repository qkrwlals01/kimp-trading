"""
슬리피지 + 펀딩비 반영 연평균 수익률 분석
진입 1.0% / 청산 3.0% / 업비트 100만원 + 비트겟 50만원
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
UPBIT_KRW         = 1_000_000
BITGET_MARGIN_KRW = 500_000
TOTAL_CAPITAL     = UPBIT_KRW + BITGET_MARGIN_KRW   # 150만원
UPBIT_FEE         = 0.0005
BITGET_FEE        = 0.0006
ENTRY             = 1.0
EXIT              = 3.0

# ── 슬리피지 가정 ─────────────────────────────────────────────────
# BTC/KRW 100만원 규모는 매우 소액 → 체결 영향 거의 없음
# 업비트 호가 스프레드 약 0.02~0.05%, 비트겟 선물 0.01~0.03%
SLIPPAGE_SCENARIOS = {
    "낙관 (0.02%)": 0.0002,
    "기본 (0.05%)": 0.0005,
    "보수 (0.10%)": 0.0010,
}

# ── 펀딩비 가정 ───────────────────────────────────────────────────
# 비트겟 BTC 무기한 선물, 8시간마다 정산
# 숏 포지션: 펀딩률 > 0이면 수취, < 0이면 지급
# BTC 장기 평균 펀딩률 ≈ +0.01%/8h (강세장 편향)
# 최근(2022~2026) 낮아진 추세 → 보수 시나리오 포함
FUNDING_SCENARIOS = {
    "약세 (-0.005%/8h)": -0.00005,   # 숏이 펀딩비 지급
    "중립  (0.000%/8h)":  0.00000,
    "기본  (+0.010%/8h)": +0.00010,  # 역사적 평균 수준
    "강세  (+0.020%/8h)": +0.00020,  # 강한 강세장
}


# ── 벡터화 백테스트 ───────────────────────────────────────────────
def run_backtest(prem, u_px, b_px, fx, ts, entry_thresh, exit_thresh):
    ei_list, xi_list = [], []
    i = 0
    n = len(prem)
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
        return None

    ei = np.array(ei_list); xi = np.array(xi_list)
    btc_qty   = UPBIT_KRW / u_px[ei]
    short_qty = UPBIT_KRW / (b_px[ei] * fx[ei])

    upbit_pnl  = btc_qty   * (u_px[xi] - u_px[ei])
    bitget_pnl = short_qty * (b_px[ei] - b_px[xi]) * fx[xi]
    gross_pnl  = upbit_pnl + bitget_pnl

    base_fee   = (UPBIT_FEE + BITGET_FEE) * 2 * UPBIT_KRW \
               + (UPBIT_FEE + BITGET_FEE) * 2 * (UPBIT_KRW / fx[ei])
    hold_hours = (ts[xi] - ts[ei]) / 3600

    entry_dt = pd.to_datetime(ts[ei], unit="s", utc=True)
    exit_dt  = pd.to_datetime(ts[xi], unit="s", utc=True)

    return dict(
        gross_pnl  = gross_pnl,
        base_fee   = base_fee,
        hold_hours = hold_hours,
        entry_dt   = entry_dt,
        exit_dt    = exit_dt,
        n          = len(ei),
    )


# ── 시나리오별 수익률 계산 ────────────────────────────────────────
def calc_scenario(trades, slip_rate, fund_rate_per_8h):
    """
    slip_rate      : 편도 슬리피지 비율 (양쪽 거래소 합산, 진입+청산 × 2)
    fund_rate_per_8h: 8시간당 펀딩비율 (+면 숏이 수취)
    """
    slip_cost     = slip_rate * 2 * UPBIT_KRW          # 왕복 슬리피지
    funding_income = (trades["hold_hours"] / 8) * fund_rate_per_8h * UPBIT_KRW

    net_pnl = trades["gross_pnl"] - trades["base_fee"] - slip_cost + funding_income
    return net_pnl


# ── 연평균 수익률(CAGR) ────────────────────────────────────────────
def cagr(total_pnl, start_dt, end_dt, capital):
    years = (end_dt - start_dt).days / 365.25
    return ((1 + total_pnl / capital) ** (1 / years) - 1) * 100


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

trades = run_backtest(prem, u_px, b_px, fx, ts, ENTRY, EXIT)
start_dt = df["datetime"].min()
end_dt   = df["datetime"].max()
years    = (end_dt - start_dt).days / 365.25

print(f"분석 기간: {start_dt.date()} ~ {end_dt.date()}  ({years:.1f}년)")
print(f"총 거래 수: {trades['n']}회  |  평균 보유: {trades['hold_hours'].mean():.0f}h")
print(f"총 자본: {TOTAL_CAPITAL//10000}만원  |  진입 {ENTRY}% / 청산 {EXIT}%\n")

# ── 테이블 출력 ───────────────────────────────────────────────────
print("=" * 78)
print(f"{'':30s} | {'총 수익(만원)':>12} | {'CAGR':>8} | {'거래당 평균':>10} | {'펀딩 합계':>9}")
print("=" * 78)

results = {}
for slip_label, slip in SLIPPAGE_SCENARIOS.items():
    for fund_label, fund in FUNDING_SCENARIOS.items():
        net = calc_scenario(trades, slip, fund)
        total = net.sum()
        avg   = net.mean()
        fund_total = ((trades["hold_hours"] / 8) * fund * UPBIT_KRW).sum()
        c = cagr(total, start_dt, end_dt, TOTAL_CAPITAL)
        key = f"{slip_label} / {fund_label}"
        results[key] = dict(total=total, cagr=c, avg=avg, fund_total=fund_total)
        print(f"  {key:<28} | {total/10000:>+11.1f} | {c:>+7.1f}% | {avg/10000:>+9.2f}만원 | {fund_total/10000:>+8.1f}만원")

print("=" * 78)

# 기준 (슬리피지 기본 + 펀딩 기본)
base_key = "기본 (0.05%) / 기본  (+0.010%/8h)"
base = results[base_key]
print(f"\n★ 기준 시나리오 ({base_key})")
print(f"  총 수익:   {base['total']/10000:+.1f}만원")
print(f"  CAGR:      {base['cagr']:+.1f}%  (연평균 복리 수익률)")
print(f"  거래당:    {base['avg']/10000:+.3f}만원")

# 비용 분해
net_base = calc_scenario(trades, 0.0005, 0.00010)
gross_total   = trades["gross_pnl"].sum()
fee_total     = trades["base_fee"].sum()
slip_total    = 0.0005 * 2 * UPBIT_KRW * trades["n"]
funding_total = ((trades["hold_hours"] / 8) * 0.00010 * UPBIT_KRW).sum()

print(f"\n[ 수익 구성 분해 (기준 시나리오) ]")
print(f"  프리미엄 차익 (gross): {gross_total/10000:>+8.1f}만원")
print(f"  수수료 (거래소):       {-fee_total/10000:>+8.1f}만원")
print(f"  슬리피지:              {-slip_total/10000:>+8.1f}만원")
print(f"  펀딩비 수취:           {funding_total/10000:>+8.1f}만원")
print(f"  ────────────────────────────────")
print(f"  순수익 합계:           {net_base.sum()/10000:>+8.1f}만원")

# ── 시각화 ────────────────────────────────────────────────────────
fig = plt.figure(figsize=(18, 14))
gs  = gridspec.GridSpec(2, 2, figure=fig, hspace=0.45, wspace=0.35)

slip_labels  = list(SLIPPAGE_SCENARIOS.keys())
fund_labels  = list(FUNDING_SCENARIOS.keys())
slip_vals    = list(SLIPPAGE_SCENARIOS.values())
fund_vals    = list(FUNDING_SCENARIOS.values())

# (1) CAGR 히트맵
ax1 = fig.add_subplot(gs[0, 0])
cagr_matrix = np.array([
    [results[f"{s} / {f}"]["cagr"] for s in slip_labels]
    for f in fund_labels
])
im = ax1.imshow(cagr_matrix, cmap="RdYlGn", aspect="auto",
                vmin=cagr_matrix.min(), vmax=cagr_matrix.max())
plt.colorbar(im, ax=ax1, label="CAGR (%)")
ax1.set_xticks(range(len(slip_labels)))
ax1.set_yticks(range(len(fund_labels)))
ax1.set_xticklabels([s.split("(")[1].rstrip(")") for s in slip_labels], fontsize=9)
ax1.set_yticklabels([f.split("(")[1].rstrip(")") for f in fund_labels], fontsize=9)
ax1.set_xlabel("슬리피지 (편도)"); ax1.set_ylabel("펀딩비 (8시간당)")
ax1.set_title("CAGR 히트맵 (%)\n슬리피지 × 펀딩비 시나리오")
for i in range(len(fund_labels)):
    for j in range(len(slip_labels)):
        ax1.text(j, i, f"{cagr_matrix[i,j]:.1f}%",
                 ha="center", va="center", fontsize=10, fontweight="bold",
                 color="black")

# (2) 수익 구성 파이차트 (기준 시나리오)
ax2 = fig.add_subplot(gs[0, 1])
components = {
    "프리미엄 차익": gross_total,
    "펀딩비 수취":   funding_total,
}
costs = {
    "수수료":     -fee_total,
    "슬리피지":   -slip_total,
}
all_labels = list(components.keys()) + list(costs.keys())
all_vals   = list(components.values()) + list(costs.values())
colors     = ["#27ae60", "#3498db", "#e74c3c", "#e67e22"]
explode    = [0.05] * len(all_labels)
abs_vals   = [abs(v) for v in all_vals]
wedges, texts, autotexts = ax2.pie(
    abs_vals, labels=all_labels, colors=colors,
    autopct="%1.1f%%", explode=explode,
    startangle=90, textprops={"fontsize": 10}
)
ax2.set_title(f"수익/비용 구성 (기준 시나리오)\n총 순수익 {net_base.sum()/10000:.1f}만원 / {years:.1f}년")

# (3) 누적 손익 (기준 시나리오)
ax3 = fig.add_subplot(gs[1, :])
net_base_sorted = net_base[np.argsort(trades["exit_dt"].view("int64"))]
exit_dts_sorted = pd.to_datetime(
    np.sort(trades["exit_dt"].view("int64")), utc=True
)
cum_gross = np.cumsum(trades["gross_pnl"][np.argsort(trades["exit_dt"].view("int64"))]) / 10000
cum_net   = np.cumsum(net_base_sorted) / 10000

ax3.plot(exit_dts_sorted, cum_gross, color="#95a5a6", linewidth=1.5,
         linestyle="--", label=f"프리미엄 차익만  ({cum_gross[-1]:+.1f}만원)")
ax3.plot(exit_dts_sorted, cum_net,   color="#e74c3c", linewidth=2.0,
         label=f"수수료+슬리피지+펀딩비 반영  ({cum_net[-1]:+.1f}만원)")
ax3.fill_between(exit_dts_sorted, cum_net, cum_gross,
                 alpha=0.15, color="#3498db", label="비용 효과")
ax3.axhline(0, color="black", linewidth=0.8)
ax3.set_title(
    f"누적 손익: 비용 반영 전후 비교 (기준 시나리오)\n"
    f"CAGR {base['cagr']:+.1f}%  |  {years:.1f}년간 {TOTAL_CAPITAL//10000}만원 투자",
    fontsize=12
)
ax3.set_ylabel("누적 손익 (만원)"); ax3.legend(fontsize=10)
ax3.xaxis.set_major_formatter(plt.matplotlib.dates.DateFormatter("%Y"))
ax3.xaxis.set_major_locator(plt.matplotlib.dates.YearLocator())
ax3.grid(axis="y", alpha=0.3)

fig.suptitle(
    f"슬리피지 + 펀딩비 반영 수익률 분석  |  진입 {ENTRY}% / 청산 {EXIT}%  |  자본 {TOTAL_CAPITAL//10000}만원",
    fontsize=14, fontweight="bold", y=1.01
)
out = "data/return_analysis.png"
plt.savefig(out, dpi=150, bbox_inches="tight"); plt.close()
print(f"\n  저장: {out}")
