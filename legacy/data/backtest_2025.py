"""
2025년 연간 백테스트 (월별 성과)
증거금 여유율 모니터링 손절 포함
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

# ── 파라미터 ──────────────────────────────────────────────────────
UPBIT_PER_COIN   = 900_000      # 코인당 업비트 (원)
TOTAL_PER_COIN   = 1_000_000    # 코인당 총 자본
TOTAL_CAPITAL    = TOTAL_PER_COIN * 3

UPBIT_FEE        = 0.0005
BITGET_FEE       = 0.0006
SLIPPAGE         = 0.0005
FUND_RATE_PER_8H = 0.00010
STOP_MARGIN_RATIO = 0.20   # 마진 20% 이하 → 강제 청산
# 손절 기준 가격 상승률 = (1 - STOP_MARGIN_RATIO) / leverage
# BTC 10배 → +8%  /  ETH·XRP 5배 → +16%

COIN_PARAMS = {
    "BTC": {"file": "data/kimp_btc_hourly.csv", "n": 8, "bottom": -2.0, "top": 2.0, "leverage": 10},
    "ETH": {"file": "data/kimp_eth_hourly.csv", "n": 8, "bottom": -2.0, "top": 4.0, "leverage":  5},
    "XRP": {"file": "data/kimp_xrp_hourly.csv", "n": 8, "bottom": -2.0, "top": 5.0, "leverage":  5},
}

MONTHS_KR = ["1월","2월","3월","4월","5월","6월",
             "7월","8월","9월","10월","11월","12월"]


# ── 백테스트 함수 ─────────────────────────────────────────────────
def run_backtest(prem, u_px, b_px, fx, ts, n, bottom, top, cpg, leverage):
    spacing      = (top - bottom) / n
    levels       = np.array([bottom + k * spacing for k in range(n)])
    exits        = levels + spacing
    stop_pct     = (1 - STOP_MARGIN_RATIO) / leverage

    trades = []
    slots  = [None] * n

    for i in range(len(prem)):
        p = prem[i]
        for k in range(n):
            if slots[k] is None:
                if p <= levels[k]:
                    slots[k] = {
                        "ts":  ts[i], "upx": u_px[i],
                        "bpx": b_px[i], "fx": fx[i], "ep": p,
                    }
            else:
                s         = slots[k]
                price_chg = (b_px[i] - s["bpx"]) / s["bpx"]   # 양수 = 가격 상승 (숏에 불리)
                margin_rt = 1 - price_chg * leverage
                forced    = margin_rt <= STOP_MARGIN_RATIO       # 강제 청산 여부

                if p >= exits[k] or forced:
                    btc_qty   = cpg / s["upx"]
                    short_qty = cpg / (s["bpx"] * s["fx"])
                    gross     = (btc_qty * (u_px[i] - s["upx"]) +
                                 short_qty * (s["bpx"] - b_px[i]) * fx[i])
                    hold_h    = (ts[i] - s["ts"]) / 3600
                    fee       = (UPBIT_FEE * 2 + BITGET_FEE * 2) * cpg
                    slip      = SLIPPAGE * 2 * cpg
                    fund      = (hold_h / 8) * FUND_RATE_PER_8H * cpg
                    net       = gross - fee - slip + fund

                    trades.append({
                        "month":    pd.Timestamp(ts[i], unit="s", tz="UTC").month,
                        "exit_dt":  pd.Timestamp(ts[i], unit="s", tz="UTC"),
                        "entry_p":  s["ep"],
                        "exit_p":   p,
                        "hold_h":   hold_h,
                        "gross":    gross,
                        "net":      net,
                        "forced":   forced,
                        "margin_rt": margin_rt,
                        "grid_k":   k,
                        "level":    levels[k],
                    })
                    slots[k] = None

    return pd.DataFrame(trades)


# ── 메인 ─────────────────────────────────────────────────────────
print("=" * 72)
print(f"  2025년 연간 백테스트  |  손절: 증거금 {STOP_MARGIN_RATIO*100:.0f}% 이하 강제 청산")
print(f"  코인당 자본: 업비트 {UPBIT_PER_COIN//10000}만원 + 비트겟 {(TOTAL_PER_COIN-UPBIT_PER_COIN)//10000}만원")
print(f"  운용 코인: BTC(10배) / ETH(5배) / XRP(5배)  |  손절 마진비율 {STOP_MARGIN_RATIO*100:.0f}%")
print(f"  손절 기준: BTC +{(1-STOP_MARGIN_RATIO)/10*100:.0f}%  /  ETH·XRP +{(1-STOP_MARGIN_RATIO)/5*100:.0f}%")
print("=" * 72)

all_results  = {}
monthly_coin = {}   # coin → {month → net}

for coin, cfg in COIN_PARAMS.items():
    df = pd.read_csv(cfg["file"], parse_dates=["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)
    df["usd_krw"] = df["usd_krw"].ffill()
    df["ts"]      = df["datetime"].astype("int64") // 10**9

    df25 = df[(df["datetime"] >= "2025-01-01") & (df["datetime"] < "2026-01-01")].copy()
    cpg  = UPBIT_PER_COIN / cfg["n"]

    tr = run_backtest(
        df25["premium_pct"].values,
        df25["upbit_close"].values,
        df25["binance_close"].values,
        df25["usd_krw"].values,
        df25["ts"].values.astype(float),
        cfg["n"], cfg["bottom"], cfg["top"], cpg, cfg["leverage"],
    )

    n_forced = tr["forced"].sum() if len(tr) > 0 else 0
    n_total  = len(tr)
    total    = tr["net"].sum() if n_total > 0 else 0
    wins     = (tr["net"] > 0).sum() if n_total > 0 else 0

    all_results[coin] = {"tr": tr, "total": total, "n": n_total,
                         "n_forced": n_forced, "wins": wins}

    # 월별 집계
    monthly_coin[coin] = {}
    if n_total > 0:
        for m, sub in tr.groupby("month"):
            monthly_coin[coin][m] = sub["net"].sum()

# ── 월별 테이블 출력 ──────────────────────────────────────────────
print(f"\n{'월':>4} | {'BTC':>10} | {'ETH':>10} | {'XRP':>10} | {'합계':>10} | {'수익률':>7}")
print("-" * 66)

monthly_totals = {}
for m in range(1, 13):
    btc = monthly_coin["BTC"].get(m, 0)
    eth = monthly_coin["ETH"].get(m, 0)
    xrp = monthly_coin["XRP"].get(m, 0)
    tot = btc + eth + xrp
    ret = tot / TOTAL_CAPITAL * 100
    monthly_totals[m] = tot
    tag = " ▲" if tot > 0 else (" ▼" if tot < 0 else "")
    print(f" {MONTHS_KR[m-1]:>3} | {btc/10000:>+9.2f}만 | {eth/10000:>+9.2f}만 | "
          f"{xrp/10000:>+9.2f}만 | {tot/10000:>+9.2f}만 | {ret:>+6.2f}%{tag}")

print("-" * 66)
grand_total = sum(monthly_totals.values())
grand_ret   = grand_total / TOTAL_CAPITAL * 100
print(f" {'연간':>3} | {all_results['BTC']['total']/10000:>+9.2f}만 | "
      f"{all_results['ETH']['total']/10000:>+9.2f}만 | "
      f"{all_results['XRP']['total']/10000:>+9.2f}만 | "
      f"{grand_total/10000:>+9.2f}만 | {grand_ret:>+6.2f}%")

# ── 코인별 요약 ───────────────────────────────────────────────────
print(f"\n{'코인':>4} | {'거래':>5} | {'승률':>6} | {'손절':>5} | {'총수익':>10} | {'연수익률':>8}")
print("-" * 55)
for coin, r in all_results.items():
    wins    = r["wins"]
    n       = r["n"]
    wr      = wins / n * 100 if n > 0 else 0
    ret_ann = r["total"] / TOTAL_PER_COIN * 100
    print(f" {coin:>4} | {n:>5} | {wr:>5.1f}% | {r['n_forced']:>4}건 | "
          f"{r['total']/10000:>+9.2f}만 | {ret_ann:>+7.2f}%")

# 손절 상세
for coin, r in all_results.items():
    tr = r["tr"]
    if r["n_forced"] > 0:
        forced_tr = tr[tr["forced"]]
        print(f"\n  [{coin}] 손절 발생 {r['n_forced']}건:")
        for _, row in forced_tr.iterrows():
            print(f"    {row['exit_dt'].strftime('%m/%d')}  "
                  f"레벨 {row['level']:+.1f}%  "
                  f"진입프리 {row['entry_p']:+.2f}%  "
                  f"마진잔여 {row['margin_rt']*100:.1f}%  "
                  f"손실 {row['net']:+,.0f}원")

# ── 시각화 ────────────────────────────────────────────────────────
fig = plt.figure(figsize=(20, 14))
gs  = gridspec.GridSpec(3, 3, figure=fig, hspace=0.5, wspace=0.35)
colors = {"BTC": "#f39c12", "ETH": "#3498db", "XRP": "#27ae60"}
month_labels = [m[:2] for m in MONTHS_KR]

# (1) 월별 합산 수익 막대
ax1 = fig.add_subplot(gs[0, :2])
x   = np.arange(12)
bar_colors = ["#e74c3c" if v < 0 else "#27ae60" for v in monthly_totals.values()]
bars = ax1.bar(x, [monthly_totals[m]/10000 for m in range(1,13)],
               color=bar_colors, edgecolor="white", width=0.6)
for bar, m in zip(bars, range(1,13)):
    h = monthly_totals[m] / 10000
    ax1.text(bar.get_x() + bar.get_width()/2, h + (0.002 if h >= 0 else -0.008),
             f"{h:+.2f}", ha="center", va="bottom" if h >= 0 else "top",
             fontsize=8, fontweight="bold")
ax1.axhline(0, color="black", linewidth=0.8)
ax1.set_xticks(x); ax1.set_xticklabels(month_labels)
ax1.set_ylabel("월 순수익 (만원)")
ax1.set_title(f"2025년 월별 순수익 (3코인 합산)  |  연간 {grand_total/10000:+.2f}만원 ({grand_ret:+.2f}%)",
              fontsize=12, fontweight="bold")
ax1.grid(axis="y", alpha=0.3)

# (2) 코인별 월별 수익 라인
ax2 = fig.add_subplot(gs[0, 2])
for coin in ["BTC", "ETH", "XRP"]:
    vals = [monthly_coin[coin].get(m, 0) / 10000 for m in range(1, 13)]
    ax2.plot(range(12), vals, marker="o", markersize=4,
             color=colors[coin], linewidth=1.5, label=coin)
ax2.axhline(0, color="black", linewidth=0.8)
ax2.set_xticks(range(12)); ax2.set_xticklabels(month_labels, fontsize=8)
ax2.set_ylabel("월 순수익 (만원)")
ax2.set_title("코인별 월별 수익")
ax2.legend(fontsize=9); ax2.grid(alpha=0.3)

# (3) 누적 수익 (일별)
ax3 = fig.add_subplot(gs[1, :2])
all_trades = []
for coin, r in all_results.items():
    if len(r["tr"]) > 0:
        t = r["tr"].copy(); t["coin"] = coin
        all_trades.append(t)
if all_trades:
    all_df = pd.concat(all_trades).sort_values("exit_dt")
    cum    = all_df["net"].cumsum() / 10000
    # 코인별 누적
    for coin in ["BTC", "ETH", "XRP"]:
        sub = all_df[all_df["coin"] == coin].copy()
        if len(sub):
            ax3.plot(sub["exit_dt"], sub["net"].cumsum() / 10000,
                     color=colors[coin], linewidth=1.5, alpha=0.7, label=coin)
    ax3.plot(all_df["exit_dt"], cum, color="black", linewidth=2.0, label="합산")
    # 손절 표시
    stop_trades = all_df[all_df["forced"]]
    if len(stop_trades):
        stop_cum = all_df.set_index("exit_dt")["net"].cumsum()
        for _, row in stop_trades.iterrows():
            idx = all_df[all_df["exit_dt"] == row["exit_dt"]].index[0]
            cum_val = all_df.iloc[:idx+1]["net"].sum() / 10000
            ax3.scatter(row["exit_dt"], cum_val, color="red", s=80, zorder=5,
                       marker="v", label="_")
    ax3.axhline(0, color="black", linewidth=0.8)
    ax3.set_title("2025년 누적 수익  (빨간▼ = 손절 발생)", fontsize=11)
    ax3.set_ylabel("누적 순수익 (만원)")
    ax3.legend(fontsize=9)
    ax3.xaxis.set_major_formatter(plt.matplotlib.dates.DateFormatter("%m월"))
    ax3.xaxis.set_major_locator(plt.matplotlib.dates.MonthLocator())
    ax3.grid(axis="y", alpha=0.3)

# (4) 월별 거래 수 + 손절 수
ax4 = fig.add_subplot(gs[1, 2])
if all_trades:
    all_df2 = pd.concat(all_trades)
    monthly_n      = all_df2.groupby("month").size()
    monthly_forced = all_df2[all_df2["forced"]].groupby("month").size()
    x4 = np.arange(12)
    ax4.bar(x4, [monthly_n.get(m, 0) for m in range(1,13)],
            color="#95a5a6", alpha=0.8, label="일반 청산")
    ax4.bar(x4, [monthly_forced.get(m, 0) for m in range(1,13)],
            color="#e74c3c", alpha=0.9, label="손절 청산")
    ax4.set_xticks(x4); ax4.set_xticklabels(month_labels, fontsize=8)
    ax4.set_ylabel("거래 수")
    ax4.set_title("월별 거래 수 (일반 vs 손절)")
    ax4.legend(fontsize=9)

# (5) 프리미엄 분포 + 월별 흐름
ax5 = fig.add_subplot(gs[2, :])
df_btc = pd.read_csv("data/kimp_btc_hourly.csv", parse_dates=["datetime"])
df25   = df_btc[(df_btc["datetime"] >= "2025-01-01") & (df_btc["datetime"] < "2026-01-01")]
ax5.plot(df25["datetime"], df25["premium_pct"], color="#95a5a6",
         linewidth=0.5, alpha=0.7, label="BTC 프리미엄")
ax5.axhline(0, color="black", linewidth=0.8)
# 그리드 레벨 표시
for lv in np.arange(-2.0, 2.01, 0.5):
    ax5.axhline(lv, color="#e74c3c", linewidth=0.4, linestyle="--", alpha=0.4)
# 손절 발생 시점
if all_trades:
    for _, row in all_df[all_df["forced"]].iterrows():
        ax5.axvline(row["exit_dt"], color="red", linewidth=1.0, alpha=0.6)
ax5.set_ylabel("김치 프리미엄 (%)")
ax5.set_title("2025년 BTC 프리미엄 흐름  (빨간 점선=그리드 레벨  빨간 세로선=손절 발생)")
ax5.xaxis.set_major_formatter(plt.matplotlib.dates.DateFormatter("%m월"))
ax5.xaxis.set_major_locator(plt.matplotlib.dates.MonthLocator())
ax5.set_ylim(-6, 14); ax5.grid(axis="y", alpha=0.2)

fig.suptitle(
    f"2025년 연간 백테스트  |  BTC(10배)/ETH(5배)/XRP(5배)  |  코인당 {UPBIT_PER_COIN//10000}만원  |  "
    f"손절 마진{STOP_MARGIN_RATIO*100:.0f}%  |  연간수익 {grand_total/10000:+.2f}만원 ({grand_ret:+.2f}%)",
    fontsize=12, fontweight="bold"
)

out = "data/backtest_2025.png"
plt.savefig(out, dpi=150, bbox_inches="tight")
plt.close()
print(f"\n  저장: {out}")
