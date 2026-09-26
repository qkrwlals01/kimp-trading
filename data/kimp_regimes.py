"""
장기 국면 분석 — 하락 국면에서도 우위가 유지되는가, 물리면 얼마나 갇히는가
────────────────────────────────────────────────────────────────────
14일 데이터는 김프가 평균회귀하던 한 국면만 담는다. 실제 투입 전에 답해야 할
질문은 두 가지다.

  1) 김프가 추세적으로 무너지는 국면에서도 '낮을 때 진입' 우위가 남는가?
     (하위 10% 가 계속 갱신되며 매번 물릴 수 있다)
  2) 한번 물리면 회복까지 얼마나 걸리는가? = 자본 잠김의 최악 시나리오

데이터 한계 (결론 해석 시 반드시 감안):
  - 해외거래소가 **바이낸스** (실제 체결은 비트겟). 김프 절대수준이 다르다.
  - 환율이 **yfinance 일봉** — 국내 고시 대비 약 -13원 편향. 편향이 거의 일정해
    '변화'와 국면 판정에는 쓸 수 있으나 절대 수익률 산출에는 부적합.
  - **1시간봉** — 분 단위 교차를 놓친다. 도달시간은 과대추정(느리게) 될 수 있다.
  → 절대값이 아니라 '국면 간 비교'와 '최악 지속기간'을 읽는 용도.

실행:
      python -m data.kimp_regimes
      python -m data.kimp_regimes --spacing 0.3 --window 24
"""

import sys, os, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np
import pandas as pd

DATA = os.path.dirname(__file__)
FILES = {"BTC": "kimp_btc_hourly.csv", "ETH": "kimp_eth_hourly.csv", "XRP": "kimp_xrp_hourly.csv"}


class RangeMax:
    """희소 테이블. 구간 최대값 O(1) 질의."""

    def __init__(self, a: np.ndarray):
        n = len(a)
        k = max(1, int(np.log2(n)) + 1)
        self.t = np.empty((k, n), dtype=float)
        self.t[0] = a
        for j in range(1, k):
            span = 1 << j
            if n < span:
                self.t[j] = self.t[j - 1]
                continue
            m = n - span + 1
            self.t[j, :m] = np.maximum(self.t[j - 1, :m], self.t[j - 1, (1 << (j - 1)):(1 << (j - 1)) + m])
            self.t[j, m:] = self.t[j - 1, m:]
        self.log = np.zeros(n + 1, dtype=int)
        for i in range(2, n + 1):
            self.log[i] = self.log[i // 2] + 1

    def query(self, l: int, r: int) -> float:
        """[l, r] 최대값 (양끝 포함)."""
        j = self.log[r - l + 1]
        return max(self.t[j, l], self.t[j, r - (1 << j) + 1])


def first_passage(p: np.ndarray, spacing: float, cap: int) -> np.ndarray:
    """각 시점에서 p[i]+spacing 에 처음 도달하기까지의 시간(칸). 미도달은 -1."""
    n = len(p)
    rm = RangeMax(p)
    out = np.full(n, -1, dtype=np.int64)
    for i in range(n):
        hi = min(n - 1, i + cap)
        if rm.query(i, hi) < p[i] + spacing:
            continue
        lo, r = i, hi                      # 이분탐색: 최초 도달 지점
        while lo < r:
            mid = (lo + r) // 2
            if rm.query(i, mid) >= p[i] + spacing:
                r = mid
            else:
                lo = mid + 1
        out[i] = lo - i
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spacing", type=float, default=0.3)
    ap.add_argument("--window", type=int, default=24, help="진입 분위수 이동창 (시간)")
    ap.add_argument("--cap-days", type=int, default=180, help="도달 추적 상한 (일)")
    args = ap.parse_args()
    CAP = args.cap_days * 24

    print("=" * 104)
    print(f"  장기 국면 분석 — spacing {args.spacing}%, 진입창 {args.window}시간, 추적상한 {args.cap_days}일")
    print("  ⚠ 바이낸스 기준 · yfinance 환율 · 1시간봉 — 국면 비교와 지속기간 용도")
    print("=" * 104)

    for coin, fn in FILES.items():
        f = os.path.join(DATA, fn)
        if not os.path.exists(f):
            continue
        df = pd.read_csv(f)
        df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
        df = df.sort_values("datetime").dropna(subset=["premium_pct"]).reset_index(drop=True)
        p = df["premium_pct"].astype(float).to_numpy()

        rank = df["premium_pct"].rolling(args.window, min_periods=args.window // 2).rank(pct=True).to_numpy()
        # 국면: 직전 30일 김프 변화
        trend = (df["premium_pct"] - df["premium_pct"].shift(30 * 24)).to_numpy()
        fp = first_passage(p, args.spacing, CAP)
        # 끝 CAP 시간은 추적 구간이 잘려 '미도달'로 오판된다 → 통계에서 제외
        valid = np.ones(len(p), bool)
        valid[len(p) - CAP:] = False

        print(f"\n  ══ {coin}  {df['datetime'].min():%Y-%m} ~ {df['datetime'].max():%Y-%m}  "
              f"({len(df):,}시간, {len(df)/24/365:.1f}년)  김프 {p.min():+.1f}~{p.max():+.1f}%")

        # 1) 진입 분위수 × 국면
        print(f"\n    [1] 진입 분위수별 · 국면별 — 24시간 내 {args.spacing}% 도달률(%) / 도달 중앙값(시간)")
        print(f"      {'진입구간':10}" + "".join(f"{n:>22}" for n in ["하락국면", "횡보국면", "상승국면", "전체"]))
        regs = [("하락", trend < -0.5), ("횡보", (trend >= -0.5) & (trend <= 0.5)),
                ("상승", trend > 0.5), ("전체", np.ones(len(p), bool))]
        for lo, hi, lab in [(0, .1, "하위 0~10%"), (.1, .3, "10~30%"), (.3, .7, "30~70%"), (.7, 1.01, "상위 70~100%")]:
            base = (rank >= lo) & (rank < hi)
            line = f"      {lab:10}"
            for _, rmask in regs:
                m = base & rmask & ~np.isnan(rank) & valid
                if m.sum() < 100:
                    line += f"{'-':>22}"
                    continue
                reached = fp[m] >= 0
                hit24 = (fp[m] >= 0) & (fp[m] <= 24)
                med = np.median(fp[m][reached]) if reached.any() else np.nan
                line += f"{hit24.mean()*100:>13.1f}% /{med:>6.0f}h"
            print(line)

        # 2) 도달시간 분포 (하위 10% 진입 기준)
        m = (rank < .1) & ~np.isnan(rank) & valid
        hit = fp[m] >= 0
        v = np.sort(fp[m][hit])
        if len(v):
            print(f"\n    [2] 하위 10% 진입 후 도달시간 분포  (n={m.sum():,}, 도달 {hit.mean()*100:.1f}%)")
            print(f"      중앙값 {np.median(v):>5.0f}h | 90분위 {np.percentile(v,90):>6.0f}h "
                  f"({np.percentile(v,90)/24:.1f}일) | 99분위 {np.percentile(v,99):>7.0f}h "
                  f"({np.percentile(v,99)/24:.1f}일) | 최장 {v.max():>7.0f}h ({v.max()/24:.0f}일)")
            print(f"      24시간 내 {(v<=24).mean()*100:>5.1f}%  |  7일 내 {(v<=168).mean()*100:>5.1f}%  "
                  f"|  30일 내 {(v<=720).mean()*100:>5.1f}%  |  {args.cap_days}일 내 미도달 {(1-hit.mean())*100:.1f}%")

        # 2-b) 연도별 기회 추이 — 김프 변동성이 곧 이 전략의 연료다
        print(f"\n    [2-b] 연도별 기회 추이")
        print(f"      {'연도':6}{'평균|시간변화|':>14}{'24h 도달률':>12}{'하위10% 도달중앙값':>18}")
        yr = df["datetime"].dt.year.to_numpy()
        for y in sorted(set(yr)):
            ym = (yr == y) & valid
            if ym.sum() < 500:
                continue
            d = np.abs(np.diff(p[yr == y])).mean()
            h24 = ((fp[ym] >= 0) & (fp[ym] <= 24)).mean() * 100
            lm = ym & (rank < .1) & ~np.isnan(rank)
            med = np.median(fp[lm][fp[lm] >= 0]) if (lm.sum() and (fp[lm] >= 0).any()) else np.nan
            print(f"      {y:<6}{d:>13.4f}%{h24:>11.1f}%{med:>16.0f}h")

        # 3) 최악 잠김 구간 — 도달까지 가장 오래 걸린 진입 시점들
        cand = np.where(valid & ~np.isnan(rank) & (rank < .1))[0]
        if len(cand):
            print(f"\n    [3] 하위10% 진입 후 가장 오래 갇힌 시점 (자본 잠김 최악)")
            order = cand[np.argsort(-np.where(fp[cand] < 0, CAP + 1, fp[cand]))]
            shown, seen = 0, []
            for i in order:
                if any(abs(i - j) < 24 * 14 for j in seen):
                    continue        # 같은 사건 중복 제거
                seen.append(i); shown += 1
                d = fp[i]
                dur = f"{d/24:>5.0f}일" if d >= 0 else f">{args.cap_days}일"
                print(f"      {df['datetime'][i]:%Y-%m-%d}  진입 김프 {p[i]:+.2f}%  →  도달까지 {dur}")
                if shown >= 5:
                    break
    print("\n" + "=" * 104)


if __name__ == "__main__":
    main()
