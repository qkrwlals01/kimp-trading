# config/settings.py 로 복사해 값을 채운다 (settings.py 는 git 에 올리지 않는다)
# 모의매매·호가 로거·리플레이는 공개 API 만 써서 키 없이 돈다.

# 비트겟 USDT 선물 (실거래 실행기 real_trading/live_trader.py)
BITGET_ACCESS_KEY = "YOUR_BITGET_ACCESS_KEY"
BITGET_SECRET_KEY = "YOUR_BITGET_SECRET_KEY"
BITGET_PASSPHRASE = "YOUR_BITGET_PASSPHRASE"

# 빗썸 API 2.0 (실거래 실행기 real_trading/live_trader.py) — 권한: 자산조회·주문조회·주문하기만, 출금 권한은 주지 말 것
BITHUMB_ACCESS_KEY = ""
BITHUMB_SECRET_KEY = ""

# 운영 서버 접속 (tools/weekly_report.py, tools/live_dashboard.py 가 SSH 로 기록을 읽을 때)
SERVER_HOST = ""        # 예: "user@서버주소"
SERVER_KEY_PATH = ""    # 예: "~/.ssh/서버키파일"
