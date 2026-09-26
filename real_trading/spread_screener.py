"""
스프레드 스크리너 — 실거래 가능 코인 선별
────────────────────────────────────────────────────────────────────
배경:
      기존 코인 선정 기준은 '호가단위/가격 < 0.15%' 였다. 그러나 실제로 넘어야
      할 벽은 익절폭(spacing 0.30%)이 아니라 순마진(spacing - 수수료 = 0.12%)이다.
      기준이 느슨했던 탓에 모의매매 수익 상위 코인(BSV, AAVE)이 실제로는
      스프레드 최악 코인이었다.

측정:
      최우선호가 스프레드가 아니라 '슬롯 자본이 호가창을 먹고 들어가는 실제 비용'
      을 본다. 시장가 주문은 호가를 순차 소진하므로 자본이 클수록 체결가가 나빠진다.

      업비트 매수: asks 를 SLOT_KRW 만큼 소진한 VWAP vs mid
      업비트 매도: bids 를 SLOT_KRW 만큼 소진한 VWAP vs mid
      비트겟 진입/청산: 동일 명목가치(USDT)로 양방향 동일 계산
      왕복비용 = 위 4개 합    (수수료 0.18% 는 별도)

판정:
      왕복비용 < 0.12%  →  spacing 0.30% 전략 흑자 가능

실행:
      python -m real_trading.spread_screener              # 전체 스캔
      python -m real_trading.spread_screener --top 30     # 상위 30개만 출력
      python -m real_trading.spread_screener --slot 1000000
"""

import sys, os, time, argparse, csv
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import requests
from utils.exchange_rate import get_usd_krw

UPBIT = "https://api.upbit.com/v1"
BITGET = "https://api.bitget.com/api/v2"
TIMEOUT = 10

SLOT_KRW = 2_000_000      # 슬롯당 자본 (1억 ÷ 10코인 ÷ 5슬롯)
FEE_PCT  = 0.18           # 업비트 0.05%×2 + 비트겟 0.04%×2
SPACING  = 0.30
MARGIN   = SPACING - FEE_PCT   # 0.12%

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")


def _walk(levels, target_krw, price_idx=0, size_idx=1):
    """호가를 target_krw 만큼 소진했을 때의 VWAP. 물량 부족이면 None."""
    filled_krw = 0.0
    cost = 0.0
    for lv in levels:
        p, sz = float(lv[price_idx]), float(lv[size_idx])
        avail = p * sz
        take = min(avail, target_krw - filled_krw)
        if take <= 0:
            break
        cost += take
        filled_krw += take
        if filled_krw >= target_krw * 0.999:
            break
    if filled_krw < target_krw * 0.999:
        return None          # 호가창 전체로도 못 채움 = 유동성 부족
    # VWAP: 소진한 각 레벨의 가중평균
    filled_krw = 0.0
    qty = 0.0
    for lv in levels:
        p, sz = float(lv[price_idx]), float(lv[size_idx])
        avail = p * sz
        take = min(avail, target_krw - filled_krw)
        if take <= 0:
            break
        qty += take / p
        filled_krw += take
        if filled_krw >= target_krw * 0.999:
            break
    return filled_krw / qty


def upbit_cost(units, slot_krw):
    """업비트 왕복비용(%) 과 진단값. units = orderbook_units (30단계)"""
    asks = [(u["ask_price"], u["ask_size"]) for u in units]
    bids = [(u["bid_price"], u["bid_size"]) for u in units]
    mid = (asks[0][0] + bids[0][0]) / 2
    top_spread = (asks[0][0] - bids[0][0]) / mid * 100

    # 틱 = 인접 호가 최소 간격
    prices = sorted({float(u["ask_price"]) for u in units} | {float(u["bid_price"]) for u in units})
    gaps = [b - a for a, b in zip(prices, prices[1:]) if b > a]
    tick_pct = (min(gaps) / mid * 100) if gaps else float("nan")

    vwap_buy = _walk(asks, slot_krw)
    vwap_sell = _walk(bids, slot_krw)
    if vwap_buy is None or vwap_sell is None:
        return None, top_spread, tick_pct
    cost = (vwap_buy - mid) / mid * 100 + (mid - vwap_sell) / mid * 100
    return cost, top_spread, tick_pct


def bitget_cost(symbol, slot_krw, usd_krw):
    """비트겟 왕복비용(%). 실패 시 None."""
    try:
        r = requests.get(f"{BITGET}/mix/market/merge-depth",
                         params={"symbol": symbol, "productType": "USDT-FUTURES", "limit": "50"},
                         timeout=TIMEOUT)
        d = r.json()["data"]
        asks, bids = d["asks"], d["bids"]
        if not asks or not bids:
            return None
        mid = (float(asks[0][0]) + float(bids[0][0])) / 2
        notional_usdt = slot_krw / usd_krw
        vwap_open = _walk(bids, notional_usdt)    # 숏 진입 = 매도 = bids 소진
        vwap_close = _walk(asks, notional_usdt)   # 숏 청산 = 매수 = asks 소진
        if vwap_open is None or vwap_close is None:
            return None
        return (mid - vwap_open) / mid * 100 + (vwap_close - mid) / mid * 100
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", type=int, default=SLOT_KRW, help="슬롯당 자본(KRW)")
    ap.add_argument("--top", type=int, default=40, help="출력 개수")
    ap.add_argument("--csv", action="store_true", help="결과를 logs/screener.csv 로 저장")
    args = ap.parse_args()
    slot = args.slot

    usd_krw = get_usd_krw()
    print("=" * 78)
    print(f"  스프레드 스크리너 — 슬롯자본 {slot:,}원  환율 {usd_krw:,.1f}")
    print(f"  기준선: 왕복비용 < {MARGIN}%  (spacing {SPACING}% - 수수료 {FEE_PCT}%)")
    print(f"  측정시각: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 78)

    # 1) 업비트 KRW ∩ 비트겟 USDT-FUTURES
    krw = [m["market"] for m in requests.get(f"{UPBIT}/market/all", timeout=TIMEOUT).json()
           if m["market"].startswith("KRW-")]
    bg = {c["symbol"] for c in requests.get(f"{BITGET}/mix/market/contracts",
                                            params={"productType": "USDT-FUTURES"},
                                            timeout=TIMEOUT).json()["data"]}
    pairs = [(m, m.split("-")[1] + "USDT") for m in krw if m.split("-")[1] + "USDT" in bg]
    print(f"  후보: 업비트 KRW {len(krw)}개 ∩ 비트겟 선물 {len(bg)}개 = {len(pairs)}개\n")

    # 2) 업비트 호가 배치 조회
    rows = []
    CH = 10
    for i in range(0, len(pairs), CH):
        chunk = pairs[i:i + CH]
        try:
            obs = requests.get(f"{UPBIT}/orderbook",
                               params={"markets": ",".join(m for m, _ in chunk)},
                               timeout=TIMEOUT).json()
        except Exception as e:
            print(f"  업비트 조회 실패 {i}: {e}")
            continue
        by_market = {o["market"]: o for o in obs}
        for market, symbol in chunk:
            o = by_market.get(market)
            if not o:
                continue
            cost, top, tick = upbit_cost(o["orderbook_units"], slot)
            rows.append({"coin": market.split("-")[1], "market": market, "symbol": symbol,
                         "up_cost": cost, "top_spread": top, "tick_pct": tick})
        time.sleep(0.15)
        print(f"\r  업비트 스캔 {min(i+CH, len(pairs))}/{len(pairs)}", end="", flush=True)
    print()

    illiquid = [r for r in rows if r["up_cost"] is None]
    ok = [r for r in rows if r["up_cost"] is not None]
    print(f"  유동성 부족(호가 30단계로 {slot:,}원 미소화): {len(illiquid)}개 제외")

    # 3) 업비트만으로 이미 기준선 초과면 비트겟 볼 필요 없음
    ok.sort(key=lambda r: r["up_cost"])
    shortlist = [r for r in ok if r["up_cost"] < MARGIN]
    print(f"  업비트 왕복비용 < {MARGIN}%: {len(shortlist)}개 → 비트겟 심도 조회\n")

    for n, r in enumerate(shortlist, 1):
        r["bg_cost"] = bitget_cost(r["symbol"], slot, usd_krw)
        r["total"] = None if r["bg_cost"] is None else r["up_cost"] + r["bg_cost"]
        time.sleep(0.1)
        print(f"\r  비트겟 스캔 {n}/{len(shortlist)}", end="", flush=True)
    print("\n")

    final = [r for r in shortlist if r["total"] is not None]
    final.sort(key=lambda r: r["total"])
    passed = [r for r in final if r["total"] < MARGIN]

    print(f"  {'코인':7}{'왕복비용':>10}{'업비트':>9}{'비트겟':>9}{'틱':>8}{'최우선':>9}  판정")
    print("  " + "-" * 72)
    for r in final[:args.top]:
        v = "✅ 통과" if r["total"] < MARGIN else ("△ 경계" if r["total"] < MARGIN * 1.5 else "❌")
        print(f"  {r['coin']:7}{r['total']:>9.4f}%{r['up_cost']:>8.4f}%{r['bg_cost']:>8.4f}%"
              f"{r['tick_pct']:>7.3f}%{r['top_spread']:>8.4f}%  {v}")
    print("  " + "-" * 72)
    print(f"  통과({MARGIN}% 미만): {len(passed)}개 — {[r['coin'] for r in passed]}")

    if args.csv:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, "screener.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["dt", "coin", "total_pct", "upbit_pct", "bitget_pct", "tick_pct", "top_spread_pct"])
            for r in final:
                w.writerow([datetime.now().isoformat(), r["coin"], round(r["total"], 4),
                            round(r["up_cost"], 4), round(r["bg_cost"], 4),
                            round(r["tick_pct"], 4), round(r["top_spread"], 4)])
        print(f"  저장: {path}")
    print("=" * 78)


if __name__ == "__main__":
    main()
