"""
페이퍼 트레이딩 전용 설정
실거래 config/settings.py 와 완전히 분리됨
"""

# ── 가상 자본 구조 ────────────────────────────────────────────────
# 총 시드: 1천만원 (실제 투입 예정 규모에 맞춤, 2026-09-27~)
#   업비트  8,333,333원 → 코인당 833,333원 (10등분) → 슬롯당 약 16.7만원
#   비트겟  1,666,667원 → 코인당 166,667원
#
# 레버리지 5배 (완전 헤지):
#   비트겟 명목가치 = 166,667 × 5 = 833,333원 = 업비트 현물
#   → 총 시드를 업비트 : 비트겟 = 5 : 1 로 나눈다
#   손절 기준: 비트겟 가격 +16% 시 마진비율 20% 도달 (1 - 0.16×5 = 0.20)
#
# 슬롯 크기가 호가 비용을 좌우한다. 모의매매가 최우선 호가만 보므로 슬롯이 크면
# 다음 호가까지 먹는 비용이 빠진다. 실자본 규모로 맞춰야 모의 결과가 현실과 가깝다.
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
#     - 김프가 진입점 대비 +spacing 오르면 청산 후 즉시 재진입 가능
#     - 항상 최대 5슬롯 유지 (빈 슬롯 생기면 즉시 채움)
#
# 2026-06-20 | 4차: spacing 0.2%→0.3%, 시간손절 24h 추가
#   이유: 김프 -1.6% 구간 장기 횡보 시 슬롯 50개 전부 묶임
#   시간손절로 자본 순환 + spacing 상향으로 손익 보완
#
# 2026-09-27 | 가상 자본 1억2천만 → 1천만 (실자본 규모). 전략 설정은 그대로.
#   함께 paper_trader 를 체결가 → 호가 기준으로 전환 (CHANGELOG 6차)

PAPER_TOTAL_KRW = 10_000_000   # 총 시드
_LEVERAGE       = 5
_N_COINS        = 10           # 운영 코인 수

# 완전 헤지: 업비트 현물 = 비트겟 증거금 × 레버리지 → 업비트 몫 = 총액 × L/(L+1)
PAPER_UPBIT_KRW  = PAPER_TOTAL_KRW * _LEVERAGE // (_LEVERAGE + 1)
PAPER_BITGET_KRW = PAPER_TOTAL_KRW - PAPER_UPBIT_KRW

_COIN = lambda market, symbol, currency: {
    "upbit_market":  market,
    "bitget_symbol": symbol,
    "currency":      currency,
    "upbit_capital": PAPER_UPBIT_KRW // _N_COINS,
    "leverage":      _LEVERAGE,
    "n_slots":       5,           # 동시 최대 보유 슬롯
    "spacing":       0.3,         # 익절 간격 (%) — 시간손절 도입으로 0.3%로 상향
}

# 7월 모의매매 운영 코인 그대로. 이 중 상당수는 호가 비용이 순마진을 넘는다 (CHANGELOG 6차).
# 호가 로그 7일치가 쌓이면(2026-10-03경) real_trading/coin_selector 판정으로 다시 고른다.
PAPER_COINS = {
    "SOL":  _COIN("KRW-SOL",  "SOLUSDT",  "SOL"),
    "AVAX": _COIN("KRW-AVAX", "AVAXUSDT", "AVAX"),
    "LINK": _COIN("KRW-LINK", "LINKUSDT", "LINK"),
    "BCH":  _COIN("KRW-BCH",  "BCHUSDT",  "BCH"),
    "SUI":  _COIN("KRW-SUI",  "SUIUSDT",  "SUI"),
    "UNI":  _COIN("KRW-UNI",  "UNIUSDT",  "UNI"),
    "TAO":  _COIN("KRW-TAO",  "TAOUSDT",  "TAO"),
    # 07월 교체 편입 (BTC→BSV, XRP→AAVE, ETH→ATOM) — 체결가 착시로 고른 코인
    "BSV":  _COIN("KRW-BSV",  "BSVUSDT",  "BSV"),
    "AAVE": _COIN("KRW-AAVE", "AAVEUSDT", "AAVE"),
    "ATOM": _COIN("KRW-ATOM", "ATOMUSDT", "ATOM"),
}

PAPER_STOP_MARGIN_RATIO = 0.20   # 마진비율 20% 이하 → 가상 손절 (레버리지 5배면 가격 +16% 상당)
PAPER_POLL_INTERVAL     = 10     # 폴링 주기 (초)
