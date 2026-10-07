"""
실거래 실행기 시험 — 가짜 거래소로 주문 흐름과 실패 처리를 확인한다 (네트워크·주문 없음)
실행: python -m pytest tests/test_live_trader.py -q
"""

import os, sys, json, logging

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from real_trading import live_trader as lt              # noqa: E402
from real_trading import live_settings as LS            # noqa: E402
from real_trading.exchanges import ExchangeError, floor_step   # noqa: E402

for h in list(logging.getLogger().handlers):            # paper_trader 를 불러오며 붙은 trading.log 핸들러 제거
    if isinstance(h, logging.FileHandler):
        logging.getLogger().removeHandler(h)
        h.close()

SPECS = {"XRPUSDT": {"step": 1.0, "min_qty": 1.0, "min_usdt": 5.0},
         "SUIUSDT": {"step": 0.1, "min_qty": 0.1, "min_usdt": 5.0},
         "LINKUSDT": {"step": 1.0, "min_qty": 1.0, "min_usdt": 5.0}}
PX = {"XRP": (1990.0, 1991.0, 1.466, 1.467), "SUI": (1549.0, 1550.0, 1.140, 1.141), "LINK": (18610.0, 18620.0, 13.70, 13.71)}
FX = 1340.0


class FakeBithumb:
    def __init__(self, buy_ratio=1.0, sell_ratio=1.0, buy_fail=False, sell_fail=False):
        self.buy_ratio, self.sell_ratio, self.buy_fail, self.sell_fail = buy_ratio, sell_ratio, buy_fail, sell_fail
        self.orders, self.coins = [], {}

    def accounts(self):
        return dict({"KRW": (10_000_000.0, 0.0)}, **{c: (q, 0.0) for c, q in self.coins.items()})

    def place_ioc(self, market, side, volume, price, client_id):
        if (side == "bid" and self.buy_fail) or (side == "ask" and self.sell_fail):
            raise ExchangeError(f"가짜 {side} 실패")
        qty = volume * (self.buy_ratio if side == "bid" else self.sell_ratio)
        coin = market.split("-")[1]
        self.coins[coin] = self.coins.get(coin, 0.0) + (qty if side == "bid" else -qty)
        self.orders.append({"id": f"B{len(self.orders)}", "side": side, "qty": qty, "price": price})
        return self.orders[-1]["id"]

    def wait_fill(self, oid, timeout):
        o = next(x for x in self.orders if x["id"] == oid)
        return {"qty": o["qty"], "avg": o["price"], "funds": o["qty"] * o["price"],
                "fee": o["qty"] * o["price"] * 0.0004, "state": "done", "id": oid}


class FakeBitget:
    def __init__(self, open_fail=False, close_fail=False, px=None):
        self.open_fail, self.close_fail = open_fail, close_fail
        self.pos, self.orders, self.px = {}, [], px or {}

    def open_short(self, sym, qty, cid):
        if self.open_fail:
            raise ExchangeError("가짜 숏 실패")
        self.pos[sym] = self.pos.get(sym, 0.0) + qty
        self.orders.append(("open", sym, qty))
        return f"G{len(self.orders)}"

    def close_short(self, sym, qty, cid):
        if self.close_fail:
            raise ExchangeError("가짜 청산 실패")
        self.pos[sym] = self.pos.get(sym, 0.0) - qty
        self.orders.append(("close", sym, qty))
        return f"G{len(self.orders)}"

    def wait_fill(self, sym, oid, want, timeout):
        px = self.px.get(sym, 1.0)
        return {"qty": want, "avg": px, "fee": want * px * 0.0006, "id": oid}

    def short_size(self, sym):
        return self.pos.get(sym, 0.0)

    def usdt_available(self):
        return 1_000.0


@pytest.fixture(autouse=True)
def tmp_files(tmp_path, monkeypatch):
    monkeypatch.setattr(LS, "STATE_FILE", str(tmp_path / "state{}.json"))
    monkeypatch.setattr(LS, "TRADE_LOG", str(tmp_path / "trades{}.csv"))
    monkeypatch.setattr(LS, "STOP_FILE", str(tmp_path / "STOP"))
    monkeypatch.setattr(lt.time, "sleep", lambda s: None)
    return tmp_path


def make(live=True, bh=None, bg=None):
    bh = bh or FakeBithumb()
    bg = bg or FakeBitget(px={"XRPUSDT": 1.466, "SUIUSDT": 1.140, "LINKUSDT": 13.70})
    ex = lt.Executor(live, bh, bg, SPECS, suffix="")
    ex.grids = {c: lt.LiveGrid(c, cfg, ex) for c, cfg in lt.live_cfgs().items()}
    ex.books = {}
    for c, (b, a, _, _) in PX.items():                     # 호가 단위: LINK 10원, 나머지 1원
        t = 10.0 if c == "LINK" else 1.0
        ex.books[f"KRW-{c}"] = {"units": [(b, 1000.0, a, 1000.0), (b - t, 1000.0, a + t, 1000.0)], "ts": 0}
    return ex


def quote(coin, up_bid=None, bg_ask=None):
    b, a, bb, ba = PX[coin]
    b, ba = up_bid or b, bg_ask or ba
    return {"up_bid": b, "up_ask": a, "bg_bid": bb, "bg_ask": ba, "funding": 0.0001, "fx": FX,
            "entry_pct": (a / (bb * FX) - 1) * 100, "exit_pct": (b / (ba * FX) - 1) * 100}


def free_slot(ex, coin):
    return next(s for s in ex.grids[coin].slots if not s.active)


def rows(tmp_path):
    p = tmp_path / "trades.csv"
    return list(__import__("csv").DictReader(open(p, encoding="utf-8"))) if p.exists() else []


def test_entry_qty_rounds_down_to_bitget_step():
    ex = make()
    assert ex.entry_qty(ex.grids["XRP"], quote("XRP")) == 28            # 57,000 / 1,991 = 28.6
    assert ex.entry_qty(ex.grids["SUI"], quote("SUI")) == pytest.approx(36.7)
    assert ex.entry_qty(ex.grids["LINK"], quote("LINK")) == 3             # 1개 단위
    assert floor_step(2.9999999999, 1.0) == 3 and floor_step(0.35, 0.1) == pytest.approx(0.3)


def test_enter_success_hedges_same_coin_qty_and_saves_state(tmp_path):
    ex = make()
    slot = free_slot(ex, "XRP")
    assert ex.enter(ex.grids["XRP"], slot, quote("XRP"), FX)
    assert slot.active and slot.coin_qty == slot.short_qty == 28
    assert ex.bg.pos["XRPUSDT"] == 28 and ex.bh.coins["XRP"] == 28
    assert slot.upbit_entry_px == ex._limit("KRW-XRP", "bid", LS.LIVE_MAX_SLIPPAGE)   # 가짜는 지정가에 체결
    st = json.load(open(tmp_path / "state.json", encoding="utf-8"))
    assert len(st["slots"]["XRP"]) == 1 and st["halted"] is None


def test_bitget_open_failure_unwinds_bithumb_buy(tmp_path):
    ex = make(bg=FakeBitget(open_fail=True))
    slot = free_slot(ex, "XRP")
    assert not ex.enter(ex.grids["XRP"], slot, quote("XRP"), FX)
    assert not slot.active and ex.halted is None
    assert [o["side"] for o in ex.bh.orders] == ["bid", "ask"] and ex.bh.coins["XRP"] == pytest.approx(0)
    assert rows(tmp_path)[-1]["reason"] == "진입 되돌림" and float(rows(tmp_path)[-1]["net_krw"]) < 0   # 되돌림 비용 기록


def test_unwind_failure_halts():
    ex = make(bh=FakeBithumb(sell_fail=True), bg=FakeBitget(open_fail=True))
    assert not ex.enter(ex.grids["XRP"], free_slot(ex, "XRP"), quote("XRP"), FX)
    assert ex.halted and "되돌림 실패" in ex.halted
    assert not ex.enter(ex.grids["SUI"], free_slot(ex, "SUI"), quote("SUI"), FX)    # 멈추면 신규 진입 없음


def test_partial_buy_hedges_floor_and_sells_extra(tmp_path):
    ex = make(bh=FakeBithumb(buy_ratio=0.95), bg=FakeBitget(px={"LINKUSDT": 13.70}))
    slot = free_slot(ex, "LINK")
    assert ex.enter(ex.grids["LINK"], slot, quote("LINK"), FX)           # 3 × 0.95 = 2.85 → 숏 2개
    assert slot.coin_qty == slot.short_qty == 2
    assert ex.bh.coins["LINK"] == pytest.approx(2.0)                     # 0.85 개(약 1.6만원)는 되팖
    assert rows(tmp_path)[-1]["reason"] == "자투리 매도" and ex.halted is None


def test_exit_records_actual_pnl_and_clears_slot(tmp_path):
    ex = make()
    g, slot = ex.grids["XRP"], free_slot(ex, "XRP")
    ex.enter(g, slot, quote("XRP"), FX)
    ex.bg.px["XRPUSDT"] = 1.460                                          # 숏 이익 (1.466 → 1.460)
    ex.books["KRW-XRP"]["units"][0] = (2000.0, 1000.0, 2001.0, 1000.0)   # 빗썸도 올라 매수 이익
    assert ex.exit(g, slot, quote("XRP", up_bid=2000.0, bg_ask=1.461), 1352.0, 1, "익절")
    r = rows(tmp_path)[-1]
    assert r["reason"] == "익절" and float(r["qty"]) == 28 and float(r["net_krw"]) > 0
    assert float(r["net_with_rebate_krw"]) > float(r["net_krw"])         # 수수료 환급 추정은 따로 더함
    assert not slot.active and ex.bg.pos["XRPUSDT"] == pytest.approx(0)


def test_exit_bitget_close_failure_halts():
    ex = make()
    g, slot = ex.grids["XRP"], free_slot(ex, "XRP")
    ex.enter(g, slot, quote("XRP"), FX)
    ex.bg.close_fail = True
    assert not ex.exit(g, slot, quote("XRP"), 1352.0, 1, "익절")
    assert ex.halted and "숏만 남음" in ex.halted


def test_state_survives_restart(tmp_path):
    ex = make()
    ex.enter(ex.grids["SUI"], free_slot(ex, "SUI"), quote("SUI"), FX)
    ex2 = make(bh=ex.bh, bg=ex.bg)
    assert ex2.load() == 1
    s = next(s for s in ex2.grids["SUI"].slots if s.active)
    assert s.coin_qty == pytest.approx(36.7) and s.live["orders"]
    assert ex2.reconcile() == []                                         # 거래소와 기록이 맞음


def test_reconcile_mismatch_halts():
    ex = make()
    ex.enter(ex.grids["XRP"], free_slot(ex, "XRP"), quote("XRP"), FX)
    ex.bg.pos["XRPUSDT"] = 0.0                                           # 누가 앱에서 숏을 닫았다면
    assert ex.reconcile() and ex.halted and "비트겟 숏" in ex.halted


def test_stop_file_blocks_new_entries(tmp_path):
    ex = make()
    (tmp_path / "STOP").write_text("")
    assert not ex.enter(ex.grids["XRP"], free_slot(ex, "XRP"), quote("XRP"), FX)
    assert ex.bh.orders == []


def test_open_cap_blocks_entries(monkeypatch):
    monkeypatch.setattr(LS, "LIVE_MAX_OPEN_KRW", 60_000)
    ex = make()
    assert ex.enter(ex.grids["XRP"], free_slot(ex, "XRP"), quote("XRP"), FX)        # 약 5.6만원
    assert not ex.enter(ex.grids["SUI"], free_slot(ex, "SUI"), quote("SUI"), FX)    # 상한 초과


def test_dry_run_walks_bithumb_book_without_orders():
    ex = make(live=False)
    ex.books["KRW-XRP"]["units"] = [(1990.0, 10.0, 1991.0, 10.0), (1989.0, 50.0, 1992.0, 50.0)]
    slot = free_slot(ex, "XRP")
    assert ex.enter(ex.grids["XRP"], slot, quote("XRP"), FX)
    assert ex.bh.orders == [] and ex.bg.orders == []                     # 주문 없음
    assert slot.upbit_entry_px == pytest.approx((10 * 1991 + 18 * 1992) / 28)   # 호가창 두 단계에 걸쳐 체결
