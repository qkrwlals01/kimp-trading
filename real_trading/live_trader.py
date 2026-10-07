"""
실거래 실행기 — 빗썸 원화 현물 매수 + 비트겟 USDT 선물 숏 (김프 전략)
────────────────────────────────────────────────────────────────────
전략 판단은 모의매매 코드(paper_trader.PaperCoinGrid)를 그대로 상속한다 — 진입 필터, 익절 진입 환율 고정,
시간손절 없음, 가상손절, 익절폭은 모의매매와 같다. 바꾸는 것은 네 가지뿐이다.
  호가   업비트 → 빗썸 (형식이 같다)
  체결   호가를 가정 → 실제 주문 (드라이런은 빗썸 호가창 15단계를 훑어 체결을 흉내)
  상태   메모리 → 파일 (logs/live_state.json) — 재시작해도 열린 슬롯이 이어진다
  기록   실제 체결가·수수료 (logs/live_trades.csv)

주문 순서
  진입  ① 빗썸 즉시체결(IOC) 지정가 매수 — 최우선 매도호가 +0.2% 까지만
        ② 체결 수량을 비트겟 수량 단위로 내린 만큼 비트겟 시장가 숏 → 두 쪽 코인 수량이 같다
        ②가 실패하면 ①을 바로 되판다. 되팔기도 실패하면 멈춘다 (수동 확인)
        단위 아래 자투리는 5,000원 이상이면 되팔고 아니면 기록만 한다
  청산  ① 빗썸 IOC 지정가 매도 — 남으면 가격 하한을 0.2 → 0.5 → 1% 로 넓혀 최대 3번
        ② 판 수량만큼 비트겟 숏 시장가 청산 — 실패하면 3번까지, 그래도 안 되면 멈춘다

안전장치
  - 기본은 드라이런 (주문 없음). --live 는 확인 입력(LIVE-<열린 금액 상한>)이 있어야 시작
  - 실거래 시작 때 상태 파일과 거래소(빗썸 코인 잔고, 비트겟 숏 수량)를 대조 — 다르면 멈춘다
  - 열린 금액 상한, 연속 오류 상한, 실패한 코인·슬롯은 1분 쉼
  - logs/STOP 파일: 신규 진입 중지 (청산은 계속) / logs/FLATTEN 파일: 전부 청산하고 멈춤
  - 멈춘 상태(halted)는 상태 파일에 남는다. 원인을 해결한 뒤 상태 파일의 halted 를 null 로 고쳐야 다시 돈다

진입 필터 기록 (logs/live_kimp_hist.csv): 빗썸 김프는 호가 로거에 없어서 이 실행기가 직접 쌓는다.
  처음에는 24시간 창의 25%(6시간)가 쌓일 때까지 진입하지 않는다 → 실거래 전에 드라이런을 하루 돌려 두면 된다.

실행
  python -m real_trading.live_trader --check                  # 읽기 전용 점검: 키·잔고·수수료·비트겟 계정 종류
  python -m real_trading.live_trader                          # 드라이런 (주문 없음)
  python -m real_trading.live_trader --dry-cycle              # 드라이런 시험: 코인마다 바로 1번 진입 → 다음 폴링에 청산
  python -m real_trading.live_trader --live                   # 실거래
  python -m real_trading.live_trader --live --roundtrip XRP   # 최소 수량 1왕복 (실제 수수료·환급·주문 확인용)
  python -m real_trading.live_trader --live --flatten         # 열린 슬롯 전부 청산
"""

import os, sys, csv, json, math, time, uuid, logging, argparse, dataclasses
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paper_trading import paper_trader as pt                     # 전략 규칙 (PaperCoinGrid)
from paper_trading.paper_settings import PAPER_COINS, PAPER_FEE_KR, PAPER_BG_REBATE, PAPER_POLL_INTERVAL
from real_trading import live_settings as LS
from real_trading.exchanges import Bithumb, BitgetFutures, ExchangeError, floor_step, load_keys

BG_TAKER = 0.0006            # 비트겟 테이커 정가. 수수료 환급(PAPER_BG_REBATE)은 다음날 들어오므로 따로 추정한다
MIN_KRW = 5000               # 빗썸 최소 주문 금액
SLOT_FIELDS = [f.name for f in dataclasses.fields(pt.FloatingSlot)]
TRADE_HEADER = [
    "trade_id", "mode", "coin", "reason", "entry_time", "exit_time", "hold_hours", "qty",
    "entry_kimp", "target_kimp", "exit_kimp_fx0",
    "bith_buy_avg", "bith_sell_avg", "bg_open_avg", "bg_close_avg", "entry_fx", "usdt_krw",
    "bith_fee_krw", "bg_fee_usdt", "funding_est_krw", "gross_krw", "net_krw", "rebate_est_krw", "net_with_rebate_krw",
    "orders",
]
log = logging.getLogger("live")


def setup_logging(suffix: str):
    """paper_trader 를 불러올 때 붙은 trading.log 핸들러를 떼고 실거래 로그로 바꾼다
    (같은 서버에서 돌려도 모의매매 로그에 섞이지 않게)."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    os.makedirs(LS.LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", "%Y-%m-%d %H:%M:%S")
    for h in (logging.StreamHandler(), logging.FileHandler(LS.LIVE_LOG.format(suffix), encoding="utf-8")):
        h.setFormatter(fmt)
        root.addHandler(h)
    root.setLevel(logging.INFO)
    pt.logger.setLevel(logging.WARNING)          # 10초마다 찍는 코인별 김프 줄은 끈다 (가상손절 경고는 남김)


def live_cfgs() -> dict:
    out = {}
    for c in LS.LIVE_COINS:
        cfg = dict(PAPER_COINS[c])
        cfg.update(n_slots=LS.LIVE_N_SLOTS, upbit_capital=LS.LIVE_SLOT_KRW * LS.LIVE_N_SLOTS, leverage=LS.LIVE_LEVERAGE)
        out[c] = cfg
    return out


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()


class LiveGrid(pt.PaperCoinGrid):
    """판단은 PaperCoinGrid 그대로, 진입·청산만 실제 주문(Executor)으로."""
    def __init__(self, coin: str, cfg: dict, ex: "Executor"):
        super().__init__(coin, cfg)
        self.ex = ex

    def _enter_slot(self, slot, q, usd_krw):
        self.ex.enter(self, slot, q, usd_krw)

    def _exit_slot(self, slot, q, upbit_usdt_krw, trade_id, reason="익절"):
        self.ex.exit(self, slot, q, upbit_usdt_krw, trade_id, reason)


class Executor:
    """주문 실행·기록·상태 저장. live=False 면 빗썸 호가창으로 체결을 흉내 낸다 (주문 없음)."""

    def __init__(self, live: bool, bithumb: Bithumb, bitget: BitgetFutures, specs: dict, suffix: str = None):
        self.live, self.bh, self.bg, self.specs = live, bithumb, bitget, specs
        self.suffix = ("" if live else "_dry") if suffix is None else suffix
        self.grids, self.books = {}, {}
        self.usdt_krw = None
        self.halted = None          # 멈춘 사유 — 헤지가 깨졌거나 거래소와 기록이 다를 때
        self.errors = 0
        self.cool = {}              # coin → 다시 시도해도 되는 시각
        self.trade_id = 0
        self._noted = {}

    # ── 공통 ──
    def _cid(self, tag: str, coin: str) -> str:
        return f"k{tag}{coin}{int(time.time() * 1000) % 10**10}{uuid.uuid4().hex[:6]}"[:32]

    def _note(self, coin: str, msg: str):
        """같은 사유는 1분에 한 번만 남긴다"""
        k = (coin, msg)
        if time.time() - self._noted.get(k, 0) > 60:
            log.info(f"  [{coin}] 진입 보류: {msg}")
            self._noted[k] = time.time()

    def _fail(self, coin: str, msg: str, count: bool = True):
        if count:
            self.errors += 1
        self.cool[coin] = time.time() + LS.LIVE_RETRY_S
        log.warning(f"  [{coin}] {msg} (연속 오류 {self.errors})")

    def _halt(self, why: str):
        if not self.halted:
            log.critical(f"!! 멈춤: {why} — 거래소에서 직접 확인하고 해결한 뒤 상태 파일의 halted 를 null 로 고치세요")
        self.halted = why
        self.save()

    def open_krw(self) -> float:
        return sum(s.coin_qty * s.upbit_entry_px for g in self.grids.values() for s in g.slots if s.active)

    def blocked(self, coin: str):
        if self.halted:
            return f"멈춤 ({self.halted})"
        if os.path.exists(LS.STOP_FILE):
            return "STOP 파일"
        if self.errors >= LS.LIVE_MAX_ERRORS:
            return f"연속 오류 {self.errors}회"
        if time.time() < self.cool.get(coin, 0):
            return "실패 후 대기"
        return None

    # ── 빗썸 가격·체결 ──
    def _tick(self, market: str) -> float:
        prices = sorted({p for u in self.books[market]["units"] for p in (u[0], u[2])})
        gaps = [b - a for a, b in zip(prices, prices[1:]) if b - a > 1e-12]
        return min(gaps) if gaps else max(prices[0] * 1e-4, 1e-8)

    def _limit(self, market: str, side: str, slip: float) -> float:
        """즉시체결 지정가: 매수는 최우선 매도호가 위로, 매도는 최우선 매수호가 아래로 slip 만큼 (호가 단위 배수)"""
        bid, _, ask, _ = self.books[market]["units"][0]
        tick = self._tick(market)
        base = ask if side == "bid" else bid
        k = max(1, math.ceil(base * slip / tick - 1e-9))
        return round(base + k * tick if side == "bid" else max(base - k * tick, tick), 8)

    def _sim_bithumb(self, market: str, side: str, qty: float, limit: float) -> dict:
        left, funds = qty, 0.0
        for bid, bid_sz, ask, ask_sz in self.books[market]["units"]:
            px, sz = (ask, ask_sz) if side == "bid" else (bid, bid_sz)
            if (side == "bid" and px > limit) or (side == "ask" and px < limit):
                break
            take = min(left, sz)
            funds += take * px
            left -= take
            if left <= 1e-12:
                break
        got = qty - max(left, 0.0)
        return {"qty": got, "avg": funds / got if got else 0.0, "funds": funds, "fee": funds * PAPER_FEE_KR,
                "state": "done", "id": "dry"}

    def _bithumb(self, market: str, side: str, qty: float, slip: float, coin: str) -> dict:
        limit = self._limit(market, side, slip)
        if not self.live:
            return self._sim_bithumb(market, side, qty, limit)
        oid = self.bh.place_ioc(market, side, qty, limit, self._cid("B", coin))
        return self.bh.wait_fill(oid, LS.LIVE_ORDER_TIMEOUT_S)

    def _sell_all(self, market: str, qty: float, coin: str) -> dict:
        """빗썸에서 qty 를 판다. 남으면 가격 하한을 넓혀 최대 3번. {qty, funds, fee, ids}"""
        step = self.specs[self.grids[coin].cfg["bitget_symbol"]]["step"]
        out = {"qty": 0.0, "funds": 0.0, "fee": 0.0, "ids": []}
        for slip in LS.LIVE_EXIT_SLIPPAGE:
            left = qty - out["qty"]
            if left <= step * 1e-6 or left * self.books[market]["units"][0][0] < MIN_KRW * 0.5:
                break
            try:
                r = self._bithumb(market, "ask", round(left, 8), slip, coin)
            except Exception as e:
                log.error(f"  [{coin}] 빗썸 매도 오류: {e}")
                break
            out["qty"] += r["qty"]
            out["funds"] += r["funds"]
            out["fee"] += r["fee"]
            out["ids"].append(r["id"])
        return out

    # ── 비트겟 ──
    def _bitget(self, q: dict, coin: str, qty: float, close: bool) -> dict:
        sym = self.grids[coin].cfg["bitget_symbol"]
        if not self.live:
            px = q["bg_ask"] if close else q["bg_bid"]
            return {"qty": qty, "avg": px, "fee": qty * px * BG_TAKER, "id": "dry"}
        fn = self.bg.close_short if close else self.bg.open_short
        oid = fn(sym, qty, self._cid("G", coin))
        return self.bg.wait_fill(sym, oid, qty, LS.LIVE_ORDER_TIMEOUT_S)

    # ── 진입 ──
    def entry_qty(self, grid, q: dict) -> float:
        spec = self.specs[grid.cfg["bitget_symbol"]]
        qty = floor_step(LS.LIVE_SLOT_KRW / q["up_ask"], spec["step"])
        if qty < spec["min_qty"] or qty * q["bg_bid"] < spec["min_usdt"] or qty * q["up_ask"] < MIN_KRW:
            return 0.0
        return qty

    def _funds_ok(self, coin: str, qty: float, q: dict) -> bool:
        """실거래: 빗썸 원화와 비트겟 증거금이 충분한지 (형식을 모르는 값은 건너뜀)"""
        try:
            krw = self.bh.accounts().get("KRW", (0.0, 0.0))[0]
            need = qty * q["up_ask"] * (1 + LS.LIVE_MAX_SLIPPAGE) * (1 + PAPER_FEE_KR) + 100
            if krw < need:
                self._fail(coin, f"빗썸 원화 부족 {krw:,.0f} < {need:,.0f}", count=False)
                return False
            avail = self.bg.usdt_available()
            margin = qty * q["bg_bid"] / LS.LIVE_LEVERAGE * 1.2
            if avail is not None and avail < margin:
                self._fail(coin, f"비트겟 증거금 부족 {avail:.2f} < {margin:.2f} USDT", count=False)
                return False
        except ExchangeError as e:
            self._fail(coin, f"잔고 확인 실패: {e}")
            return False
        return True

    def enter(self, grid, slot, q: dict, fx: float, qty: float = None, force: bool = False):
        coin, market = grid.coin, grid.cfg["upbit_market"]
        why = None if force and not self.live else self.blocked(coin)
        if why:
            self._note(coin, why)
            return False
        qty = qty or self.entry_qty(grid, q)
        if qty <= 0:
            self._note(coin, "슬롯 금액이 최소 주문 단위보다 작음")
            return False
        if self.open_krw() + qty * q["up_ask"] > LS.LIVE_MAX_OPEN_KRW:
            self._note(coin, f"열린 금액 상한 {LS.LIVE_MAX_OPEN_KRW:,}원")
            return False
        if self.live and not self._funds_ok(coin, qty, q):
            return False
        step = self.specs[grid.cfg["bitget_symbol"]]["step"]

        try:
            buy = self._bithumb(market, "bid", qty, LS.LIVE_MAX_SLIPPAGE, coin)
        except Exception as e:
            self._fail(coin, f"빗썸 매수 실패: {e}")
            return False
        if buy["qty"] <= 0:
            self._fail(coin, "빗썸 매수 미체결 (가격 상한 안에 물량 없음)", count=False)
            return False

        hedge = floor_step(buy["qty"], step)
        spec = self.specs[grid.cfg["bitget_symbol"]]
        if hedge < spec["min_qty"] or hedge * q["bg_bid"] < spec["min_usdt"]:
            self._unwind(grid, q, buy, "체결 수량이 비트겟 최소 단위 미만")
            return False
        try:
            short = self._bitget(q, coin, hedge, close=False)
        except Exception as e:
            self._unwind(grid, q, buy, f"비트겟 숏 실패: {e}")
            return False
        if short["qty"] <= 0:
            self._unwind(grid, q, buy, "비트겟 숏 미체결")
            return False

        extra = buy["qty"] - short["qty"]             # 헤지 안 된 코인 (자투리·비트겟 부분 체결)
        part = short["qty"] / buy["qty"]
        if extra > step * 1e-6:
            if extra * q["up_bid"] >= MIN_KRW:
                sold = self._sell_all(market, extra, coin)
                left = extra - sold["qty"]
                self._record_unwind(coin, "자투리 매도", buy, extra, sold, q)
                if left * q["up_bid"] >= MIN_KRW:
                    self._halt(f"{coin} 헤지 안 된 코인 {left:g} 이 남음 (자투리 매도 실패)")
            else:
                log.info(f"  [{coin}] 헤지 단위 아래 자투리 {extra:g} 개는 빗썸에 그대로 둠 (최소 주문 미만)")

        now = time.time()
        k0 = pt._kimp(buy["avg"], short["avg"], fx)
        slot.active = True
        slot.entry_premium, slot.target_premium = k0, k0 + grid.spacing
        slot.coin_qty = slot.short_qty = short["qty"]
        slot.upbit_entry_px, slot.bitget_entry_px = buy["avg"], short["avg"]
        slot.upbit_entry_mid = (q["up_bid"] + q["up_ask"]) / 2
        slot.bitget_entry_mid = (q["bg_bid"] + q["bg_ask"]) / 2
        slot.entry_usd_krw, slot.entry_time = fx, now
        slot.funding_krw, slot.funding_t = 0.0, now
        slot.live = {"buy_fee_krw": buy["fee"] * part, "open_fee_usdt": short["fee"],
                     "orders": [buy["id"], short["id"]], "slip_buy": buy["avg"] / q["up_ask"] - 1,
                     "slip_open": 1 - short["avg"] / q["bg_bid"]}
        self.errors = 0
        self.save()
        log.info(f"  [{coin}] ▶ 진입  김프={k0:.3f}%  목표={slot.target_premium:.3f}%  수량={short['qty']:g}  "
                 f"빗썸 {buy['avg']:,.4f}원 비트겟 {short['avg']:.6f} USDT  ({'실거래' if self.live else '드라이런'})")
        return True

    def _unwind(self, grid, q: dict, buy: dict, why: str):
        """진입 2단계가 실패하면 1단계에서 산 코인을 되판다. 다 못 팔면 멈춘다."""
        coin, market = grid.coin, grid.cfg["upbit_market"]
        log.error(f"  [{coin}] {why} → 빗썸 {buy['qty']:g} 개 되팔기")
        sold = self._sell_all(market, buy["qty"], coin)
        self._record_unwind(coin, "진입 되돌림", buy, buy["qty"], sold, q)
        left = buy["qty"] - sold["qty"]
        if left * q["up_bid"] >= MIN_KRW:
            self._halt(f"{coin} 진입 되돌림 실패 — 빗썸에 {left:g} 개가 헤지 없이 남음 ({why})")
        self._fail(coin, why)

    def _record_unwind(self, coin: str, reason: str, buy: dict, qty: float, sold: dict, q: dict):
        """되돌림·자투리 매도 비용도 거래 기록에 남긴다 (손익 합계에서 비용을 빠뜨리지 않게)"""
        part = qty / buy["qty"] if buy["qty"] else 0.0
        cost_in = buy["funds"] * part + buy["fee"] * part
        got = sold["funds"] - sold["fee"]
        unsold_value = (qty - sold["qty"]) * q["up_bid"]                 # 못 판 몫은 지금 매수호가로 평가
        net = got + unsold_value - cost_in
        self.trade_id += 1
        self._append({"trade_id": self.trade_id, "mode": "live" if self.live else "dry", "coin": coin, "reason": reason,
                      "entry_time": iso(time.time()), "exit_time": iso(time.time()), "hold_hours": 0, "qty": round(qty, 8),
                      "bith_buy_avg": buy["avg"], "bith_sell_avg": sold["funds"] / sold["qty"] if sold["qty"] else "",
                      "bith_fee_krw": round(buy["fee"] * part + sold["fee"], 2), "gross_krw": round(net, 2),
                      "net_krw": round(net, 2), "net_with_rebate_krw": round(net, 2),
                      "orders": "|".join([buy["id"]] + sold["ids"])})

    # ── 청산 ──
    def exit(self, grid, slot, q: dict, usdt_krw: float, trade_id: int, reason: str = "익절"):
        coin, market = grid.coin, grid.cfg["upbit_market"]
        if self.halted:
            return False
        if time.time() < getattr(slot, "retry_at", 0):
            return False
        step = self.specs[grid.cfg["bitget_symbol"]]["step"]
        want = slot.coin_qty
        sold = self._sell_all(market, want, coin)
        if sold["qty"] <= 0:
            slot.retry_at = time.time() + LS.LIVE_RETRY_S
            self._fail(coin, f"{reason} 청산: 빗썸 매도 미체결 — {LS.LIVE_RETRY_S}초 뒤 다시")
            return False
        close_qty = min(slot.short_qty, floor_step(sold["qty"] + step * 1e-6, step))
        if close_qty < step * 0.5:
            self._halt(f"{coin} 빗썸 매도가 {sold['qty']:g} 개만 체결돼 비트겟 수량 단위({step:g})에 못 미침 — 숏 그대로, 코인 일부만 팔림")
            return False
        closed = None
        for i in range(3):
            try:
                closed = self._bitget(q, coin, close_qty, close=True)
                break
            except Exception as e:
                log.error(f"  [{coin}] 비트겟 숏 청산 실패 {i + 1}/3: {e}")
                time.sleep(1)
        if closed is None or closed["qty"] <= 0:
            self._halt(f"{coin} 빗썸은 {sold['qty']:g} 개 팔았는데 비트겟 숏 청산 실패 — 숏만 남음")
            return False

        frac = closed["qty"] / slot.short_qty if slot.short_qty else 1.0
        lv = getattr(slot, "live", {}) or {}
        sell_avg = sold["funds"] / sold["qty"]
        buy_fee = lv.get("buy_fee_krw", 0.0) * frac
        open_fee = lv.get("open_fee_usdt", 0.0) * frac
        krw_leg = sold["funds"] - sold["fee"] - sold["qty"] * slot.upbit_entry_px - buy_fee
        bg_leg = closed["qty"] * (slot.bitget_entry_px - closed["avg"]) - open_fee - closed["fee"]
        funding = slot.funding_krw * frac
        gross = sold["qty"] * (sell_avg - slot.upbit_entry_px) + closed["qty"] * (slot.bitget_entry_px - closed["avg"]) * usdt_krw
        net = krw_leg + bg_leg * usdt_krw + funding
        rebate = (open_fee + closed["fee"]) * PAPER_BG_REBATE * usdt_krw
        self.trade_id = max(self.trade_id + 1, trade_id)
        self._append({
            "trade_id": self.trade_id, "mode": "live" if self.live else "dry", "coin": coin, "reason": reason,
            "entry_time": iso(slot.entry_time), "exit_time": iso(time.time()),
            "hold_hours": round((time.time() - slot.entry_time) / 3600, 3), "qty": closed["qty"],
            "entry_kimp": round(slot.entry_premium, 4), "target_kimp": round(slot.target_premium, 4),
            "exit_kimp_fx0": round(pt._kimp(sell_avg, closed["avg"], slot.entry_usd_krw), 4),
            "bith_buy_avg": slot.upbit_entry_px, "bith_sell_avg": round(sell_avg, 8),
            "bg_open_avg": slot.bitget_entry_px, "bg_close_avg": closed["avg"],
            "entry_fx": slot.entry_usd_krw, "usdt_krw": usdt_krw,
            "bith_fee_krw": round(buy_fee + sold["fee"], 2), "bg_fee_usdt": round(open_fee + closed["fee"], 6),
            "funding_est_krw": round(funding, 2), "gross_krw": round(gross, 2), "net_krw": round(net, 2),
            "rebate_est_krw": round(rebate, 2), "net_with_rebate_krw": round(net + rebate, 2),
            "orders": "|".join(lv.get("orders", []) + sold["ids"] + [closed["id"]]),
        })
        log.info(f"  [{coin}] ◀ {reason}  수량 {closed['qty']:g}  순손익 {net:+,.0f}원 (환급 추정 +{rebate:,.0f}원)  "
                 f"보유 {(time.time() - slot.entry_time) / 3600:.1f}h")

        left_coin, left_short = slot.coin_qty - sold["qty"], slot.short_qty - closed["qty"]
        if left_short <= step * 1e-6 and left_coin * q["up_bid"] < MIN_KRW:
            for k in SLOT_FIELDS:                                        # 슬롯 비우기 (PaperCoinGrid 와 같은 초기값)
                setattr(slot, k, False if k == "active" else 0.0)
            slot.live, slot.retry_at = {}, 0
        else:                                                            # 일부만 청산됨 — 남은 만큼 슬롯 유지
            slot.coin_qty, slot.short_qty = max(left_coin, 0.0), max(left_short, 0.0)
            slot.funding_krw *= (1 - frac)
            lv["buy_fee_krw"], lv["open_fee_usdt"] = lv.get("buy_fee_krw", 0.0) - buy_fee, lv.get("open_fee_usdt", 0.0) - open_fee
            if abs(slot.coin_qty - slot.short_qty) * q["up_bid"] >= MIN_KRW:
                self._halt(f"{coin} 일부만 청산돼 빗썸 {slot.coin_qty:g} / 비트겟 숏 {slot.short_qty:g} 로 어긋남")
        self.errors = 0
        self.save()
        return True

    # ── 기록·상태 ──
    def _append(self, row: dict):
        path = LS.TRADE_LOG.format(self.suffix)
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=TRADE_HEADER)
            if new:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in TRADE_HEADER})

    def save(self):
        st = {"version": 1, "mode": "live" if self.live else "dry", "saved_at": iso(time.time()),
              "halted": self.halted, "trade_id": self.trade_id,
              "slots": {c: [dict({k: getattr(s, k) for k in SLOT_FIELDS}, live=getattr(s, "live", {}))
                            for s in g.slots if s.active] for c, g in self.grids.items()}}
        path = LS.STATE_FILE.format(self.suffix)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(path + ".tmp", path)

    def load(self) -> int:
        path = LS.STATE_FILE.format(self.suffix)
        if not os.path.exists(path):
            return 0
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        self.halted, self.trade_id, n = st.get("halted"), int(st.get("trade_id") or 0), 0
        for coin, rows in (st.get("slots") or {}).items():
            g = self.grids.get(coin)
            if g is None:
                if rows:
                    self.halted = self.halted or f"상태 파일에 운영하지 않는 코인 {coin} 의 열린 슬롯이 있음"
                continue
            free = [s for s in g.slots if not s.active]
            for row in rows:
                if not free:
                    self.halted = self.halted or f"{coin} 상태 파일 슬롯이 슬롯 수보다 많음"
                    break
                s = free.pop(0)
                for k in SLOT_FIELDS:
                    setattr(s, k, row[k])
                s.live = row.get("live") or {}
                n += 1
        return n

    def reconcile(self) -> list:
        """실거래 시작 전: 상태 파일과 거래소를 맞춰 본다. 다르면 멈춘다."""
        issues = []
        bal = self.bh.accounts()
        for coin, g in self.grids.items():
            sym = g.cfg["bitget_symbol"]
            step = self.specs[sym]["step"]
            want = sum(s.coin_qty for s in g.slots if s.active)
            have = sum(bal.get(coin, (0.0, 0.0)))
            if have + 1e-9 < want * 0.999:
                issues.append(f"{coin}: 빗썸 보유 {have:g} < 기록 {want:g}")
            short_want = sum(s.short_qty for s in g.slots if s.active)
            short_have = self.bg.short_size(sym)
            if abs(short_have - short_want) > step / 2:
                issues.append(f"{coin}: 비트겟 숏 {short_have:g} ≠ 기록 {short_want:g}")
        if issues:
            self._halt("거래소와 기록이 다름 — " + "; ".join(issues))
        return issues


# ── 시세 ─────────────────────────────────────────────────────────────

def fetch_quotes(bh: Bithumb, markets: list) -> tuple:
    """빗썸 호가창(전체 단계)과, 모의매매와 같은 형식의 up/bg 묶음"""
    from real_trading.quote_logger import fetch_bitget
    books = bh.orderbooks(markets + ["KRW-USDT"])
    up = {m: (b["units"][0][0], b["units"][0][2], b["units"][0][1], b["units"][0][3], b["ts"]) for m, b in books.items()}
    return books, up, fetch_bitget()


class KimpHistory:
    """진입 필터용 빗썸 진입김프 기록. 재시작해도 24시간 분포가 남도록 파일에 쌓는다."""
    def __init__(self, path: str, window_s: float):
        self.path, self.window_s = path, window_s

    def warm(self, grids: dict) -> int:
        if not os.path.exists(self.path):
            return 0
        cutoff, keep = time.time() - self.window_s, []
        with open(self.path, encoding="utf-8") as f:
            for row in csv.reader(f):
                try:
                    t, coin, prem = float(row[0]), row[1], float(row[2])
                except (ValueError, IndexError):
                    continue
                if t >= cutoff and coin in grids:
                    grids[coin].observe(t, prem)
                    keep.append(row)
        with open(self.path, "w", newline="", encoding="utf-8") as f:     # 창 밖 기록은 정리
            csv.writer(f).writerows(keep)
        return len(keep)

    def append(self, rows: list):
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)


# ── 실행 ─────────────────────────────────────────────────────────────

def build(live: bool, need_keys: bool):
    keys = load_keys()
    bh = Bithumb(*keys["bithumb"]) if need_keys else Bithumb()
    bg = BitgetFutures(*keys["bitget"], margin_mode=LS.LIVE_MARGIN_MODE) if need_keys else BitgetFutures()
    cfgs = live_cfgs()
    specs = BitgetFutures.contracts([c["bitget_symbol"] for c in cfgs.values()])
    ex = Executor(live, bh, bg, specs)
    grids = {c: LiveGrid(c, cfg, ex) for c, cfg in cfgs.items()}
    ex.grids = grids
    return ex, grids


def prepare_live(ex: Executor, grids: dict) -> bool:
    """실거래 준비: 비트겟 계정 종류·모드 확인, 레버리지 설정, 거래소와 기록 대조."""
    first = next(iter(grids.values())).cfg["bitget_symbol"]
    try:
        info = ex.bg.detect(first)
    except ExchangeError as e:
        log.critical(f"비트겟 계정 확인 실패: {e}")
        return False
    log.info(f"비트겟 {'통합계정(v3)' if info['api'] == 'v3' else '기존계정(v2)'} · 포지션 모드 {info['hold'] or '알 수 없음'} ({info['hold_raw']})")
    if info["hold"] is None:
        ex._halt(f"비트겟 포지션 모드를 읽지 못함 ({info['hold_raw']!r}) — --check 결과를 확인")
        return False
    for g in grids.values():
        try:
            ex.bg.set_leverage(g.cfg["bitget_symbol"], LS.LIVE_LEVERAGE)
        except ExchangeError as e:
            log.warning(f"  [{g.coin}] 레버리지 {LS.LIVE_LEVERAGE}배 설정 실패 — 앱에서 확인하세요: {e}")
    return not ex.reconcile()


def confirm_live(args) -> bool:
    token = f"LIVE-{LS.LIVE_MAX_OPEN_KRW}"
    if args.confirm == token:
        return True
    print(f"\n실거래입니다. 빗썸·비트겟에 실제 주문이 나갑니다. 열린 금액 상한 {LS.LIVE_MAX_OPEN_KRW:,}원, "
          f"슬롯 {LS.LIVE_SLOT_KRW:,}원 × {LS.LIVE_N_SLOTS} × {len(LS.LIVE_COINS)}코인, 레버리지 {LS.LIVE_LEVERAGE}배")
    try:
        return input(f"계속하려면 {token} 을 입력하세요: ").strip() == token
    except EOFError:
        return False


def run(args) -> int:
    live = args.live
    setup_logging("" if live else "_dry")
    ex, grids = build(live, need_keys=live)
    n = ex.load()
    log.info(f"=== 실거래 실행기 시작 ({'실거래' if live else '드라이런 — 주문 없음'}) ===")
    log.info(f"코인 {list(grids)} · 슬롯 {LS.LIVE_SLOT_KRW:,}원 × {LS.LIVE_N_SLOTS} · 레버리지 {LS.LIVE_LEVERAGE}배 · "
             f"열린 금액 상한 {LS.LIVE_MAX_OPEN_KRW:,}원 · 상태 파일 슬롯 {n}개" + (f" · 멈춤 상태: {ex.halted}" if ex.halted else ""))
    if live:
        if not confirm_live(args):
            print("취소했습니다.")
            return 1
        if not prepare_live(ex, grids):
            log.critical("실거래 준비 실패 — 위 내용을 해결한 뒤 다시 시작하세요")
            return 1

    window = max(g.window_s for g in grids.values())
    hist = KimpHistory(LS.HIST_FILE, window)
    log.info(f"진입 필터 기록 {hist.warm(grids):,}개를 불러옴 (24시간 창의 25% 가 쌓여야 진입 판단)")
    from utils.exchange_rate import get_usd_krw
    markets = [g.cfg["upbit_market"] for g in grids.values()]
    counter, last_status, cycle = [ex.trade_id], 0.0, {"stage": 0} if args.dry_cycle else None
    if args.roundtrip or args.flatten:
        return one_shot(args, ex, grids, markets, get_usd_krw)

    while True:
        t0 = time.time()
        try:
            if os.path.exists(LS.FLATTEN_FILE):
                log.warning("FLATTEN 파일 → 열린 슬롯을 전부 청산하고 멈춥니다")
                rc = one_shot(argparse.Namespace(roundtrip=None, flatten=True), ex, grids, markets, get_usd_krw)
                ex._halt("FLATTEN 파일로 전부 청산")
                return rc
            fx = get_usd_krw()
            ex.books, up, bg = fetch_quotes(ex.bh, markets)
            ex.usdt_krw = up["KRW-USDT"][0]
            if cycle is not None:                       # 드라이런 시험: 바로 진입 → 다음 폴링에 청산
                dry_cycle_step(cycle, ex, grids, up, bg, fx)
            for g in grids.values():
                g.tick(up, bg, fx, ex.usdt_krw, counter)
            ex.trade_id = max(ex.trade_id, counter[0])
            counter[0] = ex.trade_id
            hist.append([[round(t0, 1), c, round(pt._kimp(up[g.cfg["upbit_market"]][1], bg[g.cfg["bitget_symbol"]][1], fx), 5)]
                         for c, g in grids.items() if g.cfg["upbit_market"] in up and g.cfg["bitget_symbol"] in bg])
            ex.save()
            if t0 - last_status >= 60:
                status(ex, grids, up, bg, fx)
                last_status = t0
        except KeyboardInterrupt:
            log.info("사용자 중단 — 열린 슬롯은 상태 파일에 남아 있어 다시 시작하면 이어집니다")
            ex.save()
            return 0
        except Exception as e:
            ex.errors += 1
            log.error(f"폴링 오류: {e}", exc_info=ex.errors <= 2)
        time.sleep(max(1.0, PAPER_POLL_INTERVAL - (time.time() - t0)))


def status(ex: Executor, grids: dict, up: dict, bg: dict, fx: float):
    parts = []
    for c, g in grids.items():
        try:
            q = g.quote(up, bg, fx)
        except ValueError:
            continue
        th = g.entry_threshold()
        parts.append(f"{c} 김프 {q['entry_pct']:+.2f}% 경계 {'—' if th is None else f'{th:+.2f}%'} 슬롯 {g.active_count()}/{g.n_slots}")
    log.info("[상태] " + " | ".join(parts) + f" | 열린 {ex.open_krw():,.0f}원 | 오류 {ex.errors}" +
             (f" | 멈춤: {ex.halted}" if ex.halted else ""))


def dry_cycle_step(cycle: dict, ex: Executor, grids: dict, up: dict, bg: dict, fx: float):
    """드라이런 시험용: 필터와 상관없이 코인마다 1슬롯 진입 → 다음 폴링에 '시험 청산'"""
    if ex.live:
        return
    if cycle["stage"] == 0:
        for g in grids.values():
            slot = next(s for s in g.slots if not s.active)
            ex.enter(g, slot, g.quote(up, bg, fx), fx, force=True)
        cycle["stage"] = 1
    elif cycle["stage"] == 1:
        for g in grids.values():
            for s in [s for s in g.slots if s.active]:
                ex.exit(g, s, g.quote(up, bg, fx), ex.usdt_krw, ex.trade_id + 1, reason="시험 청산")
        cycle["stage"] = 2
        log.info("드라이런 시험 완료 — 거래 기록: " + LS.TRADE_LOG.format(ex.suffix))


def one_shot(args, ex: Executor, grids: dict, markets: list, get_usd_krw) -> int:
    """--roundtrip COIN: 최소 수량 진입 → 3초 → 청산 / --flatten: 열린 슬롯 전부 청산"""
    fx = get_usd_krw()
    ex.books, up, bg = fetch_quotes(ex.bh, markets)
    ex.usdt_krw = up["KRW-USDT"][0]
    if args.flatten:
        n = 0
        for g in grids.values():
            for s in [s for s in g.slots if s.active]:
                n += ex.exit(g, s, g.quote(up, bg, fx), ex.usdt_krw, ex.trade_id + 1, reason="전부 청산")
        log.info(f"전부 청산: {n}개")
        return 0
    coin = args.roundtrip.upper()
    if coin not in grids:
        print(f"운영 코인이 아닙니다: {coin} ({list(grids)})")
        return 1
    g = grids[coin]
    q = g.quote(up, bg, fx)
    spec = ex.specs[g.cfg["bitget_symbol"]]
    need = max(spec["min_qty"], spec["min_usdt"] * 1.1 / q["bg_bid"], MIN_KRW * 1.1 / q["up_ask"])
    qty = math.ceil(need / spec["step"] - 1e-9) * spec["step"]
    log.info(f"[1왕복 시험] {coin} {qty:g} 개 (약 {qty * q['up_ask']:,.0f}원) 진입 → 3초 → 청산")
    slot = next(s for s in g.slots if not s.active)
    if not ex.enter(g, slot, q, fx, qty=qty, force=True):
        log.error("진입 실패 — 위 로그를 확인하세요")
        return 1
    time.sleep(3)
    ex.books, up, bg = fetch_quotes(ex.bh, markets)
    ex.exit(g, slot, g.quote(up, bg, fx), up["KRW-USDT"][0], ex.trade_id + 1, reason="1왕복 시험")
    log.info("거래 기록: " + LS.TRADE_LOG.format(ex.suffix) + " — 내일 비트겟 수수료 환급이 실제로 들어오는지 확인하세요")
    return 0


def check() -> int:
    """읽기 전용 점검. 주문은 내지 않는다. 키 값은 출력하지 않는다."""
    setup_logging("_check")
    keys = load_keys()
    ok = True
    cfgs = live_cfgs()
    coins = list(cfgs)
    need_krw = LS.LIVE_SLOT_KRW * LS.LIVE_N_SLOTS * len(coins)
    print(f"계획: 슬롯 {LS.LIVE_SLOT_KRW:,}원 × {LS.LIVE_N_SLOTS} × {len(coins)}코인 = 빗썸 최대 {need_krw:,}원, "
          f"비트겟 증거금 약 {need_krw / LS.LIVE_LEVERAGE:,.0f}원 ({LS.LIVE_LEVERAGE}배)\n")

    print("[빗썸]")
    bh = Bithumb(*keys["bithumb"])
    if not all(keys["bithumb"]):
        print("  X 키 없음 — config/settings.py 에 BITHUMB_ACCESS_KEY, BITHUMB_SECRET_KEY 를 넣으세요 (권한: 자산조회·주문조회·주문하기, 출금 X)")
        ok = False
    else:
        try:
            bal = bh.accounts()
            krw = bal.get("KRW", (0.0, 0.0))
            print(f"  {'O' if krw[0] >= need_krw else 'X'} 원화 주문 가능 {krw[0]:,.0f}원 (묶임 {krw[1]:,.0f}) — 필요 약 {need_krw:,}원")
            for c in coins:
                b = bal.get(c, (0.0, 0.0))
                print(f"    {c} 보유 {b[0] + b[1]:g}")
                ch = bh.chance(cfgs[c]["upbit_market"])
                bf, af = float(ch.get("bid_fee") or 0), float(ch.get("ask_fee") or 0)
                good = max(bf, af) <= PAPER_FEE_KR + 1e-9
                print(f"  {'O' if good else 'X'} {c} 수수료 매수 {bf * 100:.3f}% / 매도 {af * 100:.3f}% "
                      f"(모의매매 가정 {PAPER_FEE_KR * 100:.2f}%{'' if good else ' — 할인 요율 적용 확인'})")
                ok &= good
            ok &= krw[0] >= need_krw
        except ExchangeError as e:
            print(f"  X 조회 실패: {e}")
            ok = False

    print("\n[비트겟]")
    bg = BitgetFutures(*keys["bitget"], margin_mode=LS.LIVE_MARGIN_MODE)
    try:
        specs = BitgetFutures.contracts([c["bitget_symbol"] for c in cfgs.values()])
        info = bg.detect(next(iter(cfgs.values()))["bitget_symbol"])
        print(f"  {'O' if info['hold'] else 'X'} {'통합계정(v3)' if info['api'] == 'v3' else '기존계정(v2)'} · 포지션 모드 "
              f"{info['hold'] or '알 수 없음'} (원문 {info['hold_raw']!r})")
        ok &= bool(info["hold"])
        avail = bg.usdt_available()
        print(f"  {'?' if avail is None else 'O'} 선물 주문 가능 USDT {avail if avail is not None else '형식 확인 필요'}")
        for c, cfg in cfgs.items():
            sym = cfg["bitget_symbol"]
            print(f"    {sym} 숏 {bg.short_size(sym):g} 개 · 수량 단위 {specs[sym]['step']:g} · 최소 {specs[sym]['min_usdt']:g} USDT")
        if info["api"] == "v2" and info["raw"].get("v3_error"):
            print(f"    (v3 조회 실패라 v2 로 판단: {info['raw']['v3_error'][:120]})")
    except ExchangeError as e:
        print(f"  X 조회 실패: {e}")
        ok = False
    print(f"\n점검 결과: {'실거래 준비됨' if ok else '아직 준비 안 됨 — X 항목을 해결하세요'}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="빗썸 현물 + 비트겟 숏 김프 실행기 (기본: 드라이런)")
    ap.add_argument("--live", action="store_true", help="실제 주문 (확인 입력 필요)")
    ap.add_argument("--confirm", default=None, help="확인 입력을 대신하는 값 LIVE-<열린 금액 상한> (서비스로 돌릴 때)")
    ap.add_argument("--check", action="store_true", help="읽기 전용 점검")
    ap.add_argument("--roundtrip", default=None, metavar="COIN", help="최소 수량 1왕복 (--live 와 함께)")
    ap.add_argument("--flatten", action="store_true", help="열린 슬롯 전부 청산 (--live 와 함께)")
    ap.add_argument("--dry-cycle", action="store_true", help="드라이런 시험: 바로 진입 → 다음 폴링에 청산")
    a = ap.parse_args()
    if a.check:
        return check()
    if (a.roundtrip or a.flatten) and not a.live:
        print("--roundtrip / --flatten 은 실제 주문이라 --live 와 함께 써야 합니다.")
        return 1
    if a.dry_cycle and a.live:
        print("--dry-cycle 은 드라이런 전용입니다.")
        return 1
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
