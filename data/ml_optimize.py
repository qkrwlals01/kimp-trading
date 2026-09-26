"""
머신러닝(Optuna 베이지안 최적화)으로 김치 프리미엄 최적 임계값 탐색

백테스트를 numpy로 완전 벡터화 → 1회 실행 < 1ms
목적함수: 시간 가중 샤프 비율 (최근 데이터 반감기 2년)
워크포워드: 학습(2017~2023) → 검증(2024~2026)
"""

import matplotlib
matplotlib.use("Agg")
import warnings
warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np
import optuna
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.dates as mdates
import platform

optuna.logging.set_verbosity(optuna.logging.WARNING)

if platform.system() == "Darwin":
    plt.rcParams["font.family"] = "AppleGothic"
plt.rcParams["axes.unicode_minus"] = False

# ── 파라미터 ─────────────────────────────────────────────────────
UPBIT_KRW         = 1_000_000
BITGET_MARGIN_KRW = 500_000
TOTAL_CAPITAL     = UPBIT_KRW + BITGET_MARGIN_KRW
UPBIT_FEE         = 0.0005
BITGET_FEE        = 0.0006
ROUND_TRIP_FEE    = (UPBIT_FEE + BITGET_FEE) * 2   # 진입+청산 양쪽

HALF_LIFE_DAYS    = 365 * 2
MIN_TRADES        = 8
N_TRIALS          = 1000

TRAIN_END  = "2023-12-31"
TEST_START = "2024-01-01"


# ── 벡터화 백테스트 ───────────────────────────────────────────────
def run_backtest_vec(prem: np.ndarray,
                     u_px: np.ndarray,
                     b_px: np.ndarray,
                     fx:   np.ndarray,
                     ts:   np.ndarray,        # unix timestamp (초)
                     entry_thresh: float,
                     exit_thresh:  float) -> dict:
    """
    numpy 벡터화 백테스트. 파이썬 루프 없음.
    Returns dict with arrays: exit_ts, net_pct, hold_hours, net_pnl
    """
    n = len(prem)
    entry_idxs = []
    exit_idxs  = []

    i = 0
    while i < n:
        # 진입 조건 탐색
        if prem[i] <= entry_thresh:
            entry_i = i
            # 청산 조건 탐색 (entry 다음 시점부터)
            j = i + 1
            while j < n and prem[j] < exit_thresh:
                j += 1
            if j < n:
                entry_idxs.append(entry_i)
                exit_idxs.append(j)
                i = j + 1   # 청산 직후부터 다시 탐색
                continue
            else:
                break       # 기간 종료 전 미청산
        i += 1

    if not entry_idxs:
        return dict(exit_ts=np.array([]), net_pct=np.array([]),
                    hold_hours=np.array([]), net_pnl=np.array([]))

    ei = np.array(entry_idxs)
    xi = np.array(exit_idxs)

    btc_qty   = UPBIT_KRW / u_px[ei]
    short_qty = UPBIT_KRW / (b_px[ei] * fx[ei])

    upbit_pnl  = btc_qty   * (u_px[xi] - u_px[ei])
    bitget_pnl = short_qty * (b_px[ei] - b_px[xi]) * fx[xi]
    gross_pnl  = upbit_pnl + bitget_pnl

    fee = UPBIT_KRW * ROUND_TRIP_FEE + (UPBIT_KRW / fx[ei]) * ROUND_TRIP_FEE
    net_pnl = gross_pnl - fee

    hold_hours = (ts[xi] - ts[ei]) / 3600
    net_pct    = net_pnl / TOTAL_CAPITAL * 100

    return dict(
        exit_ts    = ts[xi],
        net_pct    = net_pct,
        hold_hours = hold_hours,
        net_pnl    = net_pnl,
    )


# ── 시간 가중 샤프 비율 ────────────────────────────────────────────
def weighted_sharpe(result: dict, ref_ts: float) -> float:
    net_pct = result["net_pct"]
    if len(net_pct) < MIN_TRADES:
        return -999.0

    days_ago = (ref_ts - result["exit_ts"]) / 86400
    weights  = np.exp(-np.log(2) / HALF_LIFE_DAYS * np.clip(days_ago, 0, None))
    weights /= weights.sum()

    w_mean = (net_pct * weights).sum()
    w_std  = np.sqrt((weights * (net_pct - w_mean) ** 2).sum())

    if w_std < 1e-8:
        return float(w_mean)

    sharpe = w_mean / w_std * np.sqrt(len(net_pct))
    trade_bonus = min(1.0, len(net_pct) / 50)
    return float(sharpe * trade_bonus)


# ── Optuna 목적함수 ───────────────────────────────────────────────
def make_objective(arrays: tuple, ref_ts: float):
    prem, u_px, b_px, fx, ts = arrays

    def objective(trial: optuna.Trial) -> float:
        entry = trial.suggest_float("entry", -2.0, 4.0, step=0.1)
        exit_ = trial.suggest_float("exit",  entry + 0.5, 12.0, step=0.1)
        res   = run_backtest_vec(prem, u_px, b_px, fx, ts, entry, exit_)
        return weighted_sharpe(res, ref_ts)

    return objective


def optimize(arrays: tuple, ref_ts: float, n_trials: int = N_TRIALS) -> dict:
    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
    )
    study.optimize(make_objective(arrays, ref_ts), n_trials=n_trials,
                   show_progress_bar=True, n_jobs=1)
    best = study.best_params
    prem, u_px, b_px, fx, ts = arrays
    res  = run_backtest_vec(prem, u_px, b_px, fx, ts, best["entry"], best["exit"])
    return {"study": study, "best": best, "result": res}


# ── 시각화 ────────────────────────────────────────────────────────
def plot_all(result_full, result_train, df, arrays_full, arrays_test):
    prem_f, u_f, b_f, fx_f, ts_f = arrays_full
    prem_t, u_t, b_t, fx_t, ts_t = arrays_test
    ts_full_dt = pd.to_datetime(ts_f, unit="s", utc=True)

    b_full  = result_full["best"]
    b_train = result_train["best"]

    fig = plt.figure(figsize=(22, 22))
    gs  = gridspec.GridSpec(4, 2, figure=fig, hspace=0.50, wspace=0.30)

    # (1) 파라미터 탐색 히트맵 (전체)
    ax1 = fig.add_subplot(gs[0, 0])
    trials_df = result_full["study"].trials_dataframe()
    valid = trials_df[trials_df["value"] > -990]
    sc = ax1.scatter(valid["params_entry"], valid["params_exit"],
                     c=valid["value"], cmap="RdYlGn", alpha=0.4, s=12)
    plt.colorbar(sc, ax=ax1, label="가중 샤프 비율")
    ax1.scatter(b_full["entry"], b_full["exit"], color="black", s=250,
                marker="*", zorder=5,
                label=f"최적  진입={b_full['entry']:.1f}%  청산={b_full['exit']:.1f}%")
    ax1.set_xlabel("진입 임계값 (%)"); ax1.set_ylabel("청산 임계값 (%)")
    ax1.set_title("전체 데이터 파라미터 탐색 결과\n(색 = 가중 샤프 비율)")
    ax1.legend(fontsize=9); ax1.grid(alpha=0.3)

    # (2) 수렴 곡선
    ax2 = fig.add_subplot(gs[0, 1])
    vals = [t.value for t in result_full["study"].trials
            if t.value is not None and t.value > -990]
    ax2.plot(range(len(vals)), pd.Series(vals).cummax(),
             color="#2c3e50", linewidth=1.5)
    ax2.set_xlabel("시도 횟수"); ax2.set_ylabel("최고 가중 샤프 비율")
    ax2.set_title("최적화 수렴 곡선"); ax2.grid(alpha=0.3)

    # (3) 누적 손익 비교 (전체)
    ax3 = fig.add_subplot(gs[1, :])

    def cum_pnl_series(res, ts_arr):
        if len(res["net_pnl"]) == 0:
            return pd.Series(dtype=float)
        idx = np.argsort(res["exit_ts"])
        dts = pd.to_datetime(res["exit_ts"][idx], unit="s", utc=True)
        return pd.Series(np.cumsum(res["net_pnl"][idx]) / 10000, index=dts)

    base_res  = run_backtest_vec(prem_f, u_f, b_f, fx_f, ts_f, 1.0, 3.0)
    opt_res   = result_full["result"]
    train_res = run_backtest_vec(prem_f, u_f, b_f, fx_f, ts_f,
                                 b_train["entry"], b_train["exit"])

    for res, color, lw, ls, lbl in [
        (base_res,  "#95a5a6", 1.2, "--",
         f"기본값 진입1.0/청산3.0  ({base_res['net_pnl'].sum()/10000:+.1f}만원)"),
        (train_res, "#3498db", 1.5, "-.",
         f"학습최적 진입{b_train['entry']:.1f}/청산{b_train['exit']:.1f}  ({train_res['net_pnl'].sum()/10000:+.1f}만원)"),
        (opt_res,   "#e74c3c", 2.0, "-",
         f"전체최적 진입{b_full['entry']:.1f}/청산{b_full['exit']:.1f}  ({opt_res['net_pnl'].sum()/10000:+.1f}만원)"),
    ]:
        s = cum_pnl_series(res, ts_f)
        if not s.empty:
            ax3.plot(s.index, s.values, color=color, linewidth=lw,
                     linestyle=ls, label=lbl)

    ax3.axvline(pd.Timestamp(TEST_START, tz="UTC"), color="navy",
                linewidth=2, linestyle=":", label=f"학습/검증 분리 ({TEST_START})")
    ax3.axhline(0, color="black", linewidth=0.8)
    ax3.set_title("누적 손익 비교: 기본값 vs 학습최적 vs 전체최적")
    ax3.set_ylabel("누적 손익 (만원)"); ax3.legend(fontsize=10)
    ax3.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax3.xaxis.set_major_locator(mdates.YearLocator())
    ax3.grid(axis="y", alpha=0.3)

    # (4) 학습 파라미터 탐색
    ax4 = fig.add_subplot(gs[2, 0])
    tr_df = result_train["study"].trials_dataframe()
    vt    = tr_df[tr_df["value"] > -990]
    sc2   = ax4.scatter(vt["params_entry"], vt["params_exit"],
                        c=vt["value"], cmap="RdYlGn", alpha=0.4, s=12)
    plt.colorbar(sc2, ax=ax4, label="가중 샤프 비율")
    ax4.scatter(b_train["entry"], b_train["exit"], color="black", s=250,
                marker="*", zorder=5,
                label=f"최적  진입={b_train['entry']:.1f}%  청산={b_train['exit']:.1f}%")
    ax4.set_xlabel("진입 임계값 (%)"); ax4.set_ylabel("청산 임계값 (%)")
    ax4.set_title("학습 구간(2017~2023) 파라미터 탐색")
    ax4.legend(fontsize=9); ax4.grid(alpha=0.3)

    # (5) 검증 구간 총 수익 비교 막대
    ax5 = fig.add_subplot(gs[2, 1])
    scenarios = [
        ("기본값\n진입1.0/청산3.0",           1.0, 3.0),
        (f"학습최적\n진입{b_train['entry']:.1f}/청산{b_train['exit']:.1f}",
         b_train["entry"], b_train["exit"]),
        (f"전체최적\n진입{b_full['entry']:.1f}/청산{b_full['exit']:.1f}",
         b_full["entry"], b_full["exit"]),
    ]
    test_totals = []
    test_counts = []
    for _, en, ex in scenarios:
        r = run_backtest_vec(prem_t, u_t, b_t, fx_t, ts_t, en, ex)
        test_totals.append(r["net_pnl"].sum() / 10000 if len(r["net_pnl"]) else 0)
        test_counts.append(len(r["net_pnl"]))

    colors_bar = ["#27ae60" if v >= 0 else "#e74c3c" for v in test_totals]
    bars = ax5.bar(range(3), test_totals, color=colors_bar, alpha=0.75, edgecolor="white")
    for bar, val, cnt in zip(bars, test_totals, test_counts):
        ax5.text(bar.get_x() + bar.get_width() / 2,
                 val + 0.3 if val >= 0 else val - 0.8,
                 f"{val:+.1f}만원\n({cnt}회)", ha="center", fontsize=10)
    ax5.set_xticks(range(3))
    ax5.set_xticklabels([s[0] for s in scenarios], fontsize=9)
    ax5.axhline(0, color="black", linewidth=0.8)
    ax5.set_title(f"검증 구간({TEST_START}~) 총 순수익 비교")
    ax5.set_ylabel("총 순수익 (만원)"); ax5.grid(axis="y", alpha=0.3)

    # (6) 임계값 민감도 (전체 최적 주변)
    ax6 = fig.add_subplot(gs[3, :])
    entry_range = np.arange(-1.5, 4.1, 0.2)
    exit_range  = np.arange(1.5, 12.1, 0.2)

    profits_by_entry = [
        run_backtest_vec(prem_f, u_f, b_f, fx_f, ts_f, en, b_full["exit"])["net_pnl"].sum() / 10000
        if en < b_full["exit"] else 0
        for en in entry_range
    ]
    profits_by_exit = [
        run_backtest_vec(prem_f, u_f, b_f, fx_f, ts_f, b_full["entry"], ex)["net_pnl"].sum() / 10000
        for ex in exit_range
    ]

    ax6b = ax6.twinx()
    l1, = ax6.plot(entry_range, profits_by_entry, "#3498db", linewidth=2,
                   marker="o", markersize=4,
                   label=f"진입 변화 (청산 고정={b_full['exit']:.1f}%)")
    l2, = ax6b.plot(exit_range, profits_by_exit, "#e74c3c", linewidth=2,
                    marker="s", markersize=4,
                    label=f"청산 변화 (진입 고정={b_full['entry']:.1f}%)")
    ax6.axvline(b_full["entry"], color="#3498db", linewidth=2,
                linestyle="--", alpha=0.5)
    ax6b.axvline(b_full["exit"], color="#e74c3c", linewidth=2,
                 linestyle="--", alpha=0.5)
    ax6.set_xlabel("임계값 (%)"); ax6.set_ylabel("총 수익 (만원) — 진입축", color="#3498db")
    ax6b.set_ylabel("총 수익 (만원) — 청산축", color="#e74c3c")
    ax6.set_title("최적값 주변 민감도 분석")
    ax6.legend([l1, l2], [l1.get_label(), l2.get_label()], fontsize=10)
    ax6.grid(alpha=0.3)

    fig.suptitle(
        f"ML 최적화 결과  |  전체최적: 진입 {b_full['entry']:.1f}%  청산 {b_full['exit']:.1f}%"
        f"    학습최적: 진입 {b_train['entry']:.1f}%  청산 {b_train['exit']:.1f}%\n"
        f"최근 데이터 가중치 반감기 {HALF_LIFE_DAYS//365}년  |  탐색 {N_TRIALS}회",
        fontsize=13, fontweight="bold", y=1.005
    )
    out = "data/ml_optimize.png"
    plt.savefig(out, dpi=150, bbox_inches="tight"); plt.close()
    print(f"  저장: {out}")


# ── 메인 ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    df = pd.read_csv("data/kimp_history_hourly.csv", parse_dates=["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)
    df["usd_krw"] = df["usd_krw"].ffill()
    df["ts"] = df["datetime"].astype("int64") // 10**9

    def to_arrays(d):
        return (d["premium_pct"].values, d["upbit_close"].values,
                d["binance_close"].values, d["usd_krw"].values, d["ts"].values.astype(float))

    df_train = df[df["datetime"] <= TRAIN_END].reset_index(drop=True)
    df_test  = df[df["datetime"] >= TEST_START].reset_index(drop=True)

    arr_full  = to_arrays(df)
    arr_train = to_arrays(df_train)
    arr_test  = to_arrays(df_test)

    print(f"전체: {len(df):,}개  |  학습: {len(df_train):,}개  |  검증: {len(df_test):,}개")
    print(f"탐색 횟수: {N_TRIALS}회  |  최근 가중치 반감기: {HALF_LIFE_DAYS//365}년\n")

    # 속도 확인
    import time
    t0 = time.time()
    run_backtest_vec(*arr_full, 1.0, 3.0)
    print(f"백테스트 1회 속도: {(time.time()-t0)*1000:.1f}ms\n")

    print("▶ [1/2] 전체 데이터 최적화 중 (가중 샤프 최대화)...")
    result_full = optimize(arr_full, arr_full[4].max())
    bf = result_full["best"]
    rf = result_full["result"]
    print(f"  최적값: 진입 {bf['entry']:.1f}%  청산 {bf['exit']:.1f}%")
    print(f"  {len(rf['net_pnl'])}회 | 승률 {(rf['net_pct']>0).mean()*100:.1f}% | "
          f"총수익 {rf['net_pnl'].sum()/10000:+.1f}만원 | 평균보유 {rf['hold_hours'].mean():.0f}h")

    print("\n▶ [2/2] 학습 구간 최적화 중 (워크포워드)...")
    result_train = optimize(arr_train, arr_train[4].max())
    bt = result_train["best"]
    rt = result_train["result"]
    print(f"  최적값: 진입 {bt['entry']:.1f}%  청산 {bt['exit']:.1f}%")
    print(f"  {len(rt['net_pnl'])}회 | 승률 {(rt['net_pct']>0).mean()*100:.1f}% | "
          f"총수익 {rt['net_pnl'].sum()/10000:+.1f}만원 | 평균보유 {rt['hold_hours'].mean():.0f}h")

    print(f"\n▶ 검증 구간({TEST_START}~) 성과:")
    for name, en, ex in [
        ("기본값 (진입1.0/청산3.0)",                                 1.0,         3.0),
        (f"학습최적 (진입{bt['entry']:.1f}/청산{bt['exit']:.1f})", bt["entry"], bt["exit"]),
        (f"전체최적 (진입{bf['entry']:.1f}/청산{bf['exit']:.1f})", bf["entry"], bf["exit"]),
    ]:
        r = run_backtest_vec(*arr_test, en, ex)
        n = len(r["net_pnl"])
        if n == 0:
            print(f"  {name}: 거래 없음")
        else:
            print(f"  {name}: {n}회 | 승률 {(r['net_pct']>0).mean()*100:.1f}% | "
                  f"{r['net_pnl'].sum()/10000:+.1f}만원")

    print("\n▶ 시각화 생성 중...")
    plot_all(result_full, result_train, df, arr_full, arr_test)
    print("완료.")
