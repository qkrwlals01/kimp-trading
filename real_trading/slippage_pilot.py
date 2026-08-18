"""
슬리피지 실측 파일럿
────────────────────────────────────────────────────────────────────
목적: 실거래에서 '한 왕복(업비트 매수→비트겟 숏 진입→업비트 매도→비트겟 숏 청산)'
      에 실제로 드는 체결비용(슬리피지)을 측정한다.
      모의매매는 slip=0 으로 가정했으므로, 이 값이 전략 생사를 가른다.

측정 원리:
      김프 목표를 기다리지 않고 헤지쌍을 즉시 열고 바로 닫는다.
      가격 변동이 거의 없는 상태이므로 왕복 손실 ≈ 순수 체결비용(수수료+슬리피지).
      각 다리에서 '주문 직전 티커가격' 대비 '실제 체결 평균가' 차이를 슬리피지로 집계.

판정 기준:
      익절폭 spacing 0.30%  −  수수료 0.18%  =  순마진 0.12%
      → 왕복 슬리피지 총합이 0.12% 를 넘으면 이 전략은 실거래에서 적자.

안전장치:
      - DRY_RUN 기본 True (주문 안 냄, 티커/호가/스프레드만 출력)
      - 주문당 자본 상한 하드코딩 (초과 시 실행 거부)
      - 업비트만 체결되고 비트겟 실패 시 → 업비트 즉시 되팔아 언헤지 해소 후 중단
      - 라이브 실행은 --live 플래그 + 콘솔 확인 입력 필요

실행:
      python -m real_trading.slippage_pilot           # 드라이런(안전)
      python -m real_trading.slippage_pilot --live     # 실주문
"""

import sys, os, time, csv, argparse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from utils.exchange_rate import get_usd_krw
from real_trading.real_client import UpbitReal, BitgetReal

# ── 파일럿 설정 ───────────────────────────────────────────────────
CAPITAL_KRW      = 50_000     # 왕복 1건당 업비트 투입 자본 (테스트용 소액)
LEVERAGE         = 5
N_ROUND_TRIPS    = 5          # 코인당 왕복 횟수
HOLD_SECONDS     = 2          # 진입 후 청산까지 대기(초) — 순수 체결비용 측정용 짧게
MAX_KRW_PER_ORDER = 100_000   # 안전 상한: 이 값 초과 설정 시 실행 거부

# 테스트 코인: (업비트마켓, 비트겟심볼, 통화, 비트겟수량 소수자리)
PILOT_COINS = [
    ("KRW-BSV",  "BSVUSDT",  "BSV",  2),   # 성과 1위, 유동성 상대적 낮음 → 최악 케이스
    ("KRW-AAVE", "AAVEUSDT", "AAVE", 1),   # 성과 2위
]

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
LOG_CSV = os.path.join(LOG_DIR, "slippage.csv")
HEADER = [
    "dt", "coin", "trip", "usd_krw",
    "exp_up_buy", "fill_up_buy", "slip_up_buy_pct",
    "exp_bg_open", "fill_bg_open", "slip_bg_open_pct",
    "exp_up_sell", "fill_up_sell", "slip_up_sell_pct",
    "exp_bg_close", "fill_bg_close", "slip_bg_close_pct",
    "total_slip_pct", "roundtrip_krw",
]


def _log(row: dict):
    os.makedirs(LOG_DIR, exist_ok=True)
    new = not os.path.exists(LOG_CSV)
    with open(LOG_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADER)
        if new:
            w.writeheader()
        w.writerow(row)


def dry_run():
    """주문 없이 현재 스프레드로 예상 체결비용 상한을 보여준다."""
    print("=" * 66)
    print("  DRY-RUN — 주문 없음. 현재 호가 스프레드로 슬리피지 상한 추정")
    print("=" * 66)
    usd_krw = get_usd_krw()
    print(f"  환율(dunamu): {usd_krw:,.1f}\n")
    print(f"  {'코인':6} {'업비트스프레드':>12} {'비트겟스프레드':>12} {'왕복상한':>10}  판정")
    print("  " + "-" * 60)
    for market, symbol, cur, _ in PILOT_COINS:
        up = UpbitReal(market, cur)
        bg = BitgetReal(symbol)
        uob = up.get_orderbook()
        bob = bg.get_orderbook()
        up_spread = (uob["ask"] - uob["bid"]) / ((uob["ask"] + uob["bid"]) / 2) * 100
        bg_spread = (bob["ask"] - bob["bid"]) / ((bob["ask"] + bob["bid"]) / 2) * 100
        # 시장가 왕복은 각 거래소 스프레드를 대략 1회씩 넘는다고 근사
        roundtrip_max = up_spread + bg_spread
        margin = 0.30 - 0.18   # spacing - fee = 순마진 0.12%
        verdict = "여유" if roundtrip_max < margin else ("위험" if roundtrip_max < margin * 2 else "적자우려")
        print(f"  {cur:6} {up_spread:>11.4f}% {bg_spread:>11.4f}% {roundtrip_max:>9.4f}%  {verdict}")
    print("\n  * 순마진 기준선 0.12% (spacing 0.30% - 수수료 0.18%)")
    print("  * 실제 시장가 체결은 스프레드보다 더 나쁠 수 있음 → --live 로 실측 필요")
    print("=" * 66)


def _round_trip(market, symbol, cur, size_dec, usd_krw, trip):
    up = UpbitReal(market, cur)
    bg = BitgetReal(symbol)

    # 비트겟 숏 수량 = 업비트 자본과 명목가치 매칭
    exp_bg_open = bg.get_price()
    notional_usdt = CAPITAL_KRW / usd_krw
    size = round(notional_usdt / exp_bg_open, size_dec)
    if size <= 0:
        raise ValueError(f"{cur}: 계산된 수량 0 — 자본 부족")
    bg.set_leverage(LEVERAGE)

    # ── 1) 업비트 매수 ──
    exp_up_buy = up.get_price()
    up_buy_uuid = up.buy_market(CAPITAL_KRW)
    f_up_buy = up.fetch_fill(up_buy_uuid)
    bought_vol = f_up_buy["volume"]

    # ── 2) 비트겟 숏 진입 ── (여기서 실패하면 업비트 언헤지 → 즉시 되팔기)
    try:
        exp_bg_open = bg.get_price()
        bg_open_id = bg.open_short(size, LEVERAGE)
        f_bg_open = bg.fetch_fill(bg_open_id)
    except Exception as e:
        print(f"  !! 비트겟 숏 진입 실패 → 업비트 {bought_vol} 되팔아 언헤지 해소")
        up.sell_market(bought_vol)
        raise RuntimeError(f"{cur} 헤지 실패, 언와인드 완료: {e}")

    if HOLD_SECONDS:
        time.sleep(HOLD_SECONDS)

    # ── 3) 업비트 매도 ──
    exp_up_sell = up.get_price()
    up_sell_uuid = up.sell_market(bought_vol)
    f_up_sell = up.fetch_fill(up_sell_uuid)

    # ── 4) 비트겟 숏 청산 ── (실패 시 재시도, 끝까지 실패하면 naked short 경고)
    exp_bg_close = bg.get_price()
    f_bg_close = None
    for attempt in range(3):
        try:
            bg_close_id = bg.close_short(size)
            f_bg_close = bg.fetch_fill(bg_close_id)
            break
        except Exception as e:
            print(f"  !! [{cur}] 숏 청산 시도 {attempt+1}/3 실패: {e}")
            time.sleep(1)
    if f_bg_close is None:
        raise RuntimeError(f"{cur} 숏 청산 실패 — 비트겟에 숏 포지션 남음! 수동 청산 필요")

    # ── 슬리피지 (양수 = 불리) ──
    s_up_buy   = (f_up_buy["avg_price"]  - exp_up_buy)   / exp_up_buy   * 100
    s_bg_open  = (exp_bg_open - f_bg_open["avg_price"])  / exp_bg_open  * 100
    s_up_sell  = (exp_up_sell - f_up_sell["avg_price"])  / exp_up_sell  * 100
    s_bg_close = (f_bg_close["avg_price"] - exp_bg_close) / exp_bg_close * 100
    total_slip = s_up_buy + s_bg_open + s_up_sell + s_bg_close

    # 실제 왕복 손익(KRW) — 가격변동 거의 없으므로 ≈ -(수수료+슬리피지)
    upbit_pl  = f_up_sell["funds"] - f_up_buy["funds"] - f_up_buy["fee"] - f_up_sell["fee"]
    bg_pl_usdt = (f_bg_open["avg_price"] - f_bg_close["avg_price"]) * size \
                 - f_bg_open["fee_usdt"] - f_bg_close["fee_usdt"]
    roundtrip_krw = upbit_pl + bg_pl_usdt * usd_krw

    _log({
        "dt": datetime.now(timezone.utc).isoformat(), "coin": cur, "trip": trip, "usd_krw": round(usd_krw, 1),
        "exp_up_buy": exp_up_buy, "fill_up_buy": round(f_up_buy["avg_price"], 4), "slip_up_buy_pct": round(s_up_buy, 4),
        "exp_bg_open": exp_bg_open, "fill_bg_open": f_bg_open["avg_price"], "slip_bg_open_pct": round(s_bg_open, 4),
        "exp_up_sell": exp_up_sell, "fill_up_sell": round(f_up_sell["avg_price"], 4), "slip_up_sell_pct": round(s_up_sell, 4),
        "exp_bg_close": exp_bg_close, "fill_bg_close": f_bg_close["avg_price"], "slip_bg_close_pct": round(s_bg_close, 4),
        "total_slip_pct": round(total_slip, 4), "roundtrip_krw": round(roundtrip_krw, 0),
    })
    print(f"  [{cur}] #{trip}  슬리피지합 {total_slip:+.4f}%  왕복손익 {roundtrip_krw:+,.0f}원  "
          f"(매수 {s_up_buy:+.3f} / 숏 {s_bg_open:+.3f} / 매도 {s_up_sell:+.3f} / 청산 {s_bg_close:+.3f})")

    # 잔여 포지션 감사 — 헤지가 완전히 청산됐는지 확인 (naked position 방지)
    resid_pos = bg.get_position()
    resid_bal = up.get_coin_balance()
    dust = CAPITAL_KRW / exp_up_buy * 0.02   # 2% 이하는 먼지로 간주
    if resid_pos is not None or resid_bal > dust:
        raise RuntimeError(
            f"{cur} 잔여 포지션 감지! 비트겟숏={resid_pos and resid_pos.get('total')} "
            f"업비트잔고={resid_bal} — 수동 확인 필요, 이후 왕복 중단")
    return total_slip, roundtrip_krw


def live_run(capital=CAPITAL_KRW, trips=N_ROUND_TRIPS, only_coin=None):
    global CAPITAL_KRW, N_ROUND_TRIPS
    CAPITAL_KRW, N_ROUND_TRIPS = capital, trips
    if CAPITAL_KRW > MAX_KRW_PER_ORDER:
        print(f"거부: CAPITAL_KRW({CAPITAL_KRW:,}) > 안전상한({MAX_KRW_PER_ORDER:,})")
        return
    coins = [c for c in PILOT_COINS if (only_coin is None or c[2] == only_coin)]
    if not coins:
        print(f"거부: --coin {only_coin} 은 PILOT_COINS 에 없음 {[c[2] for c in PILOT_COINS]}")
        return
    print("=" * 66)
    print("  ⚠️  실주문 모드 — 실제 자금이 사용됩니다")
    print(f"  주문당 {CAPITAL_KRW:,}원 × 코인 {len(coins)}개 × 왕복 {N_ROUND_TRIPS}회")
    print(f"  대상: {[c[2] for c in coins]}")
    print("=" * 66)
    ans = input("  계속하려면 'YES' 입력: ").strip()
    if ans != "YES":
        print("  취소됨.")
        return

    usd_krw = get_usd_krw()
    print(f"\n  환율(dunamu): {usd_krw:,.1f}\n")
    summary = {}
    for market, symbol, cur, dec in coins:
        slips, pls = [], []
        for trip in range(1, N_ROUND_TRIPS + 1):
            try:
                s, p = _round_trip(market, symbol, cur, dec, usd_krw, trip)
                slips.append(s); pls.append(p)
            except Exception as e:
                print(f"  !! [{cur}] #{trip} 오류: {e}")
                break
            time.sleep(1)
        if slips:
            summary[cur] = (sum(slips) / len(slips), sum(pls) / len(pls), len(slips))

    print("\n" + "=" * 66)
    print("  파일럿 결과 요약")
    print("=" * 66)
    margin = 0.12
    for cur, (avg_slip, avg_pl, n) in summary.items():
        verdict = "✅ 수익 가능" if avg_slip < margin else "❌ 적자 — 전략 재검토"
        print(f"  {cur:6}  평균 슬리피지 {avg_slip:+.4f}%  평균 왕복손익 {avg_pl:+,.0f}원  ({n}회)  {verdict}")
    print(f"\n  기준선: 왕복 슬리피지 < 0.12% 이면 spacing 0.30% 전략 실거래 흑자 가능")
    print(f"  상세 로그: {LOG_CSV}")
    print("=" * 66)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="실주문 실행 (기본은 드라이런)")
    ap.add_argument("--coin", type=str, default=None, help="특정 코인만 (예: BSV)")
    ap.add_argument("--capital", type=int, default=CAPITAL_KRW, help="왕복당 자본(KRW)")
    ap.add_argument("--trips", type=int, default=N_ROUND_TRIPS, help="코인당 왕복 횟수")
    args = ap.parse_args()
    if args.live:
        live_run(capital=args.capital, trips=args.trips, only_coin=args.coin)
    else:
        dry_run()
