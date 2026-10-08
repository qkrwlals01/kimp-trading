"""
멀티 코인 그리드 백테스트
BTC / ETH / XRP  |  코인당 균등 배분
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

# ── 전체 파라미터 ─────────────────────────────────────────────────
UPBIT_PER_COIN   = 9_000_000     # 코인당 업비트 (원)
TOTAL_PER_COIN   = 10_000_000    # 코인당 총 자본
N_COINS          = 3
TOTAL_CAPITAL    = TOTAL_PER_COIN * N_COINS  # 3천만원

UPBIT_FEE        = 0.0005
BITGET_FEE       = 0.0006
SLIPPAGE         = 0.0005
FUND_RATE_PER_8H = 0.00010

# ── 코인별 파라미터 (백테스트 기반 최적화) ───────────────────────
COIN_PARAMS = {
    "BTC": {"file": "data/kimp_btc_hourly.csv",
            "n": 8, "bottom": -2.0, "top": 2.0},
    "ETH": {"file": "data/kimp_eth_hourly.csv",
            "n": 8, "bottom": -2.0, "top": 4.0},
    "XRP": {"file": "data/kimp_xrp_hourly.csv",
            "n": 8, "bottom": -2.0, "top": 5.0},
}

# BTC는 기존 파일명 사용
import os
if not os.path.exists("data/kimp_btc_hourly.csv"):
    import shutil
    shutil.copy("data/kimp_history_hourly.csv", "data/kimp_btc_hourly.csv")


# ── 그리드 백테스트 함수 ──────────────────────────────────────────
def grid_backtest(prem, u_px, b_px, fx, ts,
                  n_grids, grid_bottom, grid_top, capital_per_grid):
    spacing = (grid_top - grid_bottom) / n_grids
    levels  = np.array([grid_bottom + k * spacing for k in range(n_grids)])
    exits   = levels + spacing

    trades = []
    slots  = [None] * n_grids

    for i in range(len(prem)):
        p = prem[i]
        for k in range(n_grids):
            if slots[k] is None:
                if p <= levels[k]:
                    slots[k] = {"ts": ts[i], "upx": u_px[i], "bpx": b_px[i],
                                "fx": fx[i], "ep": p}
            else:
                if p >= exits[k]:
                    s = slots[k]
                    btc_qty   = capital_per_grid / s["upx"]
                    short_qty = capital_per_grid / (s["bpx"] * s["fx"])
                    gross = (btc_qty * (u_px[i] - s["upx"]) +
                             short_qty * (s["bpx"] - b_px[i]) * fx[i])
                    hold_h = (ts[i] - s["ts"]) / 3600
                    fee  = (UPBIT_FEE * 2 + BITGET_FEE * 2) * capital_per_grid
                    slip = SLIPPAGE * 2 * capital_per_grid
                    fund = (hold_h / 8) * FUND_RATE_PER_8H * capital_per_grid
                    net  = gross - fee - slip + fund

                    trades.append({
                        "grid_k":  k,
                        "level":   levels[k],
                        "exit_dt": pd.Timestamp(ts[i], unit="s", tz="UTC"),
                        "entry_p": s["ep"],
                        "exit_p":  p,
                        "hold_h":  hold_h,
                        "gross":   gross,
                        "net":     net,
                    })
                    slots[k] = None

    return pd.DataFrame(trades)


# ── 코인별 백테스트 실행 ──────────────────────────────────────────
print("멀티 코인 그리드 백테스트\n")
results = {}

for coin, cfg in COIN_PARAMS.items():
    df = pd.read_csv(cfg["file"], parse_dates=["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)
    df["usd_krw"] = df["usd_krw"].ffill()
    df["ts"] = df["datetime"].astype("int64") // 10**9

    spacing = (cfg["top"] - cfg["bottom"]) / cfg["n"]
    cpg     = UPBIT_PER_COIN / cfg["n"]

    tr = grid_backtest(
        df["premium_pct"].values,
        df["upbit_close"].values,
        df["binance_close"].values,
        df["usd_krw"].values,
        df["ts"].values.astype(float),
        cfg["n"], cfg["bottom"], cfg["top"], cpg,
    )

    start_dt = df["datetime"].min()
    end_dt   = df["datetime"].max()
    years    = (end_dt - start_dt).days / 365.25
    total    = tr["net"].sum()
    cagr     = ((1 + total / TOTAL_PER_COIN) ** (1 / years) - 1) * 100

    results[coin] = {
        "trades":   tr,
        "start_dt": start_dt,
        "end_dt":   end_dt,
        "years":    years,
        "total":    total,
        "cagr":     cagr,
        "n_trades": len(tr),
        "win_rate": (tr["net"] > 0).mean() * 100,
        "avg_hold": tr["hold_h"].mean(),
        "spacing":  spacing,
        "cpg":      cpg,
        "prem_mean": df["premium_pct"].mean(),
        "prem_std":  df["premium_pct"].std(),
        "prem_min":  df["premium_pct"].min(),
        "prem_max":  df["premium_pct"].max(),
    }

# ── 결과 출력 ─────────────────────────────────────────────────────
print(f"자본: 코인당 {TOTAL_PER_COIN//10000}만원  ×  {N_COINS}코인  =  {TOTAL_CAPITAL//10000}만원 총투자\n")
print("=" * 78)
print(f"{'':5} | {'거래수':>6} | {'승률':>6} | {'총수익(만원)':>11} | {'CAGR':>7} | {'평균보유':>8} | {'간격':>5}")
print("=" * 78)

combined_total = 0
for coin, r in results.items():
    print(f"  {coin:<4} | {r['n_trades']:>6} | {r['win_rate']:>5.1f}% | "
          f"{r['total']/10000:>+10.1f} | {r['cagr']:>+6.1f}% | "
          f"{r['avg_hold']:>7.0f}h | {r['spacing']:.2f}%")
    combined_total += r["total"]

print("=" * 78)
combined_cagr = ((1 + combined_total / TOTAL_CAPITAL) ** (1 / 8.0) - 1) * 100
print(f"  합계                  | {sum(r['n_trades'] for r in results.values()):>6} | "
      f"                 | {combined_total/10000:>+10.1f} | {combined_cagr:>+6.1f}%")

# 코인별 프리미엄 분포
print(f"\n[ 코인별 프리미엄 분포 특성 ]")
print(f"  {'코인':>4} | {'평균':>7} | {'표준편차':>8} | {'최솟값':>8} | {'최댓값':>8}")
for coin, r in results.items():
    print(f"  {coin:>4} | {r['prem_mean']:>+7.2f}% | {r['prem_std']:>8.2f}% | "
          f"{r['prem_min']:>+8.2f}% | {r['prem_max']:>+8.2f}%")

# 연도별 합산 수익
print(f"\n[ 연도별 합산 순수익 (3코인 합계) ]")
all_trades = []
for coin, r in results.items():
    t = r["trades"].copy()
    t["coin"] = coin
    all_trades.append(t)
all_df = pd.concat(all_trades).sort_values("exit_dt")
annual = all_df.groupby(all_df["exit_dt"].dt.year)["net"].sum()
for yr, v in annual.items():
    ret = v / TOTAL_CAPITAL * 100
    n   = (all_df["exit_dt"].dt.year == yr).sum()
    print(f"  {yr}년: {v/10000:>+7.1f}만원  ({ret:>+5.1f}%)  {n}건")

# ── 시각화 ────────────────────────────────────────────────────────
fig = plt.figure(figsize=(20, 14))
gs  = gridspec.GridSpec(3, 3, figure=fig, hspace=0.5, wspace=0.35)
colors = {"BTC": "#f39c12", "ETH": "#3498db", "XRP": "#27ae60"}

# (1) 누적 손익 비교
ax1 = fig.add_subplot(gs[0, :])
for coin, r in results.items():
    tr_sorted = r["trades"].sort_values("exit_dt")
    cum = tr_sorted["net"].cumsum() / 10000
    ax1.plot(tr_sorted["exit_dt"], cum, color=colors[coin], linewidth=2,
             label=f"{coin}  {r['total']/10000:+.0f}만원  CAGR {r['cagr']:+.1f}%")

ax1.axhline(0, color="black", linewidth=0.8)
ax1.set_title(f"코인별 누적 순수익 비교  |  코인당 자본 {TOTAL_PER_COIN//10000}만원  |  그리드 {cfg['n']}슬롯",
              fontsize=12, fontweight="bold")
ax1.set_ylabel("누적 순수익 (만원)")
ax1.legend(fontsize=11)
ax1.xaxis.set_major_formatter(plt.matplotlib.dates.DateFormatter("%Y"))
ax1.xaxis.set_major_locator(plt.matplotlib.dates.YearLocator())
ax1.grid(axis="y", alpha=0.3)

# (2) 연도별 수익 막대 (코인 구분)
ax2 = fig.add_subplot(gs[1, :2])
years_list = sorted(all_df["exit_dt"].dt.year.unique())
x  = np.arange(len(years_list))
bw = 0.25
for i, (coin, r) in enumerate(results.items()):
    annual_coin = r["trades"].groupby(r["trades"]["exit_dt"].dt.year)["net"].sum() / 10000
    vals = [annual_coin.get(y, 0) for y in years_list]
    ax2.bar(x + (i - 1) * bw, vals, bw, color=colors[coin], label=coin, alpha=0.85)
ax2.set_xticks(x)
ax2.set_xticklabels([str(y) for y in years_list], fontsize=9)
ax2.axhline(0, color="black", linewidth=0.8)
ax2.set_ylabel("연간 순수익 (만원)")
ax2.set_title("연도별 코인별 수익")
ax2.legend()

# (3) 승률 + CAGR 비교
ax3 = fig.add_subplot(gs[1, 2])
coins_list = list(results.keys())
cagrs = [results[c]["cagr"] for c in coins_list]
wrates = [results[c]["win_rate"] for c in coins_list]
x3 = np.arange(len(coins_list))
ax3b = ax3.twinx()
ax3.bar(x3, cagrs, color=[colors[c] for c in coins_list], alpha=0.7, width=0.4)
ax3b.plot(x3, wrates, "o--", color="black", linewidth=1.5, label="승률")
ax3.set_xticks(x3); ax3.set_xticklabels(coins_list)
ax3.set_ylabel("CAGR (%)"); ax3b.set_ylabel("승률 (%)")
ax3.set_title("CAGR vs 승률")
ax3b.legend(loc="upper right")

# (4) 프리미엄 분포 비교 (박스플롯)
ax4 = fig.add_subplot(gs[2, :2])
prem_data = {}
for coin, cfg in COIN_PARAMS.items():
    df = pd.read_csv(cfg["file"], parse_dates=["datetime"])
    prem_data[coin] = df["premium_pct"].clip(-20, 30).values

bp = ax4.boxplot(list(prem_data.values()), tick_labels=list(prem_data.keys()),
                 patch_artist=True, notch=False,
                 boxprops=dict(linewidth=1.5),
                 medianprops=dict(color="red", linewidth=2))
for patch, coin in zip(bp["boxes"], prem_data.keys()):
    patch.set_facecolor(colors[coin])
    patch.set_alpha(0.6)
ax4.axhline(0, color="black", linewidth=0.8, linestyle="--")
ax4.set_ylabel("김치 프리미엄 (%)")
ax4.set_title("코인별 프리미엄 분포 (이상치 ±20% 클립)")
ax4.grid(axis="y", alpha=0.3)

# (5) 그리드 레벨별 수익 히트맵
ax5 = fig.add_subplot(gs[2, 2])
max_grids = max(cfg["n"] for cfg in COIN_PARAMS.values())
heatmap_data = np.zeros((N_COINS, max_grids))
for ci, (coin, r) in enumerate(results.items()):
    cfg = COIN_PARAMS[coin]
    spacing = (cfg["top"] - cfg["bottom"]) / cfg["n"]
    for ki in range(cfg["n"]):
        sub = r["trades"][r["trades"]["grid_k"] == ki]
        heatmap_data[ci, ki] = sub["net"].sum() / 10000 if len(sub) > 0 else 0

vabs = max(abs(heatmap_data.max()), abs(heatmap_data.min()), 1)
im = ax5.imshow(heatmap_data, cmap="RdYlGn", aspect="auto", vmin=-vabs, vmax=vabs)
plt.colorbar(im, ax=ax5, label="총수익 (만원)")
ax5.set_yticks(range(N_COINS))
ax5.set_yticklabels(list(results.keys()))
ax5.set_xlabel("그리드 슬롯 번호")
ax5.set_title("코인 × 슬롯 수익 히트맵")
for ci in range(N_COINS):
    for ki in range(max_grids):
        ax5.text(ki, ci, f"{heatmap_data[ci,ki]:.0f}", ha="center", va="center",
                 fontsize=7, color="black")

fig.suptitle(
    f"멀티 코인 그리드 백테스트  |  BTC / ETH / XRP  |  코인당 {TOTAL_PER_COIN//10000}만원  |  합산 CAGR {combined_cagr:+.1f}%",
    fontsize=13, fontweight="bold"
)
out = "data/backtest_multi_coin.png"
plt.savefig(out, dpi=150, bbox_inches="tight")
plt.close()
print(f"\n  저장: {out}")
