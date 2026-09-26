"""
진입 시점별 기대수익 — 범위 제한에 실제 우위가 있는가
────────────────────────────────────────────────────────────────────
통계적 평균회귀(VR<1, Hurst<0.5)가 있다고 해서 거래 가능한 우위가 있는 건 아니다.
1분봉의 평균회귀 상당분은 호가 튐(bid-ask bounce)이고, 그걸 잡으려면 스프레드를
넘어야 하는데 넘는 비용이 곧 그 튐의 크기다.

그래서 통계 검정 대신 경제적 검정을 한다:
      "진입 시점의 김프 분위수별로, 이후 김프가 어떻게 움직이는가"

두 가지를 본다.
  1) 기대 변화   : 진입 후 h분 뒤 김프 변화의 평균. 우위의 크기.
  2) 도달 확률   : 진입 후 h분 안에 +spacing 에 도달할 확률. 그리드가 실제로 익절할 확률.

판정 기준:
      기대 변화가 (수수료 + 왕복비용) 을 넘어야 거래 가치가 있다.
      시장가 0.18%+비용, 지정가 0.14%.

실행:
      python -m data.entry_edge
      python -m data.entry_edge --coins BTC AVAX LINK --window 1440
"""

import sys, os, glob, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), "candidates")


def fwd_max(s: pd.Series, h: int) -> pd.Series:
    """향후 h분 내 최대값 (현재 포함)."""
    return s[::-1].rolling(h, min_periods=1).max()[::-1]


def analyse(df: pd.DataFrame, window: int, spacing: float, horizons: list) -> pd.DataFrame:
    p = df["premium_pct"].astype(float)
    # 진입 시점의 '최근 window 분 분포 내 위치' (0=최저, 1=최고)
    rank = p.rolling(window, min_periods=window // 4).rank(pct=True)

    rows = []
    for lo, hi in [(0, .1), (.1, .2), (.2, .3), (.3, .5), (.5, .7), (.7, .9), (.9, 1.01)]:
        m = (rank >= lo) & (rank < hi)
        if m.sum() < 200:
            continue
        row = {"구간": f"{int(lo*100)}~{int(hi*100)}%", "n": int(m.sum())}
        for h in horizons:
            chg = p.shift(-h) - p
            hit = (fwd_max(p, h) - p) >= spacing
            row[f"평균{h}"] = float(chg[m].mean())
            row[f"도달{h}"] = float(hit[m].mean() * 100)
        rows.append(row)
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", nargs="*", default=["BTC", "ETH", "XRP", "SOL", "AVAX", "LINK"])
    ap.add_argument("--window", type=int, default=1440)
    ap.add_argument("--spacing", type=float, default=0.3)
    args = ap.parse_args()

    H = [60, 240, 1440]
    print("=" * 100)
    print(f"  진입 분위수별 기대수익 — 이동창 {args.window}분, spacing {args.spacing}%")
    print("=" * 100)
    print("  '평균h' = 진입 후 h분 뒤 김프 변화 평균(%)   '도달h' = h분 내 +spacing 도달 확률(%)")
    print("  손익분기: 지정가 0.14%, 시장가 0.18%+왕복비용\n")

    agg = {}
    for c in args.coins:
        f = os.path.join(DATA_DIR, c.lower() + "_1m.csv")
        if not os.path.exists(f):
            continue
        df = pd.read_csv(f)
        t = analyse(df, args.window, args.spacing, H)
        agg[c] = t
        print(f"  ── {c} " + "─" * 88)
        hdr = f"    {'진입구간':10}{'n':>7}"
        for h in H:
            hdr += f"{'평균'+str(h)+'분':>11}{'도달'+str(h)+'분':>11}"
        print(hdr)
        for _, r in t.iterrows():
            line = f"    {r['구간']:10}{r['n']:>7,}"
            for h in H:
                line += f"{r[f'평균{h}']:>10.4f}%{r[f'도달{h}']:>10.1f}%"
            print(line)
        print()

    if agg:
        print("  ── 코인 평균 " + "─" * 82)
        keys = list(next(iter(agg.values()))["구간"])
        print(f"    {'진입구간':10}" + "".join(f"{'평균'+str(h)+'분':>11}{'도달'+str(h)+'분':>11}" for h in H))
        for k in keys:
            line = f"    {k:10}"
            for h in H:
                v = np.mean([t[t["구간"] == k][f"평균{h}"].iloc[0] for t in agg.values()
                             if (t["구간"] == k).any()])
                d = np.mean([t[t["구간"] == k][f"도달{h}"].iloc[0] for t in agg.values()
                             if (t["구간"] == k).any()])
                line += f"{v:>10.4f}%{d:>10.1f}%"
            print(line)
    print("=" * 100)


if __name__ == "__main__":
    main()
