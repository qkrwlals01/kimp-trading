"""
모의거래 실행 진입점
실행: python paper_trading/run.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from paper_trading.paper_trader import PaperTrader

trader = PaperTrader()
trader.run()
