"""
김치 프리미엄 전략 백테스팅 (1시간봉 기준)

자본 구조:
  - 업비트: 100만원 (현물 롱)
  - 비트겟: 50만원 (숏 증거금, 최대 5배)
  - 포지션 진입 시 롱/숏 명목가치 동일

손절: 없음 (프리미엄 회복 대기)
임계값: 진입 ≤ 1.0% / 청산 ≥ 3.0%
"""

import matplotlib
matplotlib.use("Agg")
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.dates as mdates
import platform

if platform.system() == "Darwin":
    plt.rcParams["font.family"] = "AppleGothic"
plt.rcParams["axes.unicode_minus"] = False

# ── 파라미터 ─────────────────────────────────────────────────────
UPBIT_KRW         = 1_000_000   # 업비트 투자금
BITGET_MARGIN_KRW = 500_000     # 비트겟 증거금 (KRW 환산)
TOTAL_CAPITAL     = UPBIT_KRW + BITGET_MARGIN_KRW  # 150만원
MAX_LEVERAGE      = 5

ENTRY_THRESH = 1.0
EXIT_THRESH  = 3.0

UPBIT_FEE  = 0.0005   # 편도 0.05%
BITGET_FEE = 0.0006   # 편도 0.06%


# ── 백테스트 실행 ─────────────────────────────────────────────────
def run_backtest(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    trades   = []
    equity   = []   # (datetime, unrealized_capital)
    pos      = None
    capital  = float(TOTAL_CAPITAL)

    for _, row in df.iterrows():
        prem = row["premium_pct"]
        u_px = row["upbit_close"]
        b_px = row["binance_close"]
        fx   = row["usd_krw"]
        dt   = row["datetime"]

        # ── 미결 포지션 손익 ────────────────────────────────────
        if pos is not None:
            upbit_pnl  = pos["btc_qty"] * (u_px - pos["u_entry"])
            bitget_pnl = pos["short_qty"] * (pos["b_entry"] - b_px) * fx
            unrealized = upbit_pnl + bitget_pnl
            equity.append((dt, capital + unrealized))

            # ── 청산 조건: 프리미엄 ≥ EXIT_THRESH ────────────
            if prem >= EXIT_THRESH:
                fee_exit = (UPBIT_KRW * UPBIT_FEE +
                            (UPBIT_KRW / fx) * BITGET_FEE)
                net_pnl  = unrealized - pos["fee_entry"] - fee_exit
                capital += net_pnl

                hold_h = int((dt - pos["entry_dt"]).total_seconds() / 3600)
                trades.append(dict(
                    entry_dt     = pos["entry_dt"],
                    exit_dt      = dt,
                    hold_hours   = hold_h,
                    entry_prem   = pos["entry_prem"],
                    exit_prem    = prem,
                    prem_change  = prem - pos["entry_prem"],
                    upbit_pnl    = round(upbit_pnl),
                    bitget_pnl   = round(bitget_pnl),
                    fee          = round(pos["fee_entry"] + fee_exit),
                    net_pnl      = round(net_pnl),
                    net_pct      = net_pnl / TOTAL_CAPITAL * 100,
                    leverage_used= pos["leverage_used"],
                    max_unreal   = pos["max_unreal"],
                    min_unreal   = pos["min_unreal"],
                    status       = "익절",
                ))
                pos = None
            else:
                pos["max_unreal"] = max(pos["max_unreal"], unrealized)
                pos["min_unreal"] = min(pos["min_unreal"], unrealized)
        else:
            equity.append((dt, capital))

        # ── 진입 조건: 포지션 없고 프리미엄 ≤ ENTRY_THRESH ───
        if pos is None and prem <= ENTRY_THRESH:
            # 롱 수량 (업비트)
            btc_qty   = UPBIT_KRW / u_px
            # 숏 명목가치 = 업비트 투자금과 동일하게 맞춤
            # short_qty * b_px * fx = UPBIT_KRW
            short_qty = UPBIT_KRW / (b_px * fx)
            # 실제 사용 레버리지 = 숏 명목가치 / 증거금
            lev_used  = round(UPBIT_KRW / BITGET_MARGIN_KRW, 2)  # = 2.0x 고정

            fee_entry = (UPBIT_KRW * UPBIT_FEE +
                         (UPBIT_KRW / fx) * BITGET_FEE)
            pos = dict(
                entry_dt     = dt,
                entry_prem   = prem,
                u_entry      = u_px,
                b_entry      = b_px,
                fx_entry     = fx,
                btc_qty      = btc_qty,
                short_qty    = short_qty,
                fee_entry    = fee_entry,
                leverage_used= lev_used,
                max_unreal   = 0.0,
                min_unreal   = 0.0,
            )

    # 기간 종료 시 미청산 포지션 처리
    open_pos = None
    if pos is not None:
        last = df.iloc[-1]
        upbit_pnl  = pos["btc_qty"] * (last["upbit_close"] - pos["u_entry"])
        bitget_pnl = pos["short_qty"] * (pos["b_entry"] - last["binance_close"]) * last["usd_krw"]
        unrealized = upbit_pnl + bitget_pnl
        open_pos = dict(
            entry_dt   = pos["entry_dt"],
            entry_prem = pos["entry_prem"],
            unrealized = round(unrealized),
        )

    trades_df  = pd.DataFrame(trades)
    equity_df  = pd.DataFrame(equity, columns=["datetime", "equity"])
    return trades_df, equity_df, open_pos


# ── 성과 요약 ─────────────────────────────────────────────────────
def print_summary(trades: pd.DataFrame, open_pos: dict):
    print("\n" + "=" * 60)
    print(f"  백테스팅 결과  |  진입≤{ENTRY_THRESH}%  청산≥{EXIT_THRESH}%")
    print(f"  자본: 업비트 {UPBIT_KRW//10000}만원 + 비트겟 {BITGET_MARGIN_KRW//10000}만원 = {TOTAL_CAPITAL//10000}만원")
    print(f"  레버리지: {UPBIT_KRW//BITGET_MARGIN_KRW}배 (최대 {MAX_LEVERAGE}배)")
    print("=" * 60)

    if trades.empty:
        print("  거래 없음")
        return

    wins  = trades[trades["net_pnl"] > 0]
    total_net = trades["net_pnl"].sum()

    print(f"  총 거래 수:        {len(trades)}회")
    print(f"  승률:              {len(wins)/len(trades)*100:.1f}%  ({len(wins)}익절 / {len(trades)-len(wins)}미달)")
    print(f"  총 순수익:         {total_net/10000:+.2f}만원  ({total_net/TOTAL_CAPITAL*100:+.1f}%)")
    print(f"  평균 거래 수익:    {trades['net_pct'].mean():+.2f}%  ({trades['net_pnl'].mean()/10000:+.2f}만원)")
    print(f"  평균 보유 시간:    {trades['hold_hours'].mean():.0f}h  ({trades['hold_hours'].median():.0f}h 중앙값)")
    print(f"  최대 단일 수익:    {trades['net_pnl'].max()/10000:+.2f}만원")
    print(f"  최대 단일 손실:    {trades['net_pnl'].min()/10000:+.2f}만원")
    print(f"  보유 중 최대 미실현손실: {trades['min_unreal'].min()/10000:.2f}만원")
    print(f"  총 수수료:         {trades['fee'].sum()/10000:.2f}만원")

    print("\n  [ 연도별 수익 ]")
    trades["year"] = pd.to_datetime(trades["exit_dt"]).dt.year
    for yr, grp in trades.groupby("year"):
        print(f"    {yr}: {grp['net_pnl'].sum()/10000:+.1f}만원  ({len(grp)}회)")

    if open_pos:
        print(f"\n  ⚠ 미청산 포지션: {open_pos['entry_dt']} 진입 (프리미엄 {open_pos['entry_prem']:.2f}%)")
        print(f"    현재 미실현 손익: {open_pos['unrealized']/10000:+.2f}만원")
    print("=" * 60)


# ── 시각화 ─────────────────────────────────────────────────────────
def plot_result(trades: pd.DataFrame, equity_df: pd.DataFrame, df_raw: pd.DataFrame):
    fig = plt.figure(figsize=(20, 20))
    gs  = gridspec.GridSpec(4, 2, figure=fig, hspace=0.50, wspace=0.30)

    # (1) 누적 자산 곡선
    ax1 = fig.add_subplot(gs[0, :])
    eq = equity_df.copy()
    eq["pnl"] = eq["equity"] - TOTAL_CAPITAL

    ax1.plot(eq["datetime"], eq["pnl"] / 10000, color="#2c3e50", linewidth=0.6)
    ax1.fill_between(eq["datetime"], eq["pnl"] / 10000, 0,
                     where=eq["pnl"] >= 0, alpha=0.20, color="#27ae60")
    ax1.fill_between(eq["datetime"], eq["pnl"] / 10000, 0,
                     where=eq["pnl"] < 0,  alpha=0.25, color="#e74c3c")

    # 익절 시점 마커
    for _, t in trades.iterrows():
        ax1.axvline(pd.to_datetime(t["exit_dt"]), color="#27ae60", alpha=0.25, linewidth=0.5)

    ax1.axhline(0, color="black", linewidth=0.8)
    total_net = trades["net_pnl"].sum()
    ax1.set_title(
        f"누적 손익  |  진입≤{ENTRY_THRESH}%  청산≥{EXIT_THRESH}%  레버리지 {UPBIT_KRW//BITGET_MARGIN_KRW}배\n"
        f"총 {len(trades)}회  |  승률 {len(trades[trades['net_pnl']>0])/len(trades)*100:.1f}%  |  "
        f"총수익 {total_net/10000:+.1f}만원 ({total_net/TOTAL_CAPITAL*100:+.1f}%)  |  손절 없음",
        fontsize=12, fontweight="bold"
    )
    ax1.set_ylabel("손익 (만원)")
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax1.xaxis.set_major_locator(mdates.YearLocator())
    ax1.grid(axis="y", alpha=0.3)

    # (2) 프리미엄 시계열 + 진입/청산 마커
    ax2 = fig.add_subplot(gs[1, :])
    ax2.plot(df_raw["datetime"], df_raw["premium_pct"], color="#95a5a6", linewidth=0.4, alpha=0.8)
    ax2.axhline(ENTRY_THRESH, color="#3498db", linewidth=1.5, linestyle="--", label=f"진입 {ENTRY_THRESH}%")
    ax2.axhline(EXIT_THRESH,  color="#e74c3c", linewidth=1.5, linestyle="--", label=f"청산 {EXIT_THRESH}%")
    ax2.axhline(0, color="black", linewidth=0.8)

    for _, t in trades.iterrows():
        ax2.axvline(pd.to_datetime(t["entry_dt"]), color="#3498db", alpha=0.4, linewidth=0.6)
        ax2.axvline(pd.to_datetime(t["exit_dt"]),  color="#e74c3c", alpha=0.4, linewidth=0.6)

    ax2.set_title("김치 프리미엄 시계열  (파랑=진입, 빨강=청산)", fontsize=11)
    ax2.set_ylabel("프리미엄 (%)")
    ax2.legend(fontsize=9)
    ax2.set_ylim(-15, 30)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax2.xaxis.set_major_locator(mdates.YearLocator())
    ax2.grid(axis="y", alpha=0.3)

    # (3) 거래별 순손익 막대
    ax3 = fig.add_subplot(gs[2, 0])
    colors = ["#27ae60" if p > 0 else "#e74c3c" for p in trades["net_pnl"]]
    ax3.bar(range(len(trades)), trades["net_pnl"] / 10000, color=colors, alpha=0.75, width=0.8)
    ax3.axhline(0, color="black", linewidth=0.8)
    ax3.set_title("거래별 순손익 (만원)")
    ax3.set_xlabel("거래 번호"); ax3.set_ylabel("순손익 (만원)")
    ax3.grid(axis="y", alpha=0.3)

    # (4) 수익률 분포
    ax4 = fig.add_subplot(gs[2, 1])
    ax4.hist(trades["net_pct"], bins=30, color="#3498db", edgecolor="white", linewidth=0.3, alpha=0.8)
    ax4.axvline(0, color="black", linewidth=1)
    ax4.axvline(trades["net_pct"].mean(), color="orange", linewidth=2, linestyle="--",
                label=f"평균 {trades['net_pct'].mean():.2f}%")
    ax4.set_title("거래별 수익률 분포 (%)")
    ax4.set_xlabel("수익률 (%)"); ax4.set_ylabel("거래 수")
    ax4.legend(); ax4.grid(axis="y", alpha=0.3)

    # (5) 보유 시간 분포
    ax5 = fig.add_subplot(gs[3, 0])
    ax5.hist(trades["hold_hours"], bins=50, color="#9b59b6", edgecolor="white", linewidth=0.3, alpha=0.8)
    med = trades["hold_hours"].median()
    ax5.axvline(med, color="orange", linewidth=2, linestyle="--", label=f"중앙값 {med:.0f}h ({med/24:.0f}일)")
    ax5.set_title("포지션 보유 시간 분포")
    ax5.set_xlabel("보유 시간 (h)"); ax5.set_ylabel("거래 수")
    ax5.legend(); ax5.grid(axis="y", alpha=0.3)

    # (6) 연도별 순손익
    ax6 = fig.add_subplot(gs[3, 1])
    trades["year"] = pd.to_datetime(trades["exit_dt"]).dt.year
    yearly = trades.groupby("year")["net_pnl"].sum() / 10000
    colors_y = ["#27ae60" if v >= 0 else "#e74c3c" for v in yearly.values]
    bars = ax6.bar(yearly.index, yearly.values, color=colors_y, alpha=0.75, edgecolor="white")
    for bar, val in zip(bars, yearly.values):
        ax6.text(bar.get_x() + bar.get_width() / 2,
                 val + 0.3 if val >= 0 else val - 0.8,
                 f"{val:.1f}", ha="center", fontsize=9)
    ax6.axhline(0, color="black", linewidth=0.8)
    ax6.set_title("연도별 순손익 (만원)")
    ax6.set_xlabel("연도"); ax6.set_ylabel("순손익 (만원)")
    ax6.grid(axis="y", alpha=0.3)

    fig.suptitle(
        f"백테스팅  |  업비트 {UPBIT_KRW//10000}만원 + 비트겟 {BITGET_MARGIN_KRW//10000}만원  |  레버리지 {UPBIT_KRW//BITGET_MARGIN_KRW}배  |  손절 없음",
        fontsize=14, fontweight="bold", y=1.005
    )
    out = "data/backtest_result.png"
    plt.savefig(out, dpi=150, bbox_inches="tight"); plt.close()
    print(f"\n  저장: {out}")


# ── 메인 ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    df = pd.read_csv("data/kimp_history_hourly.csv", parse_dates=["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)
    df["usd_krw"] = df["usd_krw"].ffill()

    print(f"데이터: {df['datetime'].min()} ~ {df['datetime'].max()}  ({len(df):,}개)")
    print(f"진입 조건: 프리미엄 ≤ {ENTRY_THRESH}%  |  청산 조건: 프리미엄 ≥ {EXIT_THRESH}%")
    print(f"자본: 업비트 {UPBIT_KRW//10000}만원 + 비트겟 {BITGET_MARGIN_KRW//10000}만원  |  레버리지 {UPBIT_KRW//BITGET_MARGIN_KRW}배")

    trades, equity_df, open_pos = run_backtest(df)
    print_summary(trades, open_pos)
    plot_result(trades, equity_df, df)
