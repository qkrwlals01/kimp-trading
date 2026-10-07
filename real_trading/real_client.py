"""
실거래용 클라이언트 — 주문 후 '실제 체결가'를 조회해서 반환한다.

기존 core/coin_client.py 와 차이점:
  - 주문 결과의 체결 내역(trades / fills)을 다시 조회해서
    체결 평균가·수수료·체결수량을 실측한다.
  - 이 실측값으로 슬리피지(티커가격 대비 실제 체결가 차이)를 계산할 수 있다.

주의: 이 모듈은 진짜 주문을 낸다. config/settings.py 에 실제 API 키가 있어야 한다.

⚠ 2026-10-07 확인: 이 비트겟 코드는 v2(기존 계정) 전용이다.
  - 비트겟은 2026-09-15 부터 기존 계정을 통합계정(UTA)으로 전환 중이고, UTA 키로는 v2 주문 API 를 못 쓴다.
  - close_short 의 side="buy"+tradeSide="close" 는 hedge 모드에서는 '롱 청산'이다 (v2 문서: 숏 청산은
    side="sell"+tradeSide="close"). one-way 모드에서는 tradeSide 가 무시돼 동작하지만 reduceOnly 가 없다.
  새 실거래는 real_trading/exchanges.py + live_trader.py 를 쓴다 (v3/v2, one-way/hedge 를 자동 판별).
"""

import time, hmac, hashlib, base64, json, uuid, requests
import jwt
from urllib.parse import urlencode
from config.settings import (
    UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY,
    BITGET_ACCESS_KEY, BITGET_SECRET_KEY, BITGET_PASSPHRASE,
)

UPBIT_BASE   = "https://api.upbit.com/v1"
BITGET_BASE  = "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"
TIMEOUT      = 10
FILL_POLL    = 0.4    # 체결 확인 폴링 간격(초)
FILL_TRIES   = 15     # 최대 폴링 횟수 (약 6초)


# ── 업비트 ────────────────────────────────────────────────────────
class UpbitReal:
    def __init__(self, market: str, currency: str):
        self.market   = market
        self.currency = currency

    def _auth(self, params: dict = None) -> dict:
        payload = {"access_key": UPBIT_ACCESS_KEY, "nonce": str(uuid.uuid4())}
        if params:
            qs = urlencode(params).encode()
            payload["query_hash"]     = hashlib.sha512(qs).hexdigest()
            payload["query_hash_alg"] = "SHA512"
        token = jwt.encode(payload, UPBIT_SECRET_KEY, algorithm="HS256")
        return {"Authorization": f"Bearer {token}"}

    def get_price(self) -> float:
        r = requests.get(f"{UPBIT_BASE}/ticker", params={"markets": self.market}, timeout=TIMEOUT)
        r.raise_for_status()
        return float(r.json()[0]["trade_price"])

    def get_orderbook(self) -> dict:
        """최우선 매수/매도 호가 — 스프레드 확인용"""
        r = requests.get(f"{UPBIT_BASE}/orderbook", params={"markets": self.market}, timeout=TIMEOUT)
        r.raise_for_status()
        u = r.json()[0]["orderbook_units"][0]
        return {"bid": float(u["bid_price"]), "ask": float(u["ask_price"])}

    def get_coin_balance(self) -> float:
        r = requests.get(f"{UPBIT_BASE}/accounts", headers=self._auth(), timeout=TIMEOUT)
        r.raise_for_status()
        for item in r.json():
            if item["currency"] == self.currency:
                return float(item["balance"])
        return 0.0

    def _order(self, params: dict) -> str:
        r = requests.post(f"{UPBIT_BASE}/orders", json=params,
                          headers=self._auth(params), timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()["uuid"]

    def buy_market(self, krw_amount: float) -> str:
        return self._order({"market": self.market, "side": "bid",
                            "price": str(krw_amount), "ord_type": "price"})

    def sell_market(self, volume: float) -> str:
        return self._order({"market": self.market, "side": "ask",
                            "volume": str(volume), "ord_type": "market"})

    def fetch_fill(self, order_uuid: str) -> dict:
        """체결 완료까지 대기 후 실제 체결 평균가/수량/수수료 반환"""
        for _ in range(FILL_TRIES):
            r = requests.get(f"{UPBIT_BASE}/order",
                             params={"uuid": order_uuid},
                             headers=self._auth({"uuid": order_uuid}), timeout=TIMEOUT)
            r.raise_for_status()
            o = r.json()
            trades = o.get("trades", [])
            if o.get("state") in ("done", "cancel") and trades:
                vol   = sum(float(t["volume"]) for t in trades)
                funds = sum(float(t["funds"])  for t in trades)
                avg   = funds / vol if vol else 0.0
                return {
                    "avg_price":  avg,           # 실제 체결 평균가
                    "volume":     vol,           # 체결 수량
                    "funds":      funds,         # 총 체결 금액(KRW)
                    "fee":        float(o.get("paid_fee", 0)),
                    "state":      o["state"],
                }
            time.sleep(FILL_POLL)
        raise TimeoutError(f"업비트 체결 확인 실패: {order_uuid}")


# ── 비트겟 ────────────────────────────────────────────────────────
class BitgetReal:
    def __init__(self, symbol: str):
        self.symbol = symbol

    def _sign(self, ts, method, path, body=""):
        msg = ts + method.upper() + path + body
        mac = hmac.new(BITGET_SECRET_KEY.encode(), msg.encode(), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def _headers(self, method, path, body=""):
        ts = str(int(time.time() * 1000))
        return {
            "ACCESS-KEY":        BITGET_ACCESS_KEY,
            "ACCESS-SIGN":       self._sign(ts, method, path, body),
            "ACCESS-TIMESTAMP":  ts,
            "ACCESS-PASSPHRASE": BITGET_PASSPHRASE,
            "Content-Type":      "application/json",
            "locale":            "en-US",
        }

    def _get(self, path, params=None):
        q = ("?" + urlencode(params)) if params else ""
        r = requests.get(BITGET_BASE + path + q, headers=self._headers("GET", path + q), timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        if d.get("code") != "00000":
            raise RuntimeError(f"Bitget GET 오류 [{self.symbol}]: {d}")
        return d

    def _post(self, path, body):
        s = json.dumps(body)
        r = requests.post(BITGET_BASE + path, data=s, headers=self._headers("POST", path, s), timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        if d.get("code") != "00000":
            raise RuntimeError(f"Bitget POST 오류 [{self.symbol}]: {d}")
        return d

    def get_price(self) -> float:
        d = self._get("/api/v2/mix/market/ticker", {"symbol": self.symbol, "productType": PRODUCT_TYPE})
        return float(d["data"][0]["lastPr"])

    def get_orderbook(self) -> dict:
        d = self._get("/api/v2/mix/market/merge-depth",
                      {"symbol": self.symbol, "productType": PRODUCT_TYPE, "limit": "1"})
        data = d["data"]
        return {"bid": float(data["bids"][0][0]), "ask": float(data["asks"][0][0])}

    def set_leverage(self, leverage: int, hold_side: str = "short"):
        return self._post("/api/v2/mix/account/set-leverage", {
            "symbol": self.symbol, "productType": PRODUCT_TYPE, "marginCoin": "USDT",
            "leverage": str(leverage), "holdSide": hold_side,
        })

    def open_short(self, size: float, leverage: int) -> str:
        d = self._post("/api/v2/mix/order/place-order", {
            "symbol": self.symbol, "productType": PRODUCT_TYPE,
            "marginMode": "isolated", "marginCoin": "USDT",
            "size": str(size), "side": "sell", "tradeSide": "open",
            "orderType": "market", "leverage": str(leverage),
        })
        return d["data"]["orderId"]

    def close_short(self, size: float) -> str:
        d = self._post("/api/v2/mix/order/place-order", {
            "symbol": self.symbol, "productType": PRODUCT_TYPE,
            "marginMode": "isolated", "marginCoin": "USDT",
            "size": str(size), "side": "buy", "tradeSide": "close",
            "orderType": "market",
        })
        return d["data"]["orderId"]

    def fetch_fill(self, order_id: str) -> dict:
        """체결 완료까지 대기 후 실제 체결 평균가/수량/수수료 반환"""
        for _ in range(FILL_TRIES):
            d = self._get("/api/v2/mix/order/detail",
                          {"symbol": self.symbol, "productType": PRODUCT_TYPE, "orderId": order_id})
            o = d["data"]
            if o.get("state") in ("filled", "partially_filled") and float(o.get("priceAvg", 0)) > 0:
                fee = 0.0
                for f in (o.get("feeDetail") or []):
                    try: fee += abs(float(f.get("totalFee", 0)))
                    except Exception: pass
                return {
                    "avg_price": float(o["priceAvg"]),        # 실제 체결 평균가(USDT)
                    "size":      float(o.get("baseVolume", 0)),
                    "fee_usdt":  fee,
                    "state":     o["state"],
                }
            time.sleep(FILL_POLL)
        raise TimeoutError(f"비트겟 체결 확인 실패: {order_id}")

    def get_position(self) -> dict | None:
        d = self._get("/api/v2/mix/position/single-position",
                      {"symbol": self.symbol, "productType": PRODUCT_TYPE, "marginCoin": "USDT"})
        for p in d.get("data", []):
            if p.get("holdSide") == "short" and float(p.get("total", 0)) > 0:
                return p
        return None
