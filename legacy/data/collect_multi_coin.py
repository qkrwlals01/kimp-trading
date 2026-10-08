"""
멀티 코인 김치 프리미엄 데이터 수집
BTC(기존) / ETH / XRP 시간봉 데이터 → data/kimp_{coin}_hourly.csv
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from data.collect_historical import fetch_upbit, fetch_binance, fetch_usdkrw_daily
import pandas as pd

COINS = {
    "ETH": {"upbit": "KRW-ETH", "binance": "ETHUSDT"},
    "XRP": {"upbit": "KRW-XRP", "binance": "XRPUSDT"},
}

def build_coin_series(upbit_market: str, binance_symbol: str, fx_df: pd.DataFrame) -> pd.DataFrame:
    upbit_df   = fetch_upbit(market=upbit_market, interval="1h")
    binance_df = fetch_binance(symbol=binance_symbol, interval="1h")

    df = upbit_df.merge(binance_df, on="datetime", how="inner")
    df["date"] = df["datetime"].dt.tz_localize(None).dt.normalize()
    df = df.merge(fx_df, on="date", how="left")
    df["usd_krw"] = df["usd_krw"].ffill()

    df["binance_krw"] = df["binance_close"] * df["usd_krw"]
    df["premium_pct"] = (df["upbit_close"] - df["binance_krw"]) / df["binance_krw"] * 100

    return df.sort_values("datetime").reset_index(drop=True)


if __name__ == "__main__":
    os.makedirs("data", exist_ok=True)

    coins = sys.argv[1:] if len(sys.argv) > 1 else list(COINS.keys())
    print(f"수집 코인: {coins}\n")

    print("환율 데이터 수집...")
    fx_df = fetch_usdkrw_daily()

    for coin in coins:
        if coin not in COINS:
            print(f"  {coin}: 미지원 코인 스킵")
            continue
        cfg = COINS[coin]
        print(f"\n{'='*50}")
        print(f"[ {coin} ]  업비트: {cfg['upbit']}  바이낸스: {cfg['binance']}")
        df = build_coin_series(cfg["upbit"], cfg["binance"], fx_df)
        out = f"data/kimp_{coin.lower()}_hourly.csv"
        df.to_csv(out, index=False, encoding="utf-8-sig")
        print(f"기간: {df['datetime'].min().date()} ~ {df['datetime'].max().date()}  ({len(df):,}개)")
        print(f"프리미엄 — 평균: {df['premium_pct'].mean():+.2f}%  범위: {df['premium_pct'].min():+.2f}%~{df['premium_pct'].max():+.2f}%")
        print(f"저장: {out}")
