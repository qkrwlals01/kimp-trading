UPBIT_ACCESS_KEY = "YOUR_UPBIT_ACCESS_KEY"
UPBIT_SECRET_KEY = "YOUR_UPBIT_SECRET_KEY"

BITGET_ACCESS_KEY = "YOUR_BITGET_ACCESS_KEY"
BITGET_SECRET_KEY = "YOUR_BITGET_SECRET_KEY"
BITGET_PASSPHRASE = "YOUR_BITGET_PASSPHRASE"

# 운영 서버 접속 (tools/weekly_report.py, tools/live_dashboard.py 가 SSH 로 기록을 읽을 때)
SERVER_HOST = ""        # 예: "user@서버주소"
SERVER_KEY_PATH = ""    # 예: "~/.ssh/서버키파일"

# 빗썸 API 2.0 (실거래 실행기 real_trading/live_trader.py) — 권한: 자산조회·주문조회·주문하기만, 출금 권한은 주지 말 것
BITHUMB_ACCESS_KEY = ""
BITHUMB_SECRET_KEY = ""

# 기존 단일 코인 설정 (하위 호환)
SYMBOL              = "BTC"
UPBIT_MARKET        = "KRW-BTC"
BITGET_SYMBOL       = "BTCUSDT"
BITGET_PRODUCT_TYPE = "USDT-FUTURES"
BITGET_LEVERAGE     = 3

# ── 멀티 코인 그리드 파라미터 ────────────────────────────────────
# 총 원금: 100만원
# 업비트 75만원 (코인당 25만원) + 비트겟 25만원 (약 181 USDT)
# 레버리지 3배: 비트겟 증거금 = 업비트 자본 ÷ 3 = 25만원
# 설정일: 2026-05-15  다음 검토: 2026-06-01

COINS = {
    "BTC": {
        "upbit_market":  "KRW-BTC",
        "bitget_symbol": "BTCUSDT",
        "currency":      "BTC",
        "upbit_capital": 250_000,    # 업비트 25만원
        "leverage":      3,          # BTC: 레버리지 3배 (손절 +27%)
        "n_grids":       8,
        "grid_bottom":  -2.0,
        "grid_top":      2.0,
    },
    "ETH": {
        "upbit_market":  "KRW-ETH",
        "bitget_symbol": "ETHUSDT",
        "currency":      "ETH",
        "upbit_capital": 250_000,
        "leverage":      3,
        "n_grids":       8,
        "grid_bottom":  -2.0,
        "grid_top":      4.0,
    },
    "XRP": {
        "upbit_market":  "KRW-XRP",
        "bitget_symbol": "XRPUSDT",
        "currency":      "XRP",
        "upbit_capital": 250_000,
        "leverage":      3,
        "n_grids":       8,
        "grid_bottom":  -2.0,
        "grid_top":      5.0,
    },
}

# 손절 파라미터
STOP_MARGIN_RATIO = 0.20   # 비트겟 증거금 20% 이하 → 강제 청산

# 단일 코인 모드 호환 파라미터 (BTC 기준)
UPBIT_TOTAL_KRW  = COINS["BTC"]["upbit_capital"]
N_GRIDS          = COINS["BTC"]["n_grids"]
GRID_BOTTOM      = COINS["BTC"]["grid_bottom"]
GRID_TOP         = COINS["BTC"]["grid_top"]
GRID_SPACING     = (GRID_TOP - GRID_BOTTOM) / N_GRIDS
CAPITAL_PER_GRID = UPBIT_TOTAL_KRW / N_GRIDS
GRID_LEVELS_LIST = [GRID_BOTTOM + k * GRID_SPACING for k in range(N_GRIDS)]

# 환율 API
EXCHANGE_RATE_API = "https://www.koreaexim.go.kr/site/program/financial/exchangeJSON"

# 모니터링 주기 (초)
POLL_INTERVAL = 10
