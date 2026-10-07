"""
실거래 설정 — 빗썸(원화 현물 매수) + 비트겟(USDT 선물 숏)
전략 규칙(익절 진입 환율 고정, 진입 필터 24h 하위 20%, 시간손절 없음, 가상손절, 익절폭)은
모의매매 설정(paper_trading/paper_settings.py)을 그대로 쓴다. 여기서는 실거래에만 필요한 것만 정한다.
"""

import os

from paper_trading.paper_settings import PAPER_COINS

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

# 운영 코인 — 모의매매와 같다. 빗썸 마켓 코드는 업비트와 같은 형식(KRW-XRP)
LIVE_COINS = list(PAPER_COINS)

# 슬롯 크기(원). 실제 수량은 비트겟 수량 단위로 내린다 — LINK 는 1개(약 1.9만원) 단위라 5.7만원이면 3개.
# 빗썸에서 산 수량과 같은 수량을 비트겟에서 숏해 가격 변동을 코인 개수 단위로 정확히 헤지한다.
LIVE_SLOT_KRW = 57_000
LIVE_N_SLOTS = 5                     # 코인당 (모의매매와 같음) → 3코인 × 5 × 5.7만 = 최대 85.5만원
LIVE_LEVERAGE = 3                    # 소액 시험은 3배 — 가상손절 거리 +27% (5배면 +16%)
LIVE_MARGIN_MODE = "crossed"         # 교차: 비트겟 증거금 전체가 모든 숏을 받친다

# 안전장치
LIVE_MAX_OPEN_KRW = 900_000          # 열린 슬롯(빗썸 매수 금액) 합계 상한
LIVE_MAX_SLIPPAGE = 0.002            # 빗썸 즉시체결 지정가: 최우선 호가에서 0.2% 까지만 체결 (넘는 수량은 체결 안 됨)
LIVE_EXIT_SLIPPAGE = (0.002, 0.005, 0.01)   # 청산 매도는 남으면 가격 하한을 넓혀 최대 3번
LIVE_ORDER_TIMEOUT_S = 8             # 체결 확인 대기
LIVE_MAX_ERRORS = 5                  # 연속 오류가 이만큼이면 신규 진입 중지 (청산은 계속)
LIVE_RETRY_S = 60                    # 실패한 코인·슬롯은 이 시간 동안 다시 시도하지 않음

# 파일 (real_trading/logs/, git 에 올리지 않음). {} 자리에 실거래 "" / 드라이런 "_dry"
STATE_FILE = os.path.join(LOG_DIR, "live_state{}.json")     # 열린 슬롯 — 재시작해도 이어진다
TRADE_LOG = os.path.join(LOG_DIR, "live_trades{}.csv")      # 실제 체결 기준 거래 기록
LIVE_LOG = os.path.join(LOG_DIR, "live{}.log")
HIST_FILE = os.path.join(LOG_DIR, "live_kimp_hist.csv")     # 진입 필터용 빗썸 김프 기록 (드라이런·실거래 공용)
STOP_FILE = os.path.join(LOG_DIR, "STOP")                   # 있으면 신규 진입 중지 (청산은 계속)
FLATTEN_FILE = os.path.join(LOG_DIR, "FLATTEN")             # 있으면 열린 슬롯을 전부 청산하고 멈춤
