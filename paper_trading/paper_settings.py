"""
페이퍼 트레이딩 전용 설정
실거래 config/settings.py 와 완전히 분리됨
"""

# ── 가상 자본 구조 ────────────────────────────────────────────────
# 총 시드: 1억 2천만원
#   업비트  1억원   → 코인당 6,666,666원 (15등분)
#   비트겟  2천만원 → 코인당 1,333,333원 (15등분)
#
# 레버리지 5배 (완전 헤지):
#   비트겟 명목가치 = 1,333,333 × 5 = 6,666,665원 ≈ 업비트 현물
#   손절 기준: 비트겟 가격 +16% 시 마진비율 20% 도달 (1 - 0.16×5 = 0.20)
#
# ── 전략 변경 이력 ────────────────────────────────────────────────
# 2026-06-07 | 1차: 고정 그리드, 3코인, -4.5%~-0.5%, spacing 0.5%
# 2026-06-14 | 2차: 고정 그리드, 3코인, -2.5%~-0.5%, spacing 0.25%
# 2026-06-14 | 3차: 플로팅 그리드, 15코인, 범위제한 없음, spacing 0.1%
#   벤치마크: 투자금 2.96억, 31일 수익 307만원(1.04%), 익절 37,587회
#   전략: 스플릿(그리드) 변형, 15개 코인, 범위제한 없음
#
#   플로팅 그리드 원리:
#     - 고정 진입레벨 없음 → 현재 김프에서 바로 진입
#     - 슬롯당 자본: 6,666,666 ÷ 5 = 약 133만원
#     - 김프가 진입점 대비 +0.1% 오르면 청산 후 즉시 재진입 가능
#     - 항상 최대 5슬롯 유지 (빈 슬롯 생기면 즉시 채움)
#
# 2026-06-20 | 4차: spacing 0.2%→0.3%, 시간손절 24h 추가
#   이유: 김프 -1.6% 구간 장기 횡보 시 슬롯 50개 전부 묶임
#   시간손절로 자본 순환 + spacing 상향으로 손익 보완

_N_COINS = 10  # 운영 코인 수

_COIN = lambda market, symbol, currency: {
    "upbit_market":  market,
    "bitget_symbol": symbol,
    "currency":      currency,
    "upbit_capital": 100_000_000 // _N_COINS,  # 1억 ÷ 코인수
    "leverage":      5,
    "n_slots":       5,           # 동시 최대 보유 슬롯
    "spacing":       0.3,         # 익절 간격 (%) — 시간손절 도입으로 0.3%로 상향
}

PAPER_COINS = {
    # 기존 유지 (수익 코인)
    "SOL":  _COIN("KRW-SOL",  "SOLUSDT",  "SOL"),
    "AVAX": _COIN("KRW-AVAX", "AVAXUSDT", "AVAX"),
    "LINK": _COIN("KRW-LINK", "LINKUSDT", "LINK"),
    "BCH":  _COIN("KRW-BCH",  "BCHUSDT",  "BCH"),
    "SUI":  _COIN("KRW-SUI",  "SUIUSDT",  "SUI"),
    "UNI":  _COIN("KRW-UNI",  "UNIUSDT",  "UNI"),
    "TAO":  _COIN("KRW-TAO",  "TAOUSDT",  "TAO"),
    # 교체 (BTC→BSV, XRP→AAVE, ETH→ATOM)
    "BSV":  _COIN("KRW-BSV",  "BSVUSDT",  "BSV"),
    "AAVE": _COIN("KRW-AAVE", "AAVEUSDT", "AAVE"),
    "ATOM": _COIN("KRW-ATOM", "ATOMUSDT", "ATOM"),
}

PAPER_STOP_MARGIN_RATIO = 0.20   # 마진비율 20% 이하 → 가상 손절 (가격 +8% 상당)
PAPER_POLL_INTERVAL     = 10     # 폴링 주기 (초)
