import requests
import time
import logging

logger = logging.getLogger(__name__)

_cache = {"rate": None, "ts": 0}
CACHE_TTL = 60  # 1분 캐시

# ── 환율 API 우선순위 ─────────────────────────────────────────────
# 1. dunamu CDN  : 국내 기준 환율 (하나은행 고시). 오차 < ±1원.
# 2. upbit crix  : dunamu 실패 시 백업. 오차 약 +1원으로 허용 범위.
# ❌ exchangerate-api / open.er-api 등 국제 API: 약 -13원 오차 → 사용 금지
_FOREX_URLS = [
    "https://quotation-api-cdn.dunamu.com/v1/forex/recent?codes=FRX.KRWUSD",
    "https://crix-api-endpoint.upbit.com/v1/forex/recent?codes=FRX.KRWUSD",
]


def get_usd_krw() -> float:
    """USD/KRW 환율 조회 (dunamu 기준, 백업: upbit crix)."""
    now = time.time()
    if _cache["rate"] and now - _cache["ts"] < CACHE_TTL:
        return _cache["rate"]

    for url in _FOREX_URLS:
        try:
            resp = requests.get(url, timeout=5)
            resp.raise_for_status()
            rate = float(resp.json()[0]["basePrice"])
            _cache["rate"] = rate
            _cache["ts"] = now
            return rate
        except Exception:
            continue

    if _cache["rate"]:
        logger.warning("환율 API 실패 — 캐시된 값 재사용: %.1f", _cache["rate"])
        return _cache["rate"]

    raise RuntimeError("USD/KRW 환율 조회 실패 (dunamu/upbit crix 모두 응답 없음)")
