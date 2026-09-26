"""
김치 프리미엄 시각화 (일봉 / 1시간봉 공용)
실행: python data/visualize.py [1h|1d]
"""

import matplotlib
matplotlib.use("Agg")
import sys, os
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.dates as mdates
import seaborn as sns
import numpy as np
import platform

# ── 한글 폰트 ──────────────────────────────────────────────────
if platform.system() == "Darwin":
    plt.rcParams["font.family"] = "AppleGothic"
else:
    plt.rcParams["font.family"] = "NanumGothic"
plt.rcParams["axes.unicode_minus"] = False

# ── 설정 ───────────────────────────────────────────────────────
interval = sys.argv[1] if len(sys.argv) > 1 else "1h"
assert interval in ("1d", "1h")

label    = "hourly" if interval == "1h" else "daily"
csv_path = f"data/kimp_history_{label}.csv"

ENTRY = 2.0
EXIT  = 4.0

# ── 데이터 로드 ────────────────────────────────────────────────
df = pd.read_csv(csv_path, parse_dates=["datetime"])
df = df.sort_values("datetime").reset_index(drop=True)
df["year"] = df["datetime"].dt.year

unit = "시간봉" if interval == "1h" else "일봉"
print(f"[{unit}] {df['datetime'].min()} ~ {df['datetime'].max()}  ({len(df):,}개)")

# ══════════════════════════════════════════════════════════════
# Figure 1: 종합 분석 (6-panel)
# ══════════════════════════════════════════════════════════════
fig = plt.figure(figsize=(22, 18))
gs  = gridspec.GridSpec(3, 2, figure=fig, hspace=0.50, wspace=0.30)

# ── (1) 전체 시계열 ────────────────────────────────────────────
ax1 = fig.add_subplot(gs[0, :])
ax1.fill_between(df["datetime"], df["premium_pct"], 0,
                 where=df["premium_pct"] >= 0, alpha=0.20, color="#e74c3c")
ax1.fill_between(df["datetime"], df["premium_pct"], 0,
                 where=df["premium_pct"] < 0,  alpha=0.20, color="#3498db")
ax1.plot(df["datetime"], df["premium_pct"], color="#2c3e50", linewidth=0.4, alpha=0.7)
ax1.axhline(ENTRY, color="#27ae60", linewidth=1.5, linestyle="--", label=f"진입 기준 ({ENTRY}%)")
ax1.axhline(EXIT,  color="#e74c3c", linewidth=1.5, linestyle="--", label=f"청산 기준 ({EXIT}%)")
ax1.axhline(0,     color="black",   linewidth=0.8)

peak = df.loc[df["premium_pct"].idxmax()]
ax1.annotate(f"  최대 {peak['premium_pct']:.1f}%\n  ({str(peak['datetime'])[:13]})",
             xy=(peak["datetime"], peak["premium_pct"]),
             xytext=(peak["datetime"] + pd.Timedelta(days=300), peak["premium_pct"] - 10),
             arrowprops=dict(arrowstyle="->", color="gray"), fontsize=9, color="gray")

ax1.set_title(f"김치 프리미엄 전체 시계열 [{unit}] (2017~2026)", fontsize=14, fontweight="bold")
ax1.set_ylabel("프리미엄 (%)")
ax1.legend(loc="upper right", fontsize=9)
ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
ax1.xaxis.set_major_locator(mdates.YearLocator())
ax1.grid(axis="y", alpha=0.3)

# ── (2) 히스토그램 (극단값 제외) ───────────────────────────────
ax2 = fig.add_subplot(gs[1, 0])
df_trim = df[df["premium_pct"].between(-10, 20)]
ax2.hist(df_trim["premium_pct"], bins=120, color="#3498db", edgecolor="white", linewidth=0.2, alpha=0.8)
ax2.axvline(ENTRY, color="#27ae60", linewidth=2, linestyle="--", label=f"진입 {ENTRY}%")
ax2.axvline(EXIT,  color="#e74c3c", linewidth=2, linestyle="--", label=f"청산 {EXIT}%")
ax2.axvline(df["premium_pct"].median(), color="orange", linewidth=1.5, linestyle=":",
            label=f"중앙값 {df['premium_pct'].median():.1f}%")

entry_pct = (df["premium_pct"] <= ENTRY).mean() * 100
exit_pct  = (df["premium_pct"] >= EXIT).mean()  * 100
ax2.set_title(f"프리미엄 분포 [{unit}] (극단 제외)\n진입 가능 {entry_pct:.1f}% | 청산 가능 {exit_pct:.1f}%", fontsize=11)
ax2.set_xlabel("프리미엄 (%)"); ax2.set_ylabel(f"{unit} 수")
ax2.legend(fontsize=9); ax2.grid(axis="y", alpha=0.3)

# ── (3) 연도별 박스플롯 ────────────────────────────────────────
ax3 = fig.add_subplot(gs[1, 1])
years     = sorted(df["year"].unique())
year_data = [df[df["year"] == y]["premium_pct"].clip(-15, 30).values for y in years]
bp = ax3.boxplot(year_data, tick_labels=years, patch_artist=True,
                 medianprops=dict(color="black", linewidth=2),
                 flierprops=dict(marker=".", markersize=2, alpha=0.3))
colors = plt.cm.RdYlGn(np.linspace(0.2, 0.8, len(years)))
for patch, c in zip(bp["boxes"], colors):
    patch.set_facecolor(c); patch.set_alpha(0.7)
ax3.axhline(ENTRY, color="#27ae60", linewidth=1.5, linestyle="--", alpha=0.8)
ax3.axhline(EXIT,  color="#e74c3c", linewidth=1.5, linestyle="--", alpha=0.8)
ax3.axhline(0,     color="black",   linewidth=0.8)
ax3.set_title(f"연도별 김치 프리미엄 분포 [{unit}]", fontsize=11)
ax3.set_ylabel("프리미엄 (%)"); ax3.set_xlabel("연도")
ax3.grid(axis="y", alpha=0.3); ax3.tick_params(axis="x", rotation=45)

# ── (4) 최근 2년 상세 시계열 ────────────────────────────────────
ax4 = fig.add_subplot(gs[2, 0])
cutoff = df["datetime"].max() - pd.Timedelta(days=730)
df_r   = df[df["datetime"] >= cutoff]
ax4.plot(df_r["datetime"], df_r["premium_pct"], color="#2c3e50", linewidth=0.6)
ax4.fill_between(df_r["datetime"], df_r["premium_pct"], 0,
                 where=df_r["premium_pct"] >= 0, alpha=0.2, color="#e74c3c")
ax4.fill_between(df_r["datetime"], df_r["premium_pct"], 0,
                 where=df_r["premium_pct"] < 0,  alpha=0.2, color="#3498db")
ax4.axhline(ENTRY, color="#27ae60", linewidth=1.5, linestyle="--", label=f"진입 {ENTRY}%")
ax4.axhline(EXIT,  color="#e74c3c", linewidth=1.5, linestyle="--", label=f"청산 {EXIT}%")
ax4.axhline(0,     color="black",   linewidth=0.8)
ax4.set_title(f"최근 2년 상세 [{unit}]", fontsize=11)
ax4.set_ylabel("프리미엄 (%)"); ax4.legend(fontsize=9)
ax4.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
ax4.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
ax4.tick_params(axis="x", rotation=30); ax4.grid(axis="y", alpha=0.3)

# ── (5) 시간대별 평균 프리미엄 (시간봉 전용) ───────────────────
ax5 = fig.add_subplot(gs[2, 1])
if interval == "1h":
    df["hour_kst"] = (df["datetime"].dt.hour + 9) % 24  # UTC→KST
    hourly_mean = df.groupby("hour_kst")["premium_pct"].mean()
    colors_h = ["#e74c3c" if v >= 0 else "#3498db" for v in hourly_mean.values]
    ax5.bar(hourly_mean.index, hourly_mean.values, color=colors_h, alpha=0.75, edgecolor="white")
    ax5.axhline(0, color="black", linewidth=0.8)
    ax5.set_title("KST 시간대별 평균 김치 프리미엄", fontsize=11)
    ax5.set_xlabel("시간 (KST)"); ax5.set_ylabel("평균 프리미엄 (%)")
    ax5.set_xticks(range(0, 24, 2))
    ax5.grid(axis="y", alpha=0.3)
else:
    yearly_mean = df.groupby("year")["premium_pct"].mean()
    colors_b = ["#e74c3c" if v >= 0 else "#3498db" for v in yearly_mean.values]
    bars = ax5.bar(yearly_mean.index, yearly_mean.values, color=colors_b, alpha=0.75, edgecolor="white")
    for bar, val in zip(bars, yearly_mean.values):
        ax5.text(bar.get_x() + bar.get_width() / 2,
                 val + 0.3 if val >= 0 else val - 0.8,
                 f"{val:.1f}%", ha="center", fontsize=8)
    ax5.axhline(0, color="black", linewidth=0.8)
    ax5.set_title("연도별 평균 김치 프리미엄", fontsize=11)
    ax5.set_xlabel("연도"); ax5.set_ylabel("평균 프리미엄 (%)")
    ax5.grid(axis="y", alpha=0.3)

fig.suptitle(f"김치 프리미엄 종합 분석 [{unit}]", fontsize=17, fontweight="bold", y=1.01)
out1 = f"data/kimp_analysis_{label}.png"
plt.savefig(out1, dpi=150, bbox_inches="tight"); plt.close()
print(f"저장: {out1}")

# ══════════════════════════════════════════════════════════════
# Figure 2: 임계값 민감도
# ══════════════════════════════════════════════════════════════
fig2, axes = plt.subplots(1, 3, figsize=(18, 6))

entry_vals = np.arange(-1.0, 4.5, 0.5)
exit_vals  = np.arange(2.0, 12.5, 0.5)

entry_pct_arr = np.array([(df["premium_pct"] <= e).mean() * 100 for e in entry_vals])
exit_pct_arr  = np.array([(df["premium_pct"] >= x).mean() * 100 for x in exit_vals])
heatmap_data  = np.outer(exit_pct_arr, entry_pct_arr) / 100

ax = axes[0]
im = ax.imshow(heatmap_data, aspect="auto", origin="lower",
               extent=[entry_vals[0], entry_vals[-1], exit_vals[0], exit_vals[-1]],
               cmap="YlOrRd")
ax.set_xlabel("진입 기준 (%)"); ax.set_ylabel("청산 기준 (%)")
ax.set_title(f"진입×청산 기회 빈도 [{unit}]\n(높을수록 거래 기회 많음)")
plt.colorbar(im, ax=ax)
ax.axvline(ENTRY, color="white", linewidth=2, linestyle="--")
ax.axhline(EXIT,  color="white", linewidth=2, linestyle="--")

axes[1].bar(entry_vals, entry_pct_arr, width=0.4, color="#3498db", alpha=0.8)
axes[1].axvline(ENTRY, color="#27ae60", linewidth=2, linestyle="--", label=f"현재 {ENTRY}%")
for x, y in zip(entry_vals, entry_pct_arr):
    axes[1].text(x, y + 0.5, f"{y:.0f}%", ha="center", fontsize=8)
axes[1].set_xlabel("진입 기준 (%)"); axes[1].set_ylabel("진입 가능 비율 (%)")
axes[1].set_title(f"진입 기준별 가능 비율 [{unit}]")
axes[1].legend(); axes[1].grid(axis="y", alpha=0.3)

axes[2].barh(exit_vals, exit_pct_arr, height=0.4, color="#e74c3c", alpha=0.8)
axes[2].axhline(EXIT, color="#27ae60", linewidth=2, linestyle="--", label=f"현재 {EXIT}%")
for y, x in zip(exit_vals, exit_pct_arr):
    axes[2].text(x + 0.3, y, f"{x:.0f}%", va="center", fontsize=8)
axes[2].set_ylabel("청산 기준 (%)"); axes[2].set_xlabel("청산 가능 비율 (%)")
axes[2].set_title(f"청산 기준별 가능 비율 [{unit}]")
axes[2].legend(); axes[2].grid(axis="x", alpha=0.3)

fig2.suptitle(f"임계값 민감도 분석 [{unit}]", fontsize=15, fontweight="bold")
plt.tight_layout()
out2 = f"data/kimp_threshold_{label}.png"
plt.savefig(out2, dpi=150, bbox_inches="tight"); plt.close()
print(f"저장: {out2}")
