# legacy — 초기 코드 (더 이상 쓰지 않음)

프로젝트 초기(2026-06 ~ 08)에 만든 코드다. 지금 전략과 검증 도구로 대체돼 유지하지 않으며,
폴더를 옮겨서 import 경로가 맞지 않아 그대로는 실행되지 않는다. 기록을 위해 남겨 둔다.

| 경로 | 내용 | 대체된 곳 |
|---|---|---|
| `core/`, `main.py`, `utils/premium.py` | 초기 고정 그리드 실거래 봇과 거래소 클라이언트 | `paper_trading/`, `real_trading/live_trader.py` |
| `real_trading/real_client.py`, `slippage_pilot.py` | 업비트 + 비트겟 v2 주문 클라이언트, 체결비용 측정 파일럿 | `real_trading/exchanges.py`, `live_trader.py --roundtrip` |
| `paper_trading/dashboard.py`, `run.py` | Flask 손익 대시보드, 옛 실행 진입점 | `tools/live_dashboard.py` |
| `data/backtest*.py`, `ml_optimize.py`, `return_analysis.py`, `visualize.py`, `collect_historical.py`, `collect_multi_coin.py` | 6월 백테스트·최적화 | `paper_trading/replay.py`, `data/` 의 후속 분석 |
| `tune.py`, `plot_kimp.py` | 파라미터 튜닝, 김프 그래프 | `tools/weekly_report.py` |

이 시기의 분석은 마지막 체결가로 손익을 계산해, 호가 스프레드를 반영하지 못했다
(CHANGELOG 6차 "체결가 착시"). 그래서 여기 있는 백테스트 수치는 근거로 쓰지 않는다.

`real_client.py` 의 비트겟 숏 청산 주문은 hedge 모드에서 방향이 틀리고(롱 청산), 통합계정(UTA) 키로는 동작하지 않는다.

초기 코드가 쓰던 flask·matplotlib 은 지금 requirements 에 없다.
