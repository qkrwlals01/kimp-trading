"""
김프 시계열 구조 분석 — 랜덤워크인가 평균회귀인가
────────────────────────────────────────────────────────────────────
왜 이걸 보는가:
      플로팅 그리드는 '진입 후 김프가 spacing 만큼 오른다'에 베팅한다.
      이 베팅에 우위가 있으려면 김프 수준(level)에 정보가 있어야 한다.

        - 김프가 랜덤워크(I(1))  → 수준에 정보 없음. 진입 레벨 무관, 기대수익 0.
                                   수수료·스프레드만큼 확정 손실. 전략 성립 불가.
        - 김프가 평균회귀(I(0))  → 낮을 때 사면 우위. 진입 하한 필터가 근거를 가짐.
                                   반감기가 보유기간 대비 짧아야 실현 가능.

검정:
      ADF        귀무 = 단위근(랜덤워크).      p < 0.05 면 정상성(평균회귀) 지지
      KPSS       귀무 = 정상성.                p < 0.05 면 정상성 기각 → 랜덤워크 지지
                 (ADF·KPSS 는 귀무가 반대. 둘을 교차로 봐야 결론이 선다)
      Ljung-Box  차분의 자기상관 유무. 백색잡음이면 유의하지 않음
      분산비(VR) VR<1 평균회귀 / VR=1 랜덤워크 / VR>1 추세
      Hurst      H<0.5 평균회귀 / H=0.5 랜덤워크 / H>0.5 추세
      OU 반감기  평균으로 절반 돌아오는 데 걸리는 시간

해상도 주의:
      1분봉에는 호가 튐(bid-ask bounce) 노이즈가 섞여 차분에 인위적 음의 상관을
      만든다 → 실제보다 평균회귀처럼 보인다. 여러 해상도로 함께 봐야 한다.

실행:
      python -m data.kimp_timeseries
      python -m data.kimp_timeseries --coins BTC ETH
"""

import sys, os, glob, argparse, warnings
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import adfuller, kpss, acf
from statsmodels.stats.diagnostic import acorr_ljungbox

DATA_DIR = os.path.join(os.path.dirname(__file__), "candidates")


def hurst(x: np.ndarray, max_lag: int = 100) -> float:
    """R/S 대신 분산-지연 회귀로 추정 (금융 시계열에 통상 쓰는 방식)"""
    lags = np.unique(np.logspace(0, np.log10(max_lag), 25).astype(int))
    lags = lags[lags >= 2]
    tau = [np.sqrt(np.std(x[l:] - x[:-l])) for l in lags]
    return float(np.polyfit(np.log(lags), np.log(tau), 1)[0] * 2.0)


def variance_ratio(x: np.ndarray, q: int) -> float:
    """VR(q). 1 이면 랜덤워크, <1 평균회귀, >1 추세"""
    d = np.diff(x)
    n = len(d)
    var1 = np.var(d, ddof=1)
    agg = np.add.reduceat(d, np.arange(0, n - n % q, q))
    varq = np.var(agg, ddof=1)
    return float(varq / (q * var1)) if var1 > 0 else float("nan")


def ou_halflife(x: np.ndarray) -> float:
    """dx = -k(x - mu)dt 회귀 → 반감기 = ln2/k (단위: 관측 간격)"""
    lag = x[:-1]
    delta = np.diff(x)
    beta = np.polyfit(lag, delta, 1)[0]
    return float(np.log(2) / -beta) if beta < 0 else float("inf")


def analyse(x: np.ndarray, label: str) -> dict:
    adf_p = adfuller(x, autolag="AIC")[1]
    kpss_p = kpss(x, regression="c", nlags="auto")[1]
    d = np.diff(x)
    lb_p = float(acorr_ljungbox(d, lags=[10], return_df=True)["lb_pvalue"].iloc[0])
    ac1 = float(acf(d, nlags=1, fft=True)[1])
    return {
        "label": label, "n": len(x),
        "adf_p": adf_p, "kpss_p": kpss_p, "lb_p": lb_p, "ac1": ac1,
        "vr2": variance_ratio(x, 2), "vr10": variance_ratio(x, 10),
        "vr60": variance_ratio(x, 60),
        "hurst": hurst(x), "hl": ou_halflife(x),
    }


def verdict(r: dict) -> str:
    mr = (r["adf_p"] < 0.05) + (r["kpss_p"] >= 0.05) + (r["vr60"] < 0.9) + (r["hurst"] < 0.45)
    rw = (r["adf_p"] >= 0.05) + (r["kpss_p"] < 0.05) + (abs(r["vr60"] - 1) < 0.1) + (abs(r["hurst"] - 0.5) < 0.05)
    if mr >= 3:
        return "평균회귀"
    if rw >= 3:
        return "랜덤워크"
    return "혼재"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", nargs="*", default=None)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(DATA_DIR, "*_1m.csv")))
    if args.coins:
        want = {c.lower() for c in args.coins}
        files = [f for f in files if os.path.basename(f).replace("_1m.csv", "") in want]
    if not files:
        print("데이터 없음. 먼저: python -m data.collect_candidates --days 14")
        return

    RES = {"1분": 1, "5분": 5, "30분": 30, "2시간": 120}

    print("=" * 92)
    print("  김프 시계열 구조 분석 — 랜덤워크 vs 평균회귀")
    print("=" * 92)
    print("  ADF p<0.05 → 평균회귀 지지 | KPSS p<0.05 → 랜덤워크 지지 (귀무가 반대)")
    print("  VR≈1 랜덤워크, <1 평균회귀 | Hurst≈0.5 랜덤워크, <0.5 평균회귀")
    print()

    for f in files:
        coin = os.path.basename(f).replace("_1m.csv", "").upper()
        s = pd.read_csv(f)["premium_pct"].astype(float).to_numpy()
        print(f"  ── {coin}  ({len(s):,} 분봉) " + "─" * 50)
        print(f"    {'해상도':7}{'n':>7}{'ADF p':>9}{'KPSS p':>9}{'차분AC1':>9}"
              f"{'LB p':>9}{'VR(2)':>8}{'VR(60)':>8}{'Hurst':>8}{'반감기':>11}  판정")
        for name, step in RES.items():
            x = s[::step]
            if len(x) < 300:
                continue
            r = analyse(x, name)
            hl = "∞" if not np.isfinite(r["hl"]) else f"{r['hl']*step:,.0f}분"
            print(f"    {name:7}{r['n']:>7,}{r['adf_p']:>9.4f}{r['kpss_p']:>9.4f}"
                  f"{r['ac1']:>9.3f}{r['lb_p']:>9.4f}{r['vr2']:>8.3f}{r['vr60']:>8.3f}"
                  f"{r['hurst']:>8.3f}{hl:>11}  {verdict(r)}")
        print()
    print("=" * 92)


if __name__ == "__main__":
    main()
