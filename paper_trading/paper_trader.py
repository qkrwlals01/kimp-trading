"""
페이퍼 트레이딩 (모의거래) — 플로팅 그리드 전략

변경 이력:
  2026-06-14: 플로팅 그리드로 전환 (범위제한 없음, 15코인)
  - 고정 진입레벨 제거 → 현재 김프에서 바로 진입
  - 빈 슬롯 생기면 즉시 재진입 (항상 최대 슬롯 유지)
  - spacing(0.1%) 오르면 익절
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import time, csv, logging, requests
from dataclasses import dataclass
from datetime import datetime, timezone
from utils.exchange_rate import get_usd_krw
from core.coin_client import UpbitCoin, BitgetCoin
from paper_trading.paper_settings import (
    PAPER_COINS as COINS,
    PAPER_POLL_INTERVAL as POLL_INTERVAL,
    PAPER_STOP_MARGIN_RATIO as STOP_MARGIN_RATIO,
)

TIME_STOP_HOURS = 24   # 24시간 이상 보유 시 강제 청산

# ── 로그 설정 ─────────────────────────────────────────────────────
os.makedirs(os.path.join(os.path.dirname(__file__), "logs"), exist_ok=True)
LOG_DIR   = os.path.join(os.path.dirname(__file__), "logs")
TRADE_LOG = os.path.join(LOG_DIR, "trades.csv")

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
]


def _ensure_log():
    if not os.path.exists(TRADE_LOG):
        with open(TRADE_LOG, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(TRADE_LOG_HEADER)

def _append_trade(row: dict):
    with open(TRADE_LOG, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=TRADE_LOG_HEADER).writerow(row)


@dataclass
class FloatingSlot:
    """플로팅 그리드 슬롯 — 진입 시점의 김프를 기준으로 목표가 설정"""
    active:          bool  = False
    entry_premium:   float = 0.0
    target_premium:  float = 0.0   # entry_premium + spacing
    coin_qty:        float = 0.0
    short_qty:       float = 0.0
    upbit_entry_px:  float = 0.0
    bitget_entry_px: float = 0.0
    entry_usd_krw:   float = 0.0
    entry_time:      float = 0.0


class PaperCoinGrid:
    def __init__(self, coin: str, cfg: dict):
        self.coin    = coin
        self.cfg     = cfg
        self.n_slots = cfg["n_slots"]
        self.spacing = cfg["spacing"]
        self.cpg     = cfg["upbit_capital"] / cfg["n_slots"]
        self.slots   = [FloatingSlot() for _ in range(self.n_slots)]
        self.upbit   = UpbitCoin(cfg["upbit_market"], cfg["currency"])
        self.bitget  = BitgetCoin(cfg["bitget_symbol"])

    def active_count(self) -> int:
        return sum(1 for s in self.slots if s.active)

    def get_premium(self, usd_krw: float) -> dict:
        upbit_px   = self.upbit.get_price()
        bitget_px  = self.bitget.get_price()
        bitget_krw = bitget_px * usd_krw
        prem       = (upbit_px - bitget_krw) / bitget_krw * 100
        return {
            "upbit_px":    upbit_px,
            "bitget_px":   bitget_px,
            "bitget_krw":  bitget_krw,
            "premium_pct": prem,
        }

    def _can_enter(self, prem: float) -> bool:
        """기존 슬롯과 spacing/2 이내 중복 진입 방지"""
        for s in self.slots:
            if s.active and abs(s.entry_premium - prem) < self.spacing * 0.5:
                return False
        return True

    def _enter_slot(self, slot: FloatingSlot, px: dict, usd_krw: float):
        coin_qty  = self.cpg / px["upbit_px"]
        margin_u  = self.cpg / usd_krw / self.cfg["leverage"]
        short_qty = round(margin_u * self.cfg["leverage"] / px["bitget_px"], 6)

        slot.active          = True
        slot.entry_premium   = px["premium_pct"]
        slot.target_premium  = px["premium_pct"] + self.spacing
        slot.coin_qty        = coin_qty
        slot.short_qty       = short_qty
        slot.upbit_entry_px  = px["upbit_px"]
        slot.bitget_entry_px = px["bitget_px"]
        slot.entry_usd_krw   = usd_krw
        slot.entry_time      = time.time()

        logger.info(
            f"  [모의/{self.coin}] ▶ 진입  "
            f"김프={px['premium_pct']:.2f}%  목표={slot.target_premium:.2f}%  "
            f"수량={coin_qty:.6f}  자본={self.cpg/10000:.1f}만원"
        )

    def _exit_slot(self, slot: FloatingSlot, px: dict, upbit_usdt_krw: float,
                   trade_id: int, reason: str = "익절"):
        upbit_pnl  = slot.coin_qty * (px["upbit_px"] - slot.upbit_entry_px)
        # 비트겟 USDT → 업비트 USDT 매도 경로이므로 업비트 USDT 시세(김프 포함) 적용
        bitget_pnl = slot.short_qty * (slot.bitget_entry_px - px["bitget_px"]) * upbit_usdt_krw
        gross_pnl  = upbit_pnl + bitget_pnl
        hold_h     = (time.time() - slot.entry_time) / 3600
        fee_krw    = (0.0005 * 2 + 0.0004 * 2) * self.cpg  # 업비트 0.05%×2 + 비트겟 0.04%×2
        slip_krw   = 0.0
        fund_krw   = (hold_h / 8) * 0.0001 * self.cpg
        net_pnl    = gross_pnl - fee_krw - slip_krw + fund_krw

        k = self.slots.index(slot)
        _append_trade({
            "trade_id":        trade_id,
            "coin":            self.coin,
            "slot_k":          k,
            "entry_premium":   round(slot.entry_premium, 4),
            "exit_premium":    round(px["premium_pct"], 4),
            "target_premium":  round(slot.target_premium, 4),
            "entry_dt":        datetime.fromtimestamp(slot.entry_time, tz=timezone.utc).isoformat(),
            "exit_dt":         datetime.now(tz=timezone.utc).isoformat(),
            "hold_hours":      round(hold_h, 2),
            "entry_upbit_px":  slot.upbit_entry_px,
            "entry_bitget_px": slot.bitget_entry_px,
            "entry_usd_krw":   slot.entry_usd_krw,
            "exit_upbit_px":   px["upbit_px"],
            "exit_bitget_px":  px["bitget_px"],
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
        })
        icon = "◀" if reason == "익절" else "⏱"
        logger.info(
            f"  [모의/{self.coin}] {icon} {reason}  "
            f"진입={slot.entry_premium:.2f}%→현재={px['premium_pct']:.2f}%  "
            f"보유 {hold_h:.1f}h  순수익 {net_pnl/10000:+.3f}만원"
        )

        slot.active = False
        slot.coin_qty = slot.short_qty = 0.0
        slot.entry_premium = slot.target_premium = 0.0
        slot.upbit_entry_px = slot.bitget_entry_px = slot.entry_usd_krw = slot.entry_time = 0.0

    def _check_stoploss(self, px: dict, usd_krw: float, trade_counter_ref: list):
        active_slots = [s for s in self.slots if s.active]
        if not active_slots:
            return
        for slot in active_slots:
            price_chg = (px["bitget_px"] - slot.bitget_entry_px) / slot.bitget_entry_px
            margin_rt = 1 - price_chg * self.cfg["leverage"]
            if margin_rt <= STOP_MARGIN_RATIO:
                logger.warning(
                    f"  [모의/{self.coin}] !! 가상손절: 마진비율 {margin_rt*100:.1f}%"
                    f" <= {STOP_MARGIN_RATIO*100:.0f}%  가격변동 {price_chg*100:+.1f}%"
                )
                for s in list(active_slots):
                    trade_counter_ref[0] += 1
                    self._exit_slot(s, px, usd_krw, trade_counter_ref[0])
                break

    def tick(self, usd_krw: float, upbit_usdt_krw: float, trade_counter_ref: list):
        try:
            px   = self.get_premium(usd_krw)
            prem = px["premium_pct"]
            logger.info(
                f"  [모의/{self.coin}] 김프 {prem:+.2f}%  "
                f"활성 {self.active_count()}/{self.n_slots}슬롯"
            )

            self._check_stoploss(px, upbit_usdt_krw, trade_counter_ref)

            # 시간 손절 체크 (24시간 이상 보유 시 강제 청산)
            for slot in list(self.slots):
                if slot.active and (time.time() - slot.entry_time) / 3600 >= TIME_STOP_HOURS:
                    trade_counter_ref[0] += 1
                    self._exit_slot(slot, px, upbit_usdt_krw, trade_counter_ref[0], reason="시간손절")

            # 익절 체크
            for slot in self.slots:
                if slot.active and prem >= slot.target_premium:
                    trade_counter_ref[0] += 1
                    self._exit_slot(slot, px, upbit_usdt_krw, trade_counter_ref[0], reason="익절")

            # 진입 체크 (빈 슬롯 즉시 채우기)
            for slot in self.slots:
                if not slot.active and self.active_count() < self.n_slots:
                    if self._can_enter(prem):
                        self._enter_slot(slot, px, usd_krw)

        except Exception as e:
            logger.error(f"  [모의/{self.coin}] 오류: {e}", exc_info=True)


class PaperTrader:
    def __init__(self):
        _ensure_log()
        self.grids          = {coin: PaperCoinGrid(coin, cfg) for coin, cfg in COINS.items()}
        self._trade_counter = [self._load_counter()]

    def _load_counter(self) -> int:
        if not os.path.exists(TRADE_LOG):
            return 0
        with open(TRADE_LOG, encoding="utf-8") as f:
            return max(0, sum(1 for _ in f) - 1)

    def run(self):
        total_capital = sum(cfg["upbit_capital"] for cfg in COINS.values())
        first_cfg = list(COINS.values())[0]
        self._upbit_usdt = UpbitCoin("KRW-USDT", "USDT")  # 업비트 USDT 시세 (김프 포함 환율)
        logger.info("=== [모의거래] 플로팅 그리드 시작 ===")
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
                try:
                    upbit_usdt_krw = self._upbit_usdt.get_price()
                except Exception:
                    upbit_usdt_krw = usd_krw  # 조회 실패 시 dunamu 환율 fallback
                logger.info(
                    f"[폴링] 환율(dunamu) {usd_krw:,.0f}  "
                    f"업비트USDT {upbit_usdt_krw:,.0f}  누계 익절 {self._trade_counter[0]}건"
                )
                for g in self.grids.values():
                    g.tick(usd_krw, upbit_usdt_krw, self._trade_counter)

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
                    import csv as _csv
                    with open(TRADE_LOG, encoding="utf-8") as f:
                        rows = list(_csv.DictReader(f))
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
