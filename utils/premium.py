from core.upbit_client import get_btc_price as upbit_price
from core.bitget_client import get_btc_price as bitget_price
from utils.exchange_rate import get_usd_krw


def get_kimchi_premium() -> dict:
    usd_krw = get_usd_krw()
    upbit_krw = upbit_price()
    bitget_usdt = bitget_price()
    bitget_krw = bitget_usdt * usd_krw
    premium_pct = upbit_krw / (bitget_usdt * usd_krw) * 100 - 100
    return {
        "upbit_krw":    upbit_krw,
        "bitget_usdt":  bitget_usdt,
        "bitget_krw":   bitget_krw,
        "usd_krw":      usd_krw,
        "premium_pct":  premium_pct,
    }
