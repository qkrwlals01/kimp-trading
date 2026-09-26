"""
과거 김치 프리미엄 시계열 데이터 수집
- 업비트  BTC/KRW 캔들 (일봉 / 1시간봉)
- 바이낸스 BTC/USDT 캔들 (일봉 / 1시간봉) — 공개 API, 인증 불필요
- USD/KRW 환율 (Yahoo Finance 일봉 → 시간봉 forward fill)
"""

import time
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime, timezone, timedelta


# ── 업비트 캔들 수집 ────────────────────────────────────────────

def fetch_upbit(market: str = "KRW-BTC", interval: str = "1h") -> pd.DataFrame:
    """
    interval: "1d" | "1h"
    """
    if interval == "1d":
        url = "https://api.upbit.com/v1/candles/days"
        date_key = "candle_date_time_utc"
        stop_date = "2017-09-24"
    else:
        url = "https://api.upbit.com/v1/candles/minutes/60"
        date_key = "candle_date_time_utc"
        stop_date = "2017-09-24T00:00:00"

    all_candles = []
    to = None
    label = "일봉" if interval == "1d" else "1시간봉"
    print(f"업비트 {label} 수집 중...")

    while True:
        params = {"market": market, "count": 200}
        if to:
            params["to"] = to

        resp = requests.get(url, params=params)
        resp.raise_for_status()
        candles = resp.json()

        if not candles:
            break

        all_candles.extend(candles)
        oldest = candles[-1][date_key]

        if len(all_candles) % 10000 < 200:
            print(f"  {oldest[:16]} ~ {candles[0][date_key][:16]}  ({len(all_candles):,}개)")

        if oldest[:len(stop_date)] <= stop_date:
            break

        to = oldest
        time.sleep(0.12)

    df = pd.DataFrame(all_candles)
    df["datetime"] = pd.to_datetime(df[date_key], utc=True)
    df = df.rename(columns={"trade_price": "upbit_close"})
    df = df[["datetime", "upbit_close"]].drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)
    print(f"  완료: {len(df):,}개")
    return df


# ── 바이낸스 캔들 수집 ──────────────────────────────────────────

def fetch_binance(symbol: str = "BTCUSDT", interval: str = "1h") -> pd.DataFrame:
    url = "https://api.binance.com/api/v3/klines"
    all_candles = []

    start_dt = datetime(2017, 9, 24, tzinfo=timezone.utc)
    end_dt   = datetime.now(timezone.utc)

    label = "일봉" if interval == "1d" else "1시간봉"
    print(f"바이낸스 {label} 수집 중...")

    cursor = start_dt
    while cursor < end_dt:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": int(cursor.timestamp() * 1000),
            "limit": 1000,
        }
        resp = requests.get(url, params=params)
        resp.raise_for_status()
        candles = resp.json()

        if not candles:
            break

        all_candles.extend(candles)
        last_ts = candles[-1][0]
        last_dt = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc)

        if len(all_candles) % 10000 < 1000:
            print(f"  {cursor.strftime('%Y-%m-%d %H:%M')} ~ {last_dt.strftime('%Y-%m-%d %H:%M')}  ({len(all_candles):,}개)")

        cursor = last_dt + timedelta(hours=1 if interval == "1h" else 1)
        time.sleep(0.05)

    df = pd.DataFrame(all_candles, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    df["datetime"] = pd.to_datetime(df["open_time"].astype(float), unit="ms", utc=True)
    df["binance_close"] = df["close"].astype(float)
    df = df[["datetime", "binance_close"]].drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)
    print(f"  완료: {len(df):,}개")
    return df


# ── USD/KRW 환율 수집 ────────────────────────────────────────────

def fetch_usdkrw_daily() -> pd.DataFrame:
    print("USD/KRW 환율 수집 중...")
    ticker = yf.Ticker("KRW=X")
    df = ticker.history(period="max", interval="1d")
    df = df.reset_index()
    df["date"] = pd.to_datetime(df["Date"]).dt.tz_localize(None).dt.normalize()
    df["usd_krw"] = df["Close"].astype(float)
    df = df[["date", "usd_krw"]].dropna().drop_duplicates("date").sort_values("date").reset_index(drop=True)
    print(f"  수집: {df['date'].min().date()} ~ {df['date'].max().date()} ({len(df):,}개)")
    return df


# ── 합치기 및 김치 프리미엄 계산 ────────────────────────────────

def build_premium_series(interval: str = "1h") -> pd.DataFrame:
    upbit_df   = fetch_upbit(interval=interval)
    binance_df = fetch_binance(interval=interval)
    fx_df      = fetch_usdkrw_daily()

    # 시간봉 병합
    df = upbit_df.merge(binance_df, on="datetime", how="inner")

    # 환율: 날짜 기준으로 join 후 forward fill
    df["date"] = df["datetime"].dt.tz_localize(None).dt.normalize()
    df = df.merge(fx_df, on="date", how="left")
    df["usd_krw"] = df["usd_krw"].ffill()

    df["binance_krw"] = df["binance_close"] * df["usd_krw"]
    df["premium_pct"] = (df["upbit_close"] - df["binance_krw"]) / df["binance_krw"] * 100

    df = df.sort_values("datetime").reset_index(drop=True)
    return df


if __name__ == "__main__":
    import os, sys
    os.makedirs("data", exist_ok=True)

    interval = sys.argv[1] if len(sys.argv) > 1 else "1h"
    assert interval in ("1d", "1h"), "interval은 '1d' 또는 '1h'"

    df = build_premium_series(interval=interval)

    label = "hourly" if interval == "1h" else "daily"
    out_path = f"data/kimp_history_{label}.csv"
    df.to_csv(out_path, index=False, encoding="utf-8-sig")

    print(f"\n=== 수집 완료 ({interval}) ===")
    print(f"기간: {df['datetime'].min()} ~ {df['datetime'].max()}")
    print(f"총 {len(df):,}개")
    print(f"\n김치 프리미엄 통계:")
    print(f"  평균:    {df['premium_pct'].mean():.2f}%")
    print(f"  중앙값:  {df['premium_pct'].median():.2f}%")
    print(f"  최솟값:  {df['premium_pct'].min():.2f}%  ({df.loc[df['premium_pct'].idxmin(), 'datetime']})")
    print(f"  최댓값:  {df['premium_pct'].max():.2f}%  ({df.loc[df['premium_pct'].idxmax(), 'datetime']})")
    print(f"  표준편차: {df['premium_pct'].std():.2f}%")
    print(f"\n저장: {out_path}")
