"""
코인별 파라미터를 받는 범용 업비트/비트겟 클라이언트
멀티코인 트레이더에서 사용
"""

import hmac, hashlib, base64, time, json, uuid, requests
import jwt
from urllib.parse import urlencode
from config.settings import (
    UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY,
    BITGET_ACCESS_KEY, BITGET_SECRET_KEY, BITGET_PASSPHRASE,
    BITGET_PRODUCT_TYPE,
)

UPBIT_BASE  = "https://api.upbit.com/v1"
BITGET_BASE = "https://api.bitget.com"
TIMEOUT     = 10   # 모든 API 요청 타임아웃 (초) — 초과 시 예외 발생, 다음 폴링에서 재시도


# ── 업비트 ────────────────────────────────────────────────────────

class UpbitCoin:
    def __init__(self, market: str, currency: str):
        self.market   = market
        self.currency = currency

    def _auth(self, query_params: dict = None) -> dict:
        payload = {"access_key": UPBIT_ACCESS_KEY, "nonce": str(uuid.uuid4())}
        if query_params:
            qs = urlencode(query_params).encode()
            m  = hashlib.sha512(); m.update(qs)
            payload["query_hash"]     = m.hexdigest()
            payload["query_hash_alg"] = "SHA512"
        token = jwt.encode(payload, UPBIT_SECRET_KEY, algorithm="HS256")
        return {"Authorization": f"Bearer {token}"}

    def get_price(self) -> float:
        resp = requests.get(f"{UPBIT_BASE}/ticker", params={"markets": self.market},
                            timeout=TIMEOUT)
        resp.raise_for_status()
        return float(resp.json()[0]["trade_price"])

    def get_coin_balance(self) -> float:
        resp = requests.get(f"{UPBIT_BASE}/accounts", headers=self._auth(),
                            timeout=TIMEOUT)
        resp.raise_for_status()
        for item in resp.json():
            if item["currency"] == self.currency:
                return float(item["balance"])
        return 0.0

    def buy_market(self, krw_amount: float) -> dict:
        params = {"market": self.market, "side": "bid",
                  "price": str(krw_amount), "ord_type": "price"}
        resp = requests.post(f"{UPBIT_BASE}/orders", json=params,
                             headers=self._auth(params), timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def sell_market(self, volume: float) -> dict:
        params = {"market": self.market, "side": "ask",
                  "volume": str(volume), "ord_type": "market"}
        resp = requests.post(f"{UPBIT_BASE}/orders", json=params,
                             headers=self._auth(params), timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()


# ── 비트겟 ────────────────────────────────────────────────────────

class BitgetCoin:
    def __init__(self, symbol: str):
        self.symbol = symbol

    def _sign(self, ts: str, method: str, path: str, body: str = "") -> str:
        msg = ts + method.upper() + path + body
        mac = hmac.new(BITGET_SECRET_KEY.encode(), msg.encode(), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def _headers(self, method: str, path: str, body: str = "") -> dict:
        ts = str(int(time.time() * 1000))
        return {
            "ACCESS-KEY":        BITGET_ACCESS_KEY,
            "ACCESS-SIGN":       self._sign(ts, method, path, body),
            "ACCESS-TIMESTAMP":  ts,
            "ACCESS-PASSPHRASE": BITGET_PASSPHRASE,
            "Content-Type":      "application/json",
            "locale":            "en-US",
        }

    def _get(self, path: str, params: dict = None) -> dict:
        from urllib.parse import urlencode as ue
        q    = ("?" + ue(params)) if params else ""
        resp = requests.get(BITGET_BASE + path + q,
                            headers=self._headers("GET", path + q), timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "00000":
            raise RuntimeError(f"Bitget 오류 [{self.symbol}]: {data}")
        return data

    def _post(self, path: str, body: dict) -> dict:
        body_str = json.dumps(body)
        resp = requests.post(BITGET_BASE + path, data=body_str,
                             headers=self._headers("POST", path, body_str),
                             timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "00000":
            raise RuntimeError(f"Bitget 오류 [{self.symbol}]: {data}")
        return data

    def get_price(self) -> float:
        data = self._get("/api/v2/mix/market/ticker",
                         {"symbol": self.symbol, "productType": BITGET_PRODUCT_TYPE})
        return float(data["data"][0]["lastPr"])

    def set_leverage(self, leverage: int) -> dict:
        return self._post("/api/v2/mix/account/set-leverage", {
            "symbol": self.symbol, "productType": BITGET_PRODUCT_TYPE,
            "marginCoin": "USDT", "leverage": str(leverage), "holdSide": "short",
        })

    def open_short(self, usdt_margin: float, leverage: int) -> dict:
        price = self.get_price()
        size  = round((usdt_margin * leverage) / price, 4)
        return self._post("/api/v2/mix/order/place-order", {
            "symbol": self.symbol, "productType": BITGET_PRODUCT_TYPE,
            "marginMode": "isolated", "marginCoin": "USDT",
            "size": str(size), "side": "sell", "tradeSide": "open",
            "orderType": "market", "leverage": str(leverage),
        })

    def close_short(self, size: float) -> dict:
        return self._post("/api/v2/mix/order/place-order", {
            "symbol": self.symbol, "productType": BITGET_PRODUCT_TYPE,
            "marginMode": "isolated", "marginCoin": "USDT",
            "size": str(size), "side": "buy", "tradeSide": "close",
            "orderType": "market",
        })

    def get_position(self) -> dict | None:
        data = self._get("/api/v2/mix/position/single-position", {
            "symbol": self.symbol, "productType": BITGET_PRODUCT_TYPE,
            "marginCoin": "USDT",
        })
        for pos in data.get("data", []):
            if pos.get("holdSide") == "short" and float(pos.get("total", 0)) > 0:
                return pos
        return None
