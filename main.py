"""
실행: python main.py [single|multi]
  single  — BTC 단일 코인 그리드 (기본값)
  multi   — BTC + ETH + XRP 멀티 코인 그리드
"""

import logging, sys, os

sys.path.insert(0, os.path.dirname(__file__))
os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/trading.log", encoding="utf-8"),
    ],
)

mode = sys.argv[1] if len(sys.argv) > 1 else "single"

if mode == "multi":
    from core.multi_grid_trader import MultiGridTrader
    trader = MultiGridTrader()
else:
    from core.grid_trader import GridTrader
    trader = GridTrader()

trader.run()
