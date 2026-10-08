"""
멀티 코인 그리드 트레이더
BTC / ETH / XRP 각자 독립적인 그리드 슬롯 관리
"""

import time, csv, os, logging
from dataclasses import dataclass
from datetime import datetime, timezone
from utils.exchange_rate import get_usd_krw
from core.coin_client import UpbitCoin, BitgetCoin
from config.settings import COINS, POLL_INTERVAL, STOP_MARGIN_RATIO

logger = logging.getLogger(__name__)

TRADE_LOG = "logs/trades.csv"
TRADE_LOG_HEADER = [
    "trade_id", "coin", "grid_k", "grid_level", "exit_level",
    "entry_dt", "exit_dt", "hold_hours",
    "entry_premium", "exit_premium",
    "entry_upbit_px", "entry_bitget_px", "entry_usd_krw",
    "exit_upbit_px",  "exit_bitget_px",  "exit_usd_krw",
    "coin_qty", "short_qty",
    "upbit_pnl", "bitget_pnl", "gross_pnl",
    "fee_krw", "slip_krw", "funding_krw", "net_pnl",
    "capital_per_grid",
]


def _ensure_log():
    os.makedirs("logs", exist_ok=True)
    if not os.path.exists(TRADE_LOG):
        with open(TRADE_LOG, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(TRADE_LOG_HEADER)

def _append_trade(row: dict):
    with open(TRADE_LOG, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=TRADE_LOG_HEADER).writerow(row)


@dataclass
class GridSlot:
    grid_level: float
    exit_level: float
    active: bool = False

    entry_premium:   float = 0.0
    coin_qty:        float = 0.0
    short_qty:       float = 0.0
    upbit_entry_px:  float = 0.0
    bitget_entry_px: float = 0.0
    entry_usd_krw:   float = 0.0
    entry_time:      float = 0.0


class CoinGrid:
    def __init__(self, coin: str, cfg: dict):
        self.coin    = coin
        self.cfg     = cfg
        spacing      = (cfg["grid_top"] - cfg["grid_bottom"]) / cfg["n_grids"]
        self.spacing = spacing
        self.cpg     = cfg["upbit_capital"] / cfg["n_grids"]

        self.slots = [
            GridSlot(
                grid_level = cfg["grid_bottom"] + k * spacing,
                exit_level = cfg["grid_bottom"] + (k + 1) * spacing,
            )
            for k in range(cfg["n_grids"])
        ]
        self.upbit  = UpbitCoin(cfg["upbit_market"], cfg["currency"])
        self.bitget = BitgetCoin(cfg["bitget_symbol"])

    def active_count(self) -> int:
        return sum(1 for s in self.slots if s.active)

    def get_premium(self, usd_krw: float) -> dict:
        upbit_px  = self.upbit.get_price()
        bitget_px = self.bitget.get_price()
        bitget_krw = bitget_px * usd_krw
        prem = (upbit_px - bitget_krw) / bitget_krw * 100
        return {"upbit_px": upbit_px, "bitget_px": bitget_px,
                "bitget_krw": bitget_krw, "premium_pct": prem}

    def enter_slot(self, slot: GridSlot, px: dict, usd_krw: float):
        margin_usdt = round(self.cpg / usd_krw / self.cfg["leverage"], 2)
        logger.info(
            f"  [{self.coin}] ▶ 진입 [{slot.grid_level:+.1f}%→{slot.exit_level:+.1f}%]  "
            f"프리미엄={px['premium_pct']:.2f}%  자본={self.cpg/10000:.0f}만원"
        )
        self.bitget.set_leverage(self.cfg["leverage"])
        self.upbit.buy_market(self.cpg)
        time.sleep(3)
        coin_bal = self.upbit.get_coin_balance()
        self.bitget.open_short(margin_usdt, self.cfg["leverage"])
        short_size = margin_usdt * self.cfg["leverage"] / px["bitget_px"]

        slot.active          = True
        slot.entry_premium   = px["premium_pct"]
        slot.coin_qty        = coin_bal
        slot.short_qty       = short_size
        slot.upbit_entry_px  = px["upbit_px"]
        slot.bitget_entry_px = px["bitget_px"]
        slot.entry_usd_krw   = usd_krw
        slot.entry_time      = time.time()
        logger.info(f"  [{self.coin}] ✅ 진입 완료  잔고={coin_bal:.6f}")

    def exit_slot(self, slot: GridSlot, px: dict, usd_krw: float, trade_id: int):
        logger.info(
            f"  [{self.coin}] ◀ 청산 [{slot.grid_level:+.1f}%→{slot.exit_level:+.1f}%]  "
            f"프리미엄={px['premium_pct']:.2f}%"
        )
        self.upbit.sell_market(slot.coin_qty)
        self.bitget.close_short(round(slot.short_qty, 6))

        upbit_pnl  = slot.coin_qty * (px["upbit_px"] - slot.upbit_entry_px)
        bitget_pnl = slot.short_qty * (slot.bitget_entry_px - px["bitget_px"]) * usd_krw
        gross_pnl  = upbit_pnl + bitget_pnl
        hold_h     = (time.time() - slot.entry_time) / 3600
        fee_krw    = (0.0005 * 2 + 0.0006 * 2) * self.cpg
        slip_krw   = 0.0005 * 2 * self.cpg
        fund_krw   = (hold_h / 8) * 0.0001 * self.cpg
        net_pnl    = gross_pnl - fee_krw - slip_krw + fund_krw

        k = self.slots.index(slot)
        _append_trade({
            "trade_id":        trade_id,
            "coin":            self.coin,
            "grid_k":          k,
            "grid_level":      slot.grid_level,
            "exit_level":      slot.exit_level,
            "entry_dt":        datetime.fromtimestamp(slot.entry_time, tz=timezone.utc).isoformat(),
            "exit_dt":         datetime.now(tz=timezone.utc).isoformat(),
            "hold_hours":      round(hold_h, 2),
            "entry_premium":   round(slot.entry_premium, 4),
            "exit_premium":    round(px["premium_pct"], 4),
            "entry_upbit_px":  slot.upbit_entry_px,
            "entry_bitget_px": slot.bitget_entry_px,
            "entry_usd_krw":   slot.entry_usd_krw,
            "exit_upbit_px":   px["upbit_px"],
            "exit_bitget_px":  px["bitget_px"],
            "exit_usd_krw":    usd_krw,
            "coin_qty":        round(slot.coin_qty, 8),
            "short_qty":       round(slot.short_qty, 8),
            "upbit_pnl":       round(upbit_pnl, 0),
            "bitget_pnl":      round(bitget_pnl, 0),
            "gross_pnl":       round(gross_pnl, 0),
            "fee_krw":         round(fee_krw, 0),
            "slip_krw":        round(slip_krw, 0),
            "funding_krw":     round(fund_krw, 0),
            "net_pnl":         round(net_pnl, 0),
            "capital_per_grid": self.cpg,
        })
        logger.info(
            f"  [{self.coin}] ✅ 청산 완료  보유 {hold_h:.1f}h  순수익 {net_pnl/10000:+.3f}만원"
        )

        slot.active = False
        slot.coin_qty = slot.short_qty = 0.0
        slot.entry_premium = slot.upbit_entry_px = 0.0
        slot.bitget_entry_px = slot.entry_usd_krw = slot.entry_time = 0.0

    def tick(self, usd_krw: float, trade_counter_ref: list):
        try:
            px   = self.get_premium(usd_krw)
            prem = px["premium_pct"]
            logger.info(
                f"  [{self.coin}] 프리미엄 {prem:+.2f}%  "
                f"활성 {self.active_count()}/{self.cfg['n_grids']}슬롯"
            )
            for slot in self.slots:
                if not slot.active:
                    if prem <= slot.grid_level:
                        self.enter_slot(slot, px, usd_krw)
                else:
                    if prem >= slot.exit_level:
                        trade_counter_ref[0] += 1
                        self.exit_slot(slot, px, usd_krw, trade_counter_ref[0])
        except Exception as e:
            logger.error(f"  [{self.coin}] 오류: {e}", exc_info=True)


class MultiGridTrader:
    def __init__(self):
        _ensure_log()
        self.grids          = {coin: CoinGrid(coin, cfg) for coin, cfg in COINS.items()}
        self._trade_counter = [self._load_counter()]

    def _load_counter(self) -> int:
        if not os.path.exists(TRADE_LOG):
            return 0
        with open(TRADE_LOG, encoding="utf-8") as f:
            return max(0, sum(1 for _ in f) - 1)

    def run(self):
        total_capital = sum(cfg["upbit_capital"] + cfg["upbit_capital"] // 9
                            for cfg in COINS.values())
        logger.info("=== 멀티 코인 그리드 트레이딩 봇 시작 ===")
        logger.info(f"운영 코인: {list(COINS.keys())}  |  총 자본 약 {total_capital//10000}만원")
        for coin, g in self.grids.items():
            lvls = "  ".join(f"{s.grid_level:+.1f}%→{s.exit_level:+.1f}%" for s in g.slots)
            logger.info(f"  [{coin}] {g.cfg['n_grids']}슬롯  간격 {g.spacing:.2f}%  슬롯당 {g.cpg/10000:.0f}만원  |  {lvls}")
        logger.info(f"거래 로그: {TRADE_LOG}")

        while True:
            try:
                usd_krw = get_usd_krw()
                logger.info(f"[폴링] 환율 {usd_krw:,.0f}  거래 누계 {self._trade_counter[0]}건")
                for g in self.grids.values():
                    g.tick(usd_krw, self._trade_counter)
            except KeyboardInterrupt:
                logger.info("사용자 중단 요청")
                for coin, g in self.grids.items():
                    n = g.active_count()
                    if n > 0:
                        levels = ", ".join(f"{s.grid_level:+.1f}%" for s in g.slots if s.active)
                        logger.warning(f"  [{coin}] 활성 포지션 {n}개: {levels} — 수동 확인 필요")
                break
            except Exception as e:
                logger.error(f"오류: {e}", exc_info=True)
            time.sleep(POLL_INTERVAL)
