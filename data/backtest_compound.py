"""
복리 백테스팅
  진입: 프리미엄 ≤ 0%  |  청산: 프리미엄 ≥ 2%
  초기 자본: 업비트 9,000만원 + 비트겟 1,000만원 = 1억원
  운용 방식: 매 거래마다 업비트 잔액 전액 사용 (복리)
  레버리지: 업비트잔액 / 비트겟잔액 ≈ 9배
  손절: 없음 (프리미엄 2% 될 때까지 무조건 보유)
  슬리피지: 편도 0.05% (양 거래소 합산)
  펀딩비:   +0.01% / 8시간 (숏 수취 기준)
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
INIT_UPBIT   = 90_000_000    # 초기 업비트 자본
INIT_BITGET  = 10_000_000    # 초기 비트겟 자본
INIT_TOTAL   = INIT_UPBIT + INIT_BITGET   # 1억
UPBIT_RATIO  = 0.9           # 업비트 비중

ENTRY_THRESH = 0.0
EXIT_THRESH  = 2.0

UPBIT_FEE    = 0.0005        # 편도 수수료
BITGET_FEE   = 0.0006
SLIP_RATE    = 0.0005        # 편도 슬리피지 (0.05%)
FUND_PER_8H  = 0.0001        # 펀딩비 +0.01%/8h (숏 수취)


# ── 백테스트 ─────────────────────────────────────────────────────
def run_backtest(df: pd.DataFrame):
    prem = df["premium_pct"].values
    u_px = df["upbit_close"].values
    b_px = df["binance_close"].values
    fx   = df["usd_krw"].values
    ts   = df["ts"].values.astype(float)
    dts  = df["datetime"].values

    upbit_bal  = float(INIT_UPBIT)
    bitget_bal = float(INIT_BITGET)
    total_bal  = upbit_bal + bitget_bal

    trades     = []
    equity_dt  = []
    equity_val = []
    pos        = None
    n          = len(prem)

    for i in range(n):
        # 미결 포지션 손익 추적
        if pos is not None:
            u_pnl  = pos["btc_qty"]   * (u_px[i] - pos["u_entry"])
            b_pnl  = pos["short_qty"] * (pos["b_entry"] - b_px[i]) * fx[i]
            equity_dt.append(dts[i])
            equity_val.append(total_bal + u_pnl + b_pnl)

            # ── 청산 ─────────────────────────────────────────
            if prem[i] >= EXIT_THRESH:
                hold_h = (ts[i] - pos["entry_ts"]) / 3600

                slip   = pos["invest_krw"] * SLIP_RATE * 4      # 진입+청산 × 양거래소
                fee    = pos["invest_krw"] * (UPBIT_FEE + BITGET_FEE) * 2
                fund   = (hold_h / 8) * FUND_PER_8H * pos["invest_krw"]

                gross  = u_pnl + b_pnl
                net    = gross - slip - fee + fund
                total_bal += net
                upbit_bal  = total_bal * UPBIT_RATIO
                bitget_bal = total_bal * (1 - UPBIT_RATIO)

                trades.append(dict(
                    entry_dt    = pd.Timestamp(pos["entry_dt"]),
                    exit_dt     = pd.Timestamp(dts[i]),
                    hold_hours  = hold_h,
                    entry_prem  = pos["entry_prem"],
                    exit_prem   = prem[i],
                    invest_krw  = pos["invest_krw"],
                    gross_pnl   = gross,
                    slip        = slip,
                    fee         = fee,
                    fund        = fund,
                    net_pnl     = net,
                    net_pct     = net / pos["invest_krw"] * 100,
                    total_after = total_bal,
                    min_prem    = pos["min_prem"],
                ))
                pos = None
            else:
                pos["min_prem"] = min(pos["min_prem"], prem[i])

        else:
            equity_dt.append(dts[i])
            equity_val.append(total_bal)

        # ── 진입 ─────────────────────────────────────────────
        if pos is None and prem[i] <= ENTRY_THRESH:
            invest_krw = upbit_bal   # 업비트 전액
            pos = dict(
                entry_dt   = dts[i],
                entry_ts   = ts[i],
                entry_prem = prem[i],
                invest_krw = invest_krw,
                u_entry    = u_px[i],
                b_entry    = b_px[i],
                btc_qty    = invest_krw / u_px[i],
                short_qty  = invest_krw / (b_px[i] * fx[i]),
                min_prem   = prem[i],
            )

    # 미청산 포지션
    open_pos = None
    if pos is not None:
        last = df.iloc[-1]
        u_pnl = pos["btc_qty"]   * (last["upbit_close"] - pos["u_entry"])
        b_pnl = pos["short_qty"] * (pos["b_entry"] - last["binance_close"]) * last["usd_krw"]
        open_pos = dict(
            entry_dt   = pos["entry_dt"],
            entry_prem = pos["entry_prem"],
            unrealized = u_pnl + b_pnl,
            min_prem   = pos["min_prem"],
        )

    trades_df = pd.DataFrame(trades)
    equity_df = pd.DataFrame({"datetime": equity_dt, "equity": equity_val})
    equity_df["datetime"] = pd.to_datetime(equity_df["datetime"])
    return trades_df, equity_df, open_pos


# ── 통계 출력 ─────────────────────────────────────────────────────
def print_stats(trades: pd.DataFrame, equity_df: pd.DataFrame, open_pos):
    start = equity_df["datetime"].min()
    end   = equity_df["datetime"].max()
    years = (end - start).days / 365.25

    final = equity_df["equity"].iloc[-1]
    cagr  = ((final / INIT_TOTAL) ** (1 / years) - 1) * 100

    wins  = trades[trades["net_pnl"] > 0]
    max_dd_prem = trades["min_prem"].min()

    print("\n" + "=" * 65)
    print(f"  백테스팅 결과  |  진입≤{ENTRY_THRESH}%  청산≥{EXIT_THRESH}%  (복리, 손절 없음)")
    print(f"  초기 자본: {INIT_TOTAL/1e8:.0f}억원  (업비트 {INIT_UPBIT/1e4:.0f}만 + 비트겟 {INIT_BITGET/1e4:.0f}만)")
    print(f"  분석 기간: {start.date()} ~ {end.date()}  ({years:.1f}년)")
    print("=" * 65)
    print(f"  총 거래 수:         {len(trades)}회")
    print(f"  승률:               {len(wins)/len(trades)*100:.1f}%  ({len(wins)}익절 / {len(trades)-len(wins)}미달)")
    print(f"  초기 자본:          {INIT_TOTAL/1e8:.2f}억원")
    print(f"  최종 자본:          {final/1e8:.4f}억원  ({final/1e4:,.0f}만원)")
    print(f"  총 순수익:          {(final-INIT_TOTAL)/1e4:+,.0f}만원  ({(final/INIT_TOTAL-1)*100:+.1f}%)")
    print(f"  CAGR (연평균 복리): {cagr:+.2f}%")
    print(f"  평균 보유 시간:     {trades['hold_hours'].mean():.0f}h  (중앙값 {trades['hold_hours'].median():.0f}h, {trades['hold_hours'].median()/24:.0f}일)")
    print(f"  최대 단일 수익:     {trades['net_pnl'].max()/1e4:+.1f}만원")
    print(f"  최대 단일 손실:     {trades['net_pnl'].min()/1e4:+.1f}만원")
    print(f"  보유 중 최저 프리미엄: {max_dd_prem:.2f}%  (최악 역프리미엄)")
    print(f"  총 수수료:          {trades['fee'].sum()/1e4:.1f}만원")
    print(f"  총 슬리피지:        {trades['slip'].sum()/1e4:.1f}만원")
    print(f"  총 펀딩비 수취:     {trades['fund'].sum()/1e4:.1f}만원")

    print("\n  [ 연도별 수익 ]")
    trades["year"] = trades["exit_dt"].dt.year
    for yr, grp in trades.groupby("year"):
        c = ((1 + grp["net_pnl"].sum() / grp["invest_krw"].mean()) ** (12/len(grp)) - 1) * 100
        print(f"    {yr}: {grp['net_pnl'].sum()/1e4:+.1f}만원  ({len(grp)}회 | 승률 {(grp['net_pnl']>0).mean()*100:.0f}%)")

    if open_pos:
        print(f"\n  ⚠ 미청산 포지션: {pd.Timestamp(open_pos['entry_dt']).date()} 진입 "
              f"(프리미엄 {open_pos['entry_prem']:.2f}%, 최저 {open_pos['min_prem']:.2f}%)")
        print(f"    미실현 손익: {open_pos['unrealized']/1e4:+.1f}만원")
    print("=" * 65)
    return cagr, final


# ── 시각화 ────────────────────────────────────────────────────────
def plot(trades: pd.DataFrame, equity_df: pd.DataFrame, cagr: float, final: float):
    fig = plt.figure(figsize=(20, 20))
    gs  = gridspec.GridSpec(4, 2, figure=fig, hspace=0.48, wspace=0.30)

    years = (equity_df["datetime"].max() - equity_df["datetime"].min()).days / 365.25

    # (1) 복리 자산 곡선
    ax1 = fig.add_subplot(gs[0, :])
    ax1.plot(equity_df["datetime"], equity_df["equity"] / 1e4,
             color="#2c3e50", linewidth=0.7)
    ax1.fill_between(equity_df["datetime"], equity_df["equity"] / 1e4,
                     INIT_TOTAL / 1e4,
                     where=equity_df["equity"] >= INIT_TOTAL,
                     alpha=0.20, color="#27ae60")
    ax1.fill_between(equity_df["datetime"], equity_df["equity"] / 1e4,
                     INIT_TOTAL / 1e4,
                     where=equity_df["equity"] < INIT_TOTAL,
                     alpha=0.25, color="#e74c3c")
    ax1.axhline(INIT_TOTAL / 1e4, color="black", linewidth=1, linestyle="--",
                label=f"원금 {INIT_TOTAL/1e4:,.0f}만원")

    # 익절 마커
    for _, t in trades.iterrows():
        ax1.axvline(t["exit_dt"], color="#27ae60", alpha=0.15, linewidth=0.4)

    ax1.set_title(
        f"복리 자산 곡선  |  진입≤{ENTRY_THRESH}%  청산≥{EXIT_THRESH}%  레버리지≈9배  손절없음\n"
        f"총 {len(trades)}회  |  승률 {(trades['net_pnl']>0).mean()*100:.1f}%  |  "
        f"최종 {final/1e4:,.0f}만원  |  CAGR {cagr:+.2f}%",
        fontsize=12, fontweight="bold"
    )
    ax1.set_ylabel("자산 (만원)")
    ax1.legend(fontsize=9)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax1.xaxis.set_major_locator(mdates.YearLocator())
    ax1.grid(axis="y", alpha=0.3)

    # (2) 프리미엄 시계열 + 진입/청산
    ax2 = fig.add_subplot(gs[1, :])
    df_raw = pd.read_csv("data/kimp_history_hourly.csv", parse_dates=["datetime"])
    ax2.plot(df_raw["datetime"], df_raw["premium_pct"],
             color="#bdc3c7", linewidth=0.3, alpha=0.8)
    ax2.axhline(ENTRY_THRESH, color="#3498db", linewidth=1.5, linestyle="--",
                label=f"진입 {ENTRY_THRESH}%")
    ax2.axhline(EXIT_THRESH,  color="#e74c3c", linewidth=1.5, linestyle="--",
                label=f"청산 {EXIT_THRESH}%")
    ax2.axhline(0, color="black", linewidth=0.8)
    for _, t in trades.iterrows():
        ax2.axvline(t["entry_dt"], color="#3498db", alpha=0.3, linewidth=0.5)
        ax2.axvline(t["exit_dt"],  color="#e74c3c", alpha=0.3, linewidth=0.5)
    ax2.set_title("김치 프리미엄 시계열  (파랑=진입, 빨강=청산)")
    ax2.set_ylabel("프리미엄 (%)")
    ax2.set_ylim(-15, 20); ax2.legend(fontsize=9)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax2.xaxis.set_major_locator(mdates.YearLocator())
    ax2.grid(axis="y", alpha=0.3)

    # (3) 거래별 순손익
    ax3 = fig.add_subplot(gs[2, 0])
    colors = ["#27ae60" if p > 0 else "#e74c3c" for p in trades["net_pnl"]]
    ax3.bar(range(len(trades)), trades["net_pnl"] / 1e4, color=colors, alpha=0.75, width=0.8)
    ax3.axhline(0, color="black", linewidth=0.8)
    ax3.set_title("거래별 순손익 (만원)")
    ax3.set_xlabel("거래 번호"); ax3.set_ylabel("순손익 (만원)")
    ax3.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax3.grid(axis="y", alpha=0.3)

    # (4) 보유 시간 분포
    ax4 = fig.add_subplot(gs[2, 1])
    ax4.hist(trades["hold_hours"], bins=40, color="#9b59b6",
             edgecolor="white", linewidth=0.3, alpha=0.8)
    med = trades["hold_hours"].median()
    ax4.axvline(med, color="orange", linewidth=2, linestyle="--",
                label=f"중앙값 {med:.0f}h ({med/24:.0f}일)")
    ax4.set_title("포지션 보유 시간 분포")
    ax4.set_xlabel("보유 시간 (h)"); ax4.set_ylabel("거래 수")
    ax4.legend(); ax4.grid(axis="y", alpha=0.3)

    # (5) 연도별 순손익
    ax5 = fig.add_subplot(gs[3, 0])
    yearly = trades.groupby("year")["net_pnl"].sum() / 1e4
    colors_y = ["#27ae60" if v >= 0 else "#e74c3c" for v in yearly.values]
    bars = ax5.bar(yearly.index, yearly.values, color=colors_y, alpha=0.75, edgecolor="white")
    for bar, val in zip(bars, yearly.values):
        ax5.text(bar.get_x() + bar.get_width() / 2,
                 val + 5 if val >= 0 else val - 10,
                 f"{val:,.0f}", ha="center", fontsize=8)
    ax5.axhline(0, color="black", linewidth=0.8)
    ax5.set_title("연도별 순손익 (만원)")
    ax5.set_xlabel("연도"); ax5.set_ylabel("순손익 (만원)")
    ax5.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax5.grid(axis="y", alpha=0.3)

    # (6) 비용 구성
    ax6 = fig.add_subplot(gs[3, 1])
    total_gross = trades["gross_pnl"].sum()
    total_fee   = trades["fee"].sum()
    total_slip  = trades["slip"].sum()
    total_fund  = trades["fund"].sum()
    total_net   = trades["net_pnl"].sum()

    items  = ["프리미엄 차익", "펀딩비 수취", "수수료", "슬리피지"]
    values = [total_gross/1e4, total_fund/1e4, -total_fee/1e4, -total_slip/1e4]
    colors_b = ["#27ae60", "#3498db", "#e74c3c", "#e67e22"]
    bars2 = ax6.bar(items, values, color=colors_b, alpha=0.75, edgecolor="white")
    for bar, val in zip(bars2, values):
        ax6.text(bar.get_x() + bar.get_width() / 2,
                 val + 2 if val >= 0 else val - 5,
                 f"{val:+,.0f}", ha="center", fontsize=9, fontweight="bold")
    ax6.axhline(0, color="black", linewidth=0.8)
    ax6.set_title(f"수익/비용 구성 (합계 {total_net/1e4:+,.0f}만원)")
    ax6.set_ylabel("금액 (만원)")
    ax6.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax6.grid(axis="y", alpha=0.3)

    fig.suptitle(
        f"복리 백테스팅  |  1억원 (업비트 9천만 + 비트겟 1천만)  |  "
        f"진입≤{ENTRY_THRESH}%  청산≥{EXIT_THRESH}%  |  손절없음  |  슬리피지+펀딩비 포함",
        fontsize=13, fontweight="bold", y=1.005
    )
    out = "data/backtest_compound.png"
    plt.savefig(out, dpi=150, bbox_inches="tight"); plt.close()
    print(f"\n  저장: {out}")


# ── 메인 ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    df = pd.read_csv("data/kimp_history_hourly.csv", parse_dates=["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)
    df["usd_krw"] = df["usd_krw"].ffill()
    df["ts"]      = df["datetime"].astype("int64") // 10**9

    trades, equity_df, open_pos = run_backtest(df)
    cagr, final = print_stats(trades, equity_df, open_pos)
    plot(trades, equity_df, cagr, final)
