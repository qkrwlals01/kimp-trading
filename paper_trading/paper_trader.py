"""
페이퍼 트레이딩 (모의거래) — 플로팅 그리드 전략

변경 이력:
  2026-06-14: 플로팅 그리드로 전환 (범위제한 없음, 15코인)
  - 고정 진입레벨 제거 → 현재 김프에서 바로 진입
  - 빈 슬롯 생기면 즉시 재진입 (항상 최대 슬롯 유지)
  - spacing(0.1%) 오르면 익절

  2026-09-26: 체결가 → 호가 기준으로 전환 (전략 설정은 그대로, 측정 방식만 수정)
  - 이전 버전은 마지막 체결가(trade_price, lastPr)로 진입·청산하고 슬리피지를 0 으로 뒀다.
    체결가가 매수·매도호가 사이를 튀는 만큼 가짜 익절이 생겨, 7월 기록 +3,400만원(월 +27%)이
    스프레드를 반영하면 적자로 뒤집혔다. 자세한 내용은 CHANGELOG 2026-09-26.
  - 진입: 업비트 매도호가에 매수 + 비트겟 매수호가에 숏    → entry 김프
    청산: 업비트 매수호가에 매도 + 비트겟 매도호가에 숏청산 → exit 김프
    익절 판단도 exit 김프로 한다 (지금 청산하면 실제로 받는 값).
  - 코인마다 두 거래소를 따로 부르던 것을 한 번에 동시 조회로 변경 (real_trading/quote_logger).
    조회 사이 시차로 김프가 어긋나는 것도 줄어든다.
  - 펀딩비: 8시간당 0.01% 고정 → 비트겟이 주는 현재 펀딩비를 보유 시간만큼 누적.
  - 기록 파일: trades.csv(체결가 기준 기록, 그대로 보존) → trades_book.csv

  한계: 최우선 호가 잔량보다 큰 주문이 다음 호가까지 먹는 추가 미끄러짐은 반영하지 않는다.
        슬롯이 200만원이면 일부 코인에서 비용이 과소평가되고, 실자본 규모(슬롯 약 33만원)면
        대부분 무시할 수 있다.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import time, csv, logging, requests
from dataclasses import dataclass
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from utils.exchange_rate import get_usd_krw
from real_trading.quote_logger import fetch_upbit, fetch_bitget
from paper_trading.paper_settings import (
    PAPER_COINS as COINS,
    PAPER_POLL_INTERVAL as POLL_INTERVAL,
    PAPER_STOP_MARGIN_RATIO as STOP_MARGIN_RATIO,
)

TIME_STOP_HOURS = 24   # 24시간 이상 보유 시 강제 청산

# ── 로그 설정 ─────────────────────────────────────────────────────
os.makedirs(os.path.join(os.path.dirname(__file__), "logs"), exist_ok=True)
LOG_DIR   = os.path.join(os.path.dirname(__file__), "logs")
TRADE_LOG = os.path.join(LOG_DIR, "trades_book.csv")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOG_DIR, "trading.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

TRADE_LOG_HEADER = [
    "trade_id", "coin", "slot_k",
    "entry_premium", "exit_premium", "target_premium",
    "entry_dt", "exit_dt", "hold_hours",
    "entry_upbit_px", "entry_bitget_px", "entry_usd_krw",
    "exit_upbit_px",  "exit_bitget_px",  "exit_usd_krw",
    "coin_qty", "short_qty",
    "upbit_pnl", "bitget_pnl", "gross_pnl",
    "fee_krw", "slip_krw", "funding_krw", "net_pnl",
    "capital_per_slot",
    # 중간가로 체결됐을 때 대비 호가를 넘느라 낸 비용. gross_pnl 에 이미 들어 있으므로
    # net_pnl 에서 다시 빼지 않는다. 체결가 기준 옛 기록과 비교할 때 쓴다.
    "spread_krw",
]


def _ensure_log():
    if not os.path.exists(TRADE_LOG):
        with open(TRADE_LOG, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(TRADE_LOG_HEADER)

def _append_trade(row: dict):
    with open(TRADE_LOG, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=TRADE_LOG_HEADER).writerow(row)


def _kimp(krw_px: float, usdt_px: float, usd_krw: float) -> float:
    bitget_krw = usdt_px * usd_krw
    return (krw_px - bitget_krw) / bitget_krw * 100


@dataclass
class FloatingSlot:
    """플로팅 그리드 슬롯 — 진입 시점의 김프를 기준으로 목표가 설정"""
    active:          bool  = False
    entry_premium:   float = 0.0
    target_premium:  float = 0.0   # entry_premium + spacing
    coin_qty:        float = 0.0
    short_qty:       float = 0.0
    upbit_entry_px:  float = 0.0   # 실제 체결가 (업비트 매도호가)
    bitget_entry_px: float = 0.0   # 실제 체결가 (비트겟 매수호가)
    upbit_entry_mid: float = 0.0   # 호가 비용 계산용 중간가
    bitget_entry_mid: float = 0.0
    entry_usd_krw:   float = 0.0
    entry_time:      float = 0.0
    funding_krw:     float = 0.0   # 보유 중 누적 펀딩 (양수 = 숏이 수취)
    funding_t:       float = 0.0   # 마지막 펀딩 누적 시각


class PaperCoinGrid:
    def __init__(self, coin: str, cfg: dict):
        self.coin    = coin
        self.cfg     = cfg
        self.n_slots = cfg["n_slots"]
        self.spacing = cfg["spacing"]
        self.cpg     = cfg["upbit_capital"] / cfg["n_slots"]
        self.slots   = [FloatingSlot() for _ in range(self.n_slots)]

    def active_count(self) -> int:
        return sum(1 for s in self.slots if s.active)

    def quote(self, up: dict, bg: dict, usd_krw: float) -> dict:
        """한 번에 받아 온 호가에서 이 코인 몫을 꺼내 진입·청산 김프를 계산한다."""
        u = up.get(self.cfg["upbit_market"])
        b = bg.get(self.cfg["bitget_symbol"])
        if not u or not b:
            raise ValueError("호가 없음")
        up_bid, up_ask = u[0], u[1]
        bg_bid, bg_ask, funding = b[0], b[1], b[5]
        return {
            "up_bid": up_bid, "up_ask": up_ask,
            "bg_bid": bg_bid, "bg_ask": bg_ask,
            "funding": funding,
            "entry_pct": _kimp(up_ask, bg_bid, usd_krw),   # 지금 진입하면 치르는 김프
            "exit_pct":  _kimp(up_bid, bg_ask, usd_krw),   # 지금 청산하면 받는 김프
        }

    def _can_enter(self, prem: float) -> bool:
        """기존 슬롯과 spacing/2 이내 중복 진입 방지"""
        for s in self.slots:
            if s.active and abs(s.entry_premium - prem) < self.spacing * 0.5:
                return False
        return True

    def _enter_slot(self, slot: FloatingSlot, q: dict, usd_krw: float):
        coin_qty  = self.cpg / q["up_ask"]
        margin_u  = self.cpg / usd_krw / self.cfg["leverage"]
        short_qty = round(margin_u * self.cfg["leverage"] / q["bg_bid"], 6)
        now       = time.time()

        slot.active           = True
        slot.entry_premium    = q["entry_pct"]
        slot.target_premium   = q["entry_pct"] + self.spacing
        slot.coin_qty         = coin_qty
        slot.short_qty        = short_qty
        slot.upbit_entry_px   = q["up_ask"]
        slot.bitget_entry_px  = q["bg_bid"]
        slot.upbit_entry_mid  = (q["up_bid"] + q["up_ask"]) / 2
        slot.bitget_entry_mid = (q["bg_bid"] + q["bg_ask"]) / 2
        slot.entry_usd_krw    = usd_krw
        slot.entry_time       = now
        slot.funding_krw      = 0.0
        slot.funding_t        = now

        logger.info(
            f"  [모의/{self.coin}] ▶ 진입  "
            f"김프={q['entry_pct']:.2f}%  목표={slot.target_premium:.2f}%  "
            f"수량={coin_qty:.6f}  자본={self.cpg/10000:.1f}만원"
        )

    def _accrue_funding(self, q: dict):
        """펀딩비는 8시간 단위 비율이다. 폴링 간격만큼 나눠 누적한다."""
        now = time.time()
        for s in self.slots:
            if s.active:
                s.funding_krw += q["funding"] * self.cpg * (now - s.funding_t) / (8 * 3600)
                s.funding_t = now

    def _exit_slot(self, slot: FloatingSlot, q: dict, upbit_usdt_krw: float,
                   trade_id: int, reason: str = "익절"):
        upbit_pnl  = slot.coin_qty * (q["up_bid"] - slot.upbit_entry_px)
        # 비트겟 USDT → 업비트 USDT 매도 경로이므로 업비트 USDT 시세(김프 포함) 적용
        bitget_pnl = slot.short_qty * (slot.bitget_entry_px - q["bg_ask"]) * upbit_usdt_krw
        gross_pnl  = upbit_pnl + bitget_pnl

        up_mid, bg_mid = (q["up_bid"] + q["up_ask"]) / 2, (q["bg_bid"] + q["bg_ask"]) / 2
        mid_gross  = (slot.coin_qty * (up_mid - slot.upbit_entry_mid)
                      + slot.short_qty * (slot.bitget_entry_mid - bg_mid) * upbit_usdt_krw)
        spread_krw = mid_gross - gross_pnl

        hold_h     = (time.time() - slot.entry_time) / 3600
        fee_krw    = (0.0005 * 2 + 0.0004 * 2) * self.cpg  # 업비트 0.05%×2 + 비트겟 0.04%×2
        slip_krw   = 0.0                                   # 최우선 잔량 초과 미끄러짐 — 미반영
        fund_krw   = slot.funding_krw
        net_pnl    = gross_pnl - fee_krw - slip_krw + fund_krw

        k = self.slots.index(slot)
        _append_trade({
            "trade_id":        trade_id,
            "coin":            self.coin,
            "slot_k":          k,
            "entry_premium":   round(slot.entry_premium, 4),
            "exit_premium":    round(q["exit_pct"], 4),
            "target_premium":  round(slot.target_premium, 4),
            "entry_dt":        datetime.fromtimestamp(slot.entry_time, tz=timezone.utc).isoformat(),
            "exit_dt":         datetime.now(tz=timezone.utc).isoformat(),
            "hold_hours":      round(hold_h, 2),
            "entry_upbit_px":  slot.upbit_entry_px,
            "entry_bitget_px": slot.bitget_entry_px,
            "entry_usd_krw":   slot.entry_usd_krw,
            "exit_upbit_px":   q["up_bid"],
            "exit_bitget_px":  q["bg_ask"],
            "exit_usd_krw":    upbit_usdt_krw,
            "coin_qty":        round(slot.coin_qty, 8),
            "short_qty":       round(slot.short_qty, 8),
            "upbit_pnl":       round(upbit_pnl, 0),
            "bitget_pnl":      round(bitget_pnl, 0),
            "gross_pnl":       round(gross_pnl, 0),
            "fee_krw":         round(fee_krw, 0),
            "slip_krw":        round(slip_krw, 0),
            "funding_krw":     round(fund_krw, 0),
            "net_pnl":         round(net_pnl, 0),
            "capital_per_slot": self.cpg,
            "spread_krw":      round(spread_krw, 0),
        })
        icon = "◀" if reason == "익절" else "⏱"
        logger.info(
            f"  [모의/{self.coin}] {icon} {reason}  "
            f"진입={slot.entry_premium:.2f}%→청산={q['exit_pct']:.2f}%  "
            f"보유 {hold_h:.1f}h  순수익 {net_pnl/10000:+.3f}만원 (호가비용 {spread_krw/10000:.3f}만원)"
        )

        slot.active = False
        slot.coin_qty = slot.short_qty = 0.0
        slot.entry_premium = slot.target_premium = 0.0
        slot.upbit_entry_px = slot.bitget_entry_px = slot.entry_usd_krw = slot.entry_time = 0.0
        slot.upbit_entry_mid = slot.bitget_entry_mid = 0.0
        slot.funding_krw = slot.funding_t = 0.0

    def _check_stoploss(self, q: dict, upbit_usdt_krw: float, trade_counter_ref: list):
        active_slots = [s for s in self.slots if s.active]
        if not active_slots:
            return
        for slot in active_slots:
            # 숏을 되사야 하는 가격(매도호가) 기준으로 본다
            price_chg = (q["bg_ask"] - slot.bitget_entry_px) / slot.bitget_entry_px
            margin_rt = 1 - price_chg * self.cfg["leverage"]
            if margin_rt <= STOP_MARGIN_RATIO:
                logger.warning(
                    f"  [모의/{self.coin}] !! 가상손절: 마진비율 {margin_rt*100:.1f}%"
                    f" <= {STOP_MARGIN_RATIO*100:.0f}%  가격변동 {price_chg*100:+.1f}%"
                )
                for s in list(active_slots):
                    trade_counter_ref[0] += 1
                    self._exit_slot(s, q, upbit_usdt_krw, trade_counter_ref[0], reason="가상손절")
                break

    def tick(self, up: dict, bg: dict, usd_krw: float, upbit_usdt_krw: float,
             trade_counter_ref: list):
        try:
            q = self.quote(up, bg, usd_krw)
            self._accrue_funding(q)
            logger.info(
                f"  [모의/{self.coin}] 김프 진입 {q['entry_pct']:+.2f}% / 청산 {q['exit_pct']:+.2f}%  "
                f"활성 {self.active_count()}/{self.n_slots}슬롯"
            )

            self._check_stoploss(q, upbit_usdt_krw, trade_counter_ref)

            # 시간 손절 체크 (24시간 이상 보유 시 강제 청산)
            for slot in list(self.slots):
                if slot.active and (time.time() - slot.entry_time) / 3600 >= TIME_STOP_HOURS:
                    trade_counter_ref[0] += 1
                    self._exit_slot(slot, q, upbit_usdt_krw, trade_counter_ref[0], reason="시간손절")

            # 익절 체크 — 지금 청산하면 받는 김프가 목표에 닿았을 때
            for slot in self.slots:
                if slot.active and q["exit_pct"] >= slot.target_premium:
                    trade_counter_ref[0] += 1
                    self._exit_slot(slot, q, upbit_usdt_krw, trade_counter_ref[0], reason="익절")

            # 진입 체크 (빈 슬롯 즉시 채우기)
            for slot in self.slots:
                if not slot.active and self.active_count() < self.n_slots:
                    if self._can_enter(q["entry_pct"]):
                        self._enter_slot(slot, q, usd_krw)

        except Exception as e:
            logger.error(f"  [모의/{self.coin}] 오류: {e}", exc_info=True)


class PaperTrader:
    def __init__(self):
        _ensure_log()
        self.grids          = {coin: PaperCoinGrid(coin, cfg) for coin, cfg in COINS.items()}
        self._trade_counter = [self._load_counter()]
        self._markets       = [cfg["upbit_market"] for cfg in COINS.values()] + ["KRW-USDT"]
        self._pool          = ThreadPoolExecutor(max_workers=2)

    def _load_counter(self) -> int:
        if not os.path.exists(TRADE_LOG):
            return 0
        with open(TRADE_LOG, encoding="utf-8") as f:
            return max(0, sum(1 for _ in f) - 1)

    def _fetch_books(self) -> tuple:
        """두 거래소를 동시에 한 번씩만 조회한다. 한쪽이라도 실패하면 이번 폴링은 건너뛴다."""
        fu = self._pool.submit(fetch_upbit, self._markets)
        fb = self._pool.submit(fetch_bitget)
        return fu.result(), fb.result()

    def run(self):
        total_capital = sum(cfg["upbit_capital"] for cfg in COINS.values())
        first_cfg = list(COINS.values())[0]
        logger.info("=== [모의거래] 플로팅 그리드 시작 (호가 기준) ===")
        logger.info(f"운영 코인 {len(COINS)}개: {list(COINS.keys())}")
        logger.info(
            f"가상 자본 {total_capital//10000}만원  |  "
            f"spacing={first_cfg['spacing']}%  슬롯={first_cfg['n_slots']}개/코인  "
            f"슬롯당 {first_cfg['upbit_capital']//first_cfg['n_slots']//10000}만원"
        )
        logger.info(f"폴링 {POLL_INTERVAL}초  |  거래 로그: {TRADE_LOG}")
        logger.info("-" * 60)

        while True:
            try:
                usd_krw = get_usd_krw()
                up, bg  = self._fetch_books()
                usdt    = up.get("KRW-USDT")
                # USDT 를 팔아 원화로 바꾸는 경로이므로 매수호가. 없으면 dunamu 환율 fallback
                upbit_usdt_krw = usdt[0] if usdt else usd_krw
                logger.info(
                    f"[폴링] 환율(dunamu) {usd_krw:,.0f}  "
                    f"업비트USDT {upbit_usdt_krw:,.0f}  누계 청산 {self._trade_counter[0]}건"
                )
                for g in self.grids.values():
                    g.tick(up, bg, usd_krw, upbit_usdt_krw, self._trade_counter)

            except KeyboardInterrupt:
                logger.info("=" * 60)
                logger.info("[모의거래] 사용자 중단")
                for coin, g in self.grids.items():
                    n = g.active_count()
                    open_prems = [f"{s.entry_premium:.2f}%" for s in g.slots if s.active]
                    logger.info(
                        f"  [{coin}] 미청산 {n}개: {', '.join(open_prems) if open_prems else '없음'}"
                    )

                if os.path.exists(TRADE_LOG):
                    with open(TRADE_LOG, encoding="utf-8") as f:
                        rows = list(csv.DictReader(f))
                    if rows:
                        total_net = sum(float(r["net_pnl"]) for r in rows)
                        logger.info(
                            f"[모의거래] 총 {len(rows)}건  누적 순수익 {total_net/10000:+.3f}만원"
                        )
                logger.info("=" * 60)
                break

            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError) as e:
                logger.warning(f"[네트워크 오류] {type(e).__name__}: {e} — {POLL_INTERVAL}초 후 재시도")

            except Exception as e:
                logger.error(f"오류: {e}", exc_info=True)

            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    PaperTrader().run()
