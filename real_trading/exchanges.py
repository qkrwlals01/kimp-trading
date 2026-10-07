"""
실거래 거래소 클라이언트 — 빗썸(API 2.0, 원화 현물) + 비트겟(USDT 선물)
────────────────────────────────────────────────────────────────────
주의: 주문 함수(place / open_short / close_short / cancel)는 진짜 주문을 낸다.
      real_trading/live_trader.py 가 --live 일 때만 부른다. 드라이런은 이 모듈의 공개 API 만 쓴다.

빗썸 API 2.0 (https://apidocs.bithumb.com)
  인증  JWT HS256 {access_key, nonce, timestamp, query_hash(SHA512), query_hash_alg}
  호가  GET  /v1/orderbook?markets=KRW-XRP,...        (공개, 업비트와 같은 형식)
  잔고  GET  /v1/accounts
  수수료 GET /v1/orders/chance?market=KRW-XRP         (bid_fee / ask_fee — 할인 요율이면 0.0004)
  주문  POST /v2/orders  {market, side, order_type=limit, price, volume, time_in_force=ioc, client_order_id}
  조회  GET  /v1/order?uuid=                           (state, executed_volume, paid_fee, trades[])
  취소  DELETE /v2/order?order_id=

비트겟 USDT 선물 — 2026-09-15 부터 기존(Classic) 계정을 통합계정(UTA)으로 자동 전환 중이고,
UTA 키로는 v2 주문 API 를 못 쓴다. 그래서 시작할 때 v3 계정 설정 조회가 되면 v3, 아니면 v2 를 쓴다.
포지션 모드(one-way / hedge)에 따라 숏을 여닫는 값이 다르다 (공식 문서 확인, 2026-10-07):
                     열기                         닫기
  v3 one-way   side=sell                    side=buy, reduceOnly=yes
  v3 hedge     side=sell, posSide=short     side=buy, posSide=short
  v2 one-way   side=sell                    side=buy, reduceOnly=YES
  v2 hedge     side=sell, tradeSide=open    side=sell, tradeSide=close
"""

import time, hmac, json, uuid, base64, hashlib, requests
from urllib.parse import urlencode

TIMEOUT = 10
BG_PRODUCT = "USDT-FUTURES"


class ExchangeError(RuntimeError):
    pass


def load_keys() -> dict:
    """config/settings.py 의 키. 없으면 빈 값 — 드라이런과 공개 API 는 키 없이 동작한다."""
    try:
        from config import settings as s
    except ImportError:
        s = None

    def g(name):
        return getattr(s, name, "") if s else ""
    return {"bithumb": (g("BITHUMB_ACCESS_KEY"), g("BITHUMB_SECRET_KEY")),
            "bitget": (g("BITGET_ACCESS_KEY"), g("BITGET_SECRET_KEY"), g("BITGET_PASSPHRASE"))}


def floor_step(x: float, step: float) -> float:
    """수량을 거래소 단위로 내림 (부동소수 오차를 피하려고 정수 배수로 계산)"""
    n = int((x + step * 1e-9) / step)
    return round(n * step, 10)


# ── 빗썸 ──────────────────────────────────────────────────────────────

class Bithumb:
    BASE = "https://api.bithumb.com"

    def __init__(self, access_key: str = "", secret_key: str = ""):
        self.ak, self.sk = access_key, secret_key

    # 공개
    def orderbooks(self, markets: list) -> dict:
        """{market: {"units": [(bid, bid_sz, ask, ask_sz), ...15단계], "ts": ms}}"""
        r = requests.get(f"{self.BASE}/v1/orderbook", params={"markets": ",".join(markets)}, timeout=TIMEOUT)
        r.raise_for_status()
        out = {}
        for o in r.json():
            units = [(float(u["bid_price"]), float(u["bid_size"]), float(u["ask_price"]), float(u["ask_size"]))
                     for u in o["orderbook_units"]]
            out[o["market"]] = {"units": units, "ts": int(o.get("timestamp") or 0)}
        return out

    # 인증
    def _headers(self, params: dict = None) -> dict:
        if not (self.ak and self.sk):
            raise ExchangeError("빗썸 API 키가 없습니다 — config/settings.py 에 BITHUMB_ACCESS_KEY, BITHUMB_SECRET_KEY")
        import jwt
        payload = {"access_key": self.ak, "nonce": str(uuid.uuid4()), "timestamp": round(time.time() * 1000)}
        if params:
            payload["query_hash"] = hashlib.sha512(urlencode(params).encode()).hexdigest()
            payload["query_hash_alg"] = "SHA512"
        return {"Authorization": f"Bearer {jwt.encode(payload, self.sk, algorithm='HS256')}"}

    def _req(self, method: str, path: str, params: dict = None, body: dict = None):
        h = self._headers(body if body is not None else params)
        if body is not None:
            h["Content-Type"] = "application/json"
        r = requests.request(method, self.BASE + path, params=params,
                             data=json.dumps(body) if body is not None else None, headers=h, timeout=TIMEOUT)
        try:
            d = r.json()
        except ValueError:
            d = {"raw": r.text[:300]}
        if r.status_code >= 400 or (isinstance(d, dict) and "error" in d):
            raise ExchangeError(f"빗썸 {method} {path} → {r.status_code} {d}")
        return d

    def accounts(self) -> dict:
        """{통화: (주문 가능, 주문 중 묶임)}"""
        return {a["currency"]: (float(a["balance"]), float(a["locked"])) for a in self._req("GET", "/v1/accounts")}

    def chance(self, market: str) -> dict:
        return self._req("GET", "/v1/orders/chance", params={"market": market})

    def place_ioc(self, market: str, side: str, volume: float, price: float, client_id: str) -> str:
        """즉시 체결 지정가(IOC): price 까지만 체결되고 남은 수량은 바로 취소된다. side: bid 매수 / ask 매도"""
        body = {"market": market, "side": side, "order_type": "limit",
                "price": f"{price:.8f}".rstrip("0").rstrip("."), "volume": f"{volume:.8f}".rstrip("0").rstrip("."),
                "time_in_force": "ioc", "client_order_id": client_id}
        d = self._req("POST", "/v2/orders", body=body)
        return d.get("order_id") or d.get("uuid")

    def order(self, order_id: str) -> dict:
        return self._req("GET", "/v1/order", params={"uuid": order_id})

    def cancel(self, order_id: str):
        return self._req("DELETE", "/v2/order", params={"order_id": order_id})

    def wait_fill(self, order_id: str, timeout: float) -> dict:
        """주문이 끝날 때까지(IOC 는 곧바로 done/cancel) 기다려 실제 체결을 돌려준다.
        {qty, avg, funds(원), fee(원), state}"""
        t0 = time.time()
        while True:
            o = self.order(order_id)
            if o.get("state") in ("done", "cancel") or time.time() - t0 > timeout:
                if o.get("state") == "wait":                  # IOC 인데 남아 있으면 취소하고 다시 본다
                    try:
                        self.cancel(order_id)
                    except ExchangeError:
                        pass
                    time.sleep(0.5)
                    o = self.order(order_id)
                trades = o.get("trades") or []
                qty = sum(float(t["volume"]) for t in trades) if trades else float(o.get("executed_volume") or 0)
                funds = sum(float(t["funds"]) for t in trades) if trades else qty * float(o.get("price") or 0)
                return {"qty": qty, "avg": funds / qty if qty else 0.0, "funds": funds,
                        "fee": float(o.get("paid_fee") or 0), "state": o.get("state"), "id": order_id}
            time.sleep(0.3)


# ── 비트겟 USDT 선물 ───────────────────────────────────────────────────

class BitgetFutures:
    BASE = "https://api.bitget.com"

    def __init__(self, access_key: str = "", secret_key: str = "", passphrase: str = "",
                 margin_mode: str = "crossed"):
        self.ak, self.sk, self.pp = access_key, secret_key, passphrase
        self.margin_mode = margin_mode
        self.api = None          # "v3" 통합계정 / "v2" 기존계정 — detect() 가 정한다
        self.hold = None         # "one_way" / "hedge"

    # 공개 (v2 시세 API 는 계정 종류와 상관없이 동작)
    @classmethod
    def contracts(cls, symbols: list) -> dict:
        """{symbol: {"step": 수량 단위, "min_qty", "min_usdt"}}"""
        r = requests.get(f"{cls.BASE}/api/v2/mix/market/contracts", params={"productType": BG_PRODUCT}, timeout=TIMEOUT)
        r.raise_for_status()
        out = {}
        for c in r.json()["data"]:
            if c["symbol"] in symbols:
                out[c["symbol"]] = {"step": float(c["sizeMultiplier"]), "min_qty": float(c["minTradeNum"]),
                                    "min_usdt": float(c.get("minTradeUSDT") or 5)}
        return out

    # 인증 (v2·v3 서명 방식 같음)
    def _req(self, method: str, path: str, params: dict = None, body: dict = None) -> dict:
        if not (self.ak and self.sk and self.pp):
            raise ExchangeError("비트겟 API 키가 없습니다 — config/settings.py 의 BITGET_* 확인")
        q = ("?" + urlencode(params)) if params else ""
        b = json.dumps(body) if body is not None else ""
        ts = str(int(time.time() * 1000))
        sign = base64.b64encode(hmac.new(self.sk.encode(), (ts + method.upper() + path + q + b).encode(),
                                         hashlib.sha256).digest()).decode()
        h = {"ACCESS-KEY": self.ak, "ACCESS-SIGN": sign, "ACCESS-TIMESTAMP": ts, "ACCESS-PASSPHRASE": self.pp,
             "Content-Type": "application/json", "locale": "en-US"}
        r = requests.request(method, self.BASE + path + q, data=b or None, headers=h, timeout=TIMEOUT)
        try:
            d = r.json()
        except ValueError:
            raise ExchangeError(f"비트겟 {method} {path} → {r.status_code} {r.text[:300]}")
        if d.get("code") != "00000":
            raise ExchangeError(f"비트겟 {method} {path} → {r.status_code} {d.get('code')} {d.get('msg')}")
        return d

    def detect(self, probe_symbol: str) -> dict:
        """통합계정(v3)인지 기존계정(v2)인지, 포지션 모드는 무엇인지 — 읽기 전용 조회로 정한다."""
        try:
            d = self._req("GET", "/api/v3/account/settings").get("data") or {}
            self.api = "v3"
            hm = str(d.get("holdMode") or "")
        except ExchangeError as e3:
            d = self._req("GET", "/api/v2/mix/account/account",
                          params={"symbol": probe_symbol, "productType": BG_PRODUCT, "marginCoin": "USDT"}).get("data") or {}
            self.api = "v2"
            hm = str(d.get("posMode") or "")
            d = dict(d, v3_error=str(e3)[:200])
        self.hold = "hedge" if "hedge" in hm else "one_way" if ("one" in hm or "single" in hm) else None
        return {"api": self.api, "hold": self.hold, "hold_raw": hm, "raw": d}

    def _need_mode(self):
        if self.api is None or self.hold is None:
            raise ExchangeError("비트겟 계정 종류·포지션 모드를 모름 — detect() 결과를 확인하세요")

    def set_leverage(self, symbol: str, leverage: int):
        self._need_mode()
        if self.api == "v3":
            return self._req("POST", "/api/v3/account/set-leverage",
                             body={"category": BG_PRODUCT, "symbol": symbol, "leverage": str(leverage)})
        return self._req("POST", "/api/v2/mix/account/set-leverage",
                         body={"symbol": symbol, "productType": BG_PRODUCT, "marginCoin": "USDT", "leverage": str(leverage)})

    def _order(self, symbol: str, qty: float, close: bool, client_id: str) -> str:
        """시장가로 숏을 열거나(close=False) 닫는다(close=True). 반환: orderId"""
        self._need_mode()
        q = f"{qty:.8f}".rstrip("0").rstrip(".")
        if self.api == "v3":
            b = {"category": BG_PRODUCT, "symbol": symbol, "qty": q, "side": "buy" if close else "sell",
                 "orderType": "market", "clientOid": client_id, "marginMode": self.margin_mode}
            if self.hold == "hedge":
                b["posSide"] = "short"
            elif close:
                b["reduceOnly"] = "yes"
            path = "/api/v3/trade/place-order"
        else:
            b = {"symbol": symbol, "productType": BG_PRODUCT, "marginMode": self.margin_mode, "marginCoin": "USDT",
                 "size": q, "side": "buy" if close else "sell", "orderType": "market", "clientOid": client_id}
            if self.hold == "hedge":
                b["side"], b["tradeSide"] = "sell", ("close" if close else "open")
            elif close:
                b["reduceOnly"] = "YES"
            path = "/api/v2/mix/order/place-order"
        return str(self._req("POST", path, body=b)["data"]["orderId"])

    def open_short(self, symbol: str, qty: float, client_id: str) -> str:
        return self._order(symbol, qty, False, client_id)

    def close_short(self, symbol: str, qty: float, client_id: str) -> str:
        return self._order(symbol, qty, True, client_id)

    def wait_fill(self, symbol: str, order_id: str, want_qty: float, timeout: float) -> dict:
        """체결 내역을 모아 실제 체결 수량·평균가·수수료(USDT, 정가 — 환급 전)를 돌려준다."""
        t0, last = time.time(), None
        while True:
            if self.api == "v3":
                d = self._req("GET", "/api/v3/trade/fills", params={"category": BG_PRODUCT, "orderId": order_id})
                rows = [(float(x["execQty"]), float(x["execPrice"]),
                         sum(abs(float(f.get("fee") or 0)) for f in (x.get("feeDetail") or [])))
                        for x in ((d.get("data") or {}).get("list") or [])]
            else:
                d = self._req("GET", "/api/v2/mix/order/fills",
                              params={"symbol": symbol, "productType": BG_PRODUCT, "orderId": order_id})
                rows = [(float(x["baseVolume"]), float(x["price"]),
                         sum(abs(float(f.get("totalFee") or 0)) for f in (x.get("feeDetail") or [])))
                        for x in ((d.get("data") or {}).get("fillList") or [])]
            qty = sum(r[0] for r in rows)
            if rows:
                last = {"qty": qty, "avg": sum(r[0] * r[1] for r in rows) / qty, "fee": sum(r[2] for r in rows),
                        "id": order_id}
            if (last and qty >= want_qty * 0.999) or time.time() - t0 > timeout:
                if last is None:
                    raise ExchangeError(f"비트겟 체결 내역 없음 (주문 {order_id})")
                return last
            time.sleep(0.3)

    def short_size(self, symbol: str) -> float:
        """지금 열린 숏 수량. 응답 형식이 계정 종류마다 달라 여러 이름을 본다."""
        self._need_mode()
        if self.api == "v3":
            d = self._req("GET", "/api/v3/position/current-position", params={"category": BG_PRODUCT, "symbol": symbol})
        else:
            d = self._req("GET", "/api/v2/mix/position/single-position",
                          params={"symbol": symbol, "productType": BG_PRODUCT, "marginCoin": "USDT"})
        data = d.get("data")
        items = data if isinstance(data, list) else ((data or {}).get("list") or [])
        total = 0.0
        for p in items:
            side = str(p.get("posSide") or p.get("holdSide") or "").lower()
            size = float(p.get("total") or p.get("size") or p.get("qty") or 0)
            if side == "short":
                total += size
            elif side in ("", "net") and size < 0:              # one-way 에서 부호로 주는 경우
                total += -size
        return total

    def usdt_available(self):
        """선물 주문에 쓸 수 있는 USDT (형식을 모르면 None)"""
        try:
            if self.api == "v3":
                d = self._req("GET", "/api/v3/account/assets").get("data") or {}
                for k in ("unionAvailable", "available", "usdtAvailable"):
                    if k in d:
                        return float(d[k])
                for a in d.get("assets") or []:
                    if a.get("coin") == "USDT":
                        return float(a.get("available") or 0)
                return None
            d = self._req("GET", "/api/v2/mix/account/accounts", params={"productType": BG_PRODUCT}).get("data") or []
            for a in d:
                if a.get("marginCoin") == "USDT":
                    return float(a.get("crossedMaxAvailable") or a.get("available") or 0)
        except (ExchangeError, ValueError, TypeError):
            return None
        return None
