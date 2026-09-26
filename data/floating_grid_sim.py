"""
플로팅 그리드 체결빈도 시뮬레이터
────────────────────────────────────────────────────────────────────
목적: 코인 선택의 두 번째 축인 '체결빈도' 측정.
      스크리너(real_trading/spread_screener.py)가 코인별 '왕복비용'을 냈지만,
      비용이 낮아도 spacing 을 치는 횟수가 적으면 수익은 0 이다.
      기대수익 = 순마진(%) × 체결횟수 × 슬롯자본  으로 두 축을 곱해야 답이 나온다.

전략 재현 (paper_trader.py 플로팅 그리드):
      - 고정 진입레벨 없음. 빈 슬롯이 있으면 현재 김프에서 즉시 진입
      - 진입 김프 + spacing 도달 시 익절 후 슬롯 해제
      - 슬롯 최대 n_slots 개, 24시간 보유 시 시간손절
      - 진입 직후 재진입 방지를 위해 슬롯은 서로 다른 시점에 열림

주의:
      입력 데이터는 1분봉 종가다. 실제 봇은 10초 폴링이므로
      여기서 나온 체결횟수는 **실제의 하한**이다. 코인 간 비교용으로만 쓸 것.

실행:
      python -m data.floating_grid_sim
      python -m data.floating_grid_sim --spacing 0.3 --slots 5 --hold-hours 24
"""

import sys, os, glob, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), "candidates")

# 스크리너 14회 샘플 중앙값 (슬롯 33.3만원 기준) — real_trading/spread_screener.py
COST_PCT = {
    "BTC": 0.0040, "AVAX": 0.0719, "ETH": 0.0750, "XRP": 0.0814,
    "LINK": 0.0855, "SOL": 0.0941, "DOGE": 0.1162,
    "SUI": 0.1243, "BCH": 0.1332, "UNI": 0.1493,
    "AAVE": 0.2560, "TAO": 0.2128, "ATOM": 0.3567, "BSV": 0.7191,
}
FEE_PCT = 0.18


def simulate(prem: pd.Series, times: pd.Series, spacing: float,
             n_slots: int, hold_hours: int,
             entry_max: float | None = None,
             entry_ref: "pd.Series | None" = None) -> dict:
    """
    플로팅 그리드. 반환: 익절횟수, 시간손절횟수, 평균보유시간(분)

    보유시간은 행 인덱스가 아니라 실제 타임스탬프로 잰다.
    (결측 분이 있으면 인덱스와 경과분이 어긋난다)
    """
    mins = ((times - times.iloc[0]).dt.total_seconds() / 60).to_numpy()
    ref_arr = entry_ref.to_numpy() if entry_ref is not None else None
    blocked = 0                       # 범위 제한으로 진입을 건너뛴 횟수
    slots = []            # 각 원소: (진입김프, 진입경과분)
    exits, timeouts, hold_mins = 0, 0, []
    gross_exit = 0.0      # 익절로 벌어들인 김프폭 합 (%)
    gross_timeout = 0.0   # 시간손절 시 실현된 김프폭 합 (%) — 대개 음수
    hold_limit = hold_hours * 60

    for i, p in enumerate(prem):
        t = mins[i]
        # 1) 익절 / 시간손절 판정
        still = []
        for entry_p, entry_t in slots:
            if p >= entry_p + spacing:
                exits += 1
                gross_exit += spacing
                hold_mins.append(t - entry_t)
            elif t - entry_t >= hold_limit:
                timeouts += 1
                gross_timeout += (p - entry_p)   # 강제청산 — 실현 김프폭 그대로
                hold_mins.append(t - entry_t)
            else:
                still.append((entry_p, entry_t))
        slots = still

        # 2) 빈 슬롯 재진입 — 범위 제한이 걸려 있으면 조건을 만족할 때만
        #    entry_max: 절대 임계 (김프가 이 값 이하일 때만 진입)
        #    entry_ref: 이동 기준선 (김프가 기준선 이하일 때만 진입)
        if len(slots) < n_slots:
            ok = True
            if entry_max is not None and p > entry_max:
                ok = False
            if entry_ref is not None:
                ref = ref_arr[i]
                if ref == ref and p > ref:      # NaN 이면 판단 보류 → 진입 허용
                    ok = False
            if ok:
                slots.append((p, t))
            else:
                blocked += 1

    last = prem.iloc[-1] if hasattr(prem, "iloc") else prem[-1]
    unreal = sum((last - ep) for ep, _ in slots)   # 열린 슬롯 평가손익 (%)
    return {
        "exits": exits,
        "timeouts": timeouts,
        "gross_exit": gross_exit,
        "gross_timeout": gross_timeout,
        "unrealized": unreal,
        "blocked": blocked,
        "avg_hold_min": (sum(hold_mins) / len(hold_mins)) if hold_mins else float("nan"),
        "open_at_end": len(slots),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spacing", type=float, default=0.3)
    ap.add_argument("--slots", type=int, default=5)
    ap.add_argument("--hold-hours", type=int, default=24)
    ap.add_argument("--slot-krw", type=int, default=333_333)
    ap.add_argument("--maker", action="store_true",
                    help="지정가 체결 가정: 스프레드 비용 0, 수수료 0.14%% (비트겟 maker 0.02%%)")
    ap.add_argument("--entry-max", type=float, default=None,
                    help="절대 진입 상한(%%). 김프가 이 값 이하일 때만 진입")
    ap.add_argument("--entry-q", type=float, default=None,
                    help="이동 분위수 진입 상한 (0~1). 예: 0.3 이면 최근 구간 하위 30%% 일 때만 진입")
    ap.add_argument("--window", type=int, default=1440,
                    help="이동 분위수 창 (분). 기본 1440 = 24시간")
    ap.add_argument("--no-stop", action="store_true",
                    help="시간손절 없음 (벤치마크 추정 구조). 미실현 손실은 열린 슬롯에 쌓인다")
    args = ap.parse_args()

    fee = 0.14 if args.maker else FEE_PCT
    if args.no_stop:
        args.hold_hours = 10 ** 6

    files = sorted(glob.glob(os.path.join(DATA_DIR, "*_1m.csv")))
    if not files:
        print(f"데이터 없음: {DATA_DIR}\n먼저 python -m data.collect_candidates --days 14")
        return

    print("=" * 88)
    print(f"  플로팅 그리드 체결빈도 — spacing {args.spacing}%  슬롯 {args.slots}개  "
          f"시간손절 {args.hold_hours}h  슬롯자본 {args.slot_krw:,}원")
    print("=" * 88)

    rows = []
    for f in files:
        coin = os.path.basename(f).replace("_1m.csv", "").upper()
        df = pd.read_csv(f)
        df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
        df = df.sort_values("datetime").reset_index(drop=True)
        # 커버리지 기준: 실제 관측된 분 수 (구간 길이가 아니라)
        days = len(df) / (24 * 60)
        ref = None
        if args.entry_q is not None:
            ref = df["premium_pct"].rolling(args.window, min_periods=args.window // 4) \
                                   .quantile(args.entry_q)
        r = simulate(df["premium_pct"], df["datetime"], args.spacing,
                     args.slots, args.hold_hours,
                     entry_max=args.entry_max, entry_ref=ref)
        cost = 0.0 if args.maker else COST_PCT.get(coin)
        n_trades = r["exits"] + r["timeouts"]
        if cost is None:
            net_pct = krw_day = None
        else:
            # 모든 청산에 수수료+왕복비용이 붙는다. 시간손절도 예외 없음.
            gross = r["gross_exit"] + r["gross_timeout"]
            net_pct = gross - n_trades * (fee + cost)
            krw_day = net_pct / 100 * args.slot_krw / days if days else 0
        rows.append({
            "coin": coin, "days": days, "exits": r["exits"],
            "per_day": r["exits"] / days if days else 0,
            "timeouts": r["timeouts"], "hold": r["avg_hold_min"],
            "to_gross": r["gross_timeout"], "n_trades": n_trades,
            "unreal": r["unrealized"], "open_end": r["open_at_end"],
            "blocked": r["blocked"],
            "vol": df["premium_pct"].diff().abs().mean(),
            "rng": df["premium_pct"].max() - df["premium_pct"].min(),
            "cost": cost, "net": net_pct, "krw_day": krw_day,
        })

    rows.sort(key=lambda r: (r["krw_day"] is None, -(r["krw_day"] or 0)))
    print(f"  {'코인':6}{'익절':>7}{'시간손절':>9}{'손절실현':>10}{'평균보유':>9}"
          f"{'미실현':>9}{'비용':>9}{'실현순손익':>11}{'일손익':>10}")
    print("  " + "-" * 84)
    for r in rows:
        cost = "  —" if r["cost"] is None else f"{r['cost']:.4f}%"
        net = "  —" if r["net"] is None else f"{r['net']:+.3f}%"
        krw = "  —" if r["krw_day"] is None else f"{r['krw_day']:>9,.0f}원"
        hold = "—" if pd.isna(r["hold"]) else f"{r['hold']:.0f}분"
        print(f"  {r['coin']:6}{r['exits']:>7,}{r['timeouts']:>9,}{r['to_gross']:>9.2f}%"
              f"{hold:>9}{r['unreal']:>8.2f}%{cost:>10}{net:>11}{krw:>10}")
    print("  " + "-" * 84)
    print(f"  14일순손익 = 익절이익 + 손절실현 - (총청산 × (수수료 {FEE_PCT}% + 왕복비용))")
    print(f"  일손익 = 위 값 × 슬롯자본 ÷ 14일  (코인당 슬롯 {args.slots}개 합계)")
    print(f"  * 시간손절도 수수료와 왕복비용을 그대로 문다"
          )
    print("  * 1분봉 기준이므로 실제 10초 폴링 대비 체결횟수는 과소추정 (하한값)")
    print("=" * 88)


if __name__ == "__main__":
    main()
