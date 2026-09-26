"""
후보 코인 김프 데이터 수집 (분봉)
────────────────────────────────────────────────────────────────────
기존 collect_multi_coin.py 와의 차이:
  - 해외거래소: 바이낸스 → **비트겟** (실제 체결 거래소와 일치)
  - 해상도: 1시간봉 → **1분봉** (봇 폴링 10초. 시간봉은 0.3% 교차를 대량 누락)
  - 코인: BTC/ETH/XRP 3개 → 스크리너 후보 전체

환율 주의:
  분봉 단위 과거 환율은 공개 API가 없다.
  ⚠️ 초기 버전은 '현재 환율 상수'를 썼다가 결과를 망쳤다. 환율이 분 단위로는
     거의 안 변하는 건 맞지만, 2026-07 한 달에만 -128원(-8.3%) 움직였다.
     상수 환율은 이 표류를 통째로 김프 변동으로 둔갑시켜, 실제로는 상승했던
     구간을 하락 국면으로 뒤집어 보이게 만든다.
  → 일별 환율(yfinance KRW=X)을 받아 분 단위로 선형보간해 쓴다.
     이 소스는 국내 고시 대비 약 -13원(≈0.9%) 편향이 있어 김프 '절대 수준'은
     그만큼 어긋난다. 편향이 거의 일정하므로 '변화'와 교차 횟수는 유효하다.
  → 절대 수익률 산출에는 여전히 쓰지 말 것.

실행:
      python -m data.collect_candidates --days 14
      python -m data.collect_candidates --days 7 --coins BTC ETH SOL
"""

import sys, os, time, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from utils.exchange_rate import get_usd_krw


def fetch_fx_minutely(start, end) -> pd.Series:
    """일별 환율을 받아 1분 단위로 선형보간. 인덱스는 tz-aware UTC."""
    import yfinance as yf
    lo = (start - timedelta(days=7)).strftime("%Y-%m-%d")
    hi = (end + timedelta(days=2)).strftime("%Y-%m-%d")
    d = yf.Ticker("KRW=X").history(start=lo, end=hi, interval="1d").reset_index()
    d["datetime"] = pd.to_datetime(d["Date"], utc=True).dt.normalize()
    ser = d.set_index("datetime")["Close"].astype(float).sort_index()
    grid = pd.date_range(ser.index.min(), end, freq="1min", tz="UTC")
    return ser.reindex(ser.index.union(grid)).interpolate("time").reindex(grid)

OUT_DIR = os.path.join(os.path.dirname(__file__), "candidates")

# 스크리너 통과 7개 + 현재 운영 중 비교군 5개
DEFAULT_COINS = ["BTC", "ETH", "XRP", "SOL", "AVAX", "LINK", "DOGE",
                 "BSV", "ATOM", "AAVE", "TAO", "SUI"]


def fetch_upbit_1m(market: str, days: int) -> pd.DataFrame:
    """업비트 1분봉. 200개씩 역방향 페이징."""
    need = days * 24 * 60
    rows, to = [], None
    while len(rows) < need:
        p = {"market": market, "count": 200}
        if to:
            p["to"] = to
        for attempt in range(5):
            r = requests.get("https://api.upbit.com/v1/candles/minutes/1", params=p, timeout=10)
            if r.status_code == 429:
                time.sleep(1.0)
                continue
            r.raise_for_status()
            break
        c = r.json()
        if not c:
            break
        rows.extend(c)
        to = c[-1]["candle_date_time_utc"]
        time.sleep(0.12)
    df = pd.DataFrame(rows)[["candle_date_time_utc", "trade_price"]]
    df.columns = ["datetime", "upbit_close"]
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    return df.drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)


def fetch_bitget_1m(symbol: str, days: int) -> pd.DataFrame:
    """비트겟 1분봉. 1000개씩 endTime 페이징."""
    need = days * 24 * 60
    rows, end = [], None
    while len(rows) < need:
        p = {"symbol": symbol, "productType": "USDT-FUTURES",
             "granularity": "1m", "limit": "1000"}
        if end:
            p["endTime"] = str(end)
        r = requests.get("https://api.bitget.com/api/v2/mix/market/candles",
                         params=p, timeout=10)
        r.raise_for_status()
        d = r.json().get("data") or []
        if not d:
            break
        rows.extend(d)
        end = int(d[0][0])
        time.sleep(0.12)
    df = pd.DataFrame([(int(x[0]), float(x[4])) for x in rows],
                      columns=["ms", "bitget_close"])
    df["datetime"] = pd.to_datetime(df["ms"], unit="ms", utc=True)
    return df[["datetime", "bitget_close"]].drop_duplicates("datetime") \
             .sort_values("datetime").reset_index(drop=True)


def build(coin: str, days: int, fx: pd.Series) -> pd.DataFrame | None:
    """
    거래가 없는 분에는 봉이 생성되지 않는다. inner join 으로 합치면
    유동성 낮은 코인일수록 표본이 줄어 체결빈도 비교가 왜곡된다.
    → 1분 그리드로 재색인 후 ffill. '봉 없음 = 직전가 유지' 로 해석한다.
    """
    up = fetch_upbit_1m(f"KRW-{coin}", days)
    bg = fetch_bitget_1m(f"{coin}USDT", days)
    if up.empty or bg.empty:
        return None

    lo = max(up["datetime"].min(), bg["datetime"].min())
    hi = min(up["datetime"].max(), bg["datetime"].max())
    if lo >= hi:
        return None
    grid = pd.date_range(lo, hi, freq="1min")

    up = up.set_index("datetime").reindex(grid).ffill()
    bg = bg.set_index("datetime").reindex(grid).ffill()
    df = pd.concat([up, bg], axis=1).dropna()
    df.index.name = "datetime"
    df = df.reset_index()
    if df.empty:
        return None
    df["usd_krw"] = fx.reindex(df["datetime"]).to_numpy()
    df["usd_krw"] = pd.Series(df["usd_krw"]).ffill().bfill()
    df["bitget_krw"] = df["bitget_close"] * df["usd_krw"]
    df["premium_pct"] = (df["upbit_close"] - df["bitget_krw"]) / df["bitget_krw"] * 100
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--coins", nargs="*", default=DEFAULT_COINS)
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    now = datetime.now(timezone.utc)
    fx = fetch_fx_minutely(now - timedelta(days=args.days + 2), now)
    print(f"수집 시작 {datetime.now():%H:%M:%S} | {args.days}일 1분봉 | "
          f"환율 일별보간 {fx.iloc[0]:,.1f} → {fx.iloc[-1]:,.1f}")
    print(f"대상 {len(args.coins)}개: {args.coins}\n")

    for i, coin in enumerate(args.coins, 1):
        try:
            df = build(coin, args.days, fx)
            if df is None or df.empty:
                print(f"  [{i}/{len(args.coins)}] {coin:6} 데이터 없음 — 스킵")
                continue
            out = os.path.join(OUT_DIR, f"{coin.lower()}_1m.csv")
            df.to_csv(out, index=False, encoding="utf-8-sig")
            span = (df['datetime'].max() - df['datetime'].min())
            print(f"  [{i}/{len(args.coins)}] {coin:6} {len(df):>6,}행  "
                  f"{span.days}일{span.seconds//3600}시간  "
                  f"김프 {df['premium_pct'].min():+.2f}~{df['premium_pct'].max():+.2f}%")
        except Exception as e:
            print(f"  [{i}/{len(args.coins)}] {coin:6} 실패: {e}")
    print(f"\n저장 위치: {OUT_DIR}")


if __name__ == "__main__":
    main()
