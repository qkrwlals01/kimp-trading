"""
어제 하루(KST 기준) BTC 김치 프리미엄 그래프
실행: python plot_kimp.py
"""

import sys, os, requests
from datetime import datetime, timezone, timedelta

# ── 시간 범위 (어제 KST 00:00 ~ 24:00 = UTC 전날 15:00 ~ 당일 15:00) ──
KST = timezone(timedelta(hours=9))
today_kst  = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
start_kst  = today_kst - timedelta(days=1)   # 어제 00:00 KST
end_kst    = today_kst                        # 오늘 00:00 KST (= 어제 24:00)

start_utc  = start_kst.astimezone(timezone.utc)
end_utc    = end_kst.astimezone(timezone.utc)

print(f"조회 구간: {start_kst.strftime('%Y-%m-%d %H:%M KST')} ~ {end_kst.strftime('%Y-%m-%d %H:%M KST')}")

# ── 업비트 1시간 캔들 ─────────────────────────────────────────────
def fetch_upbit_hourly():
    to_str = end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    url    = "https://api.upbit.com/v1/candles/minutes/60"
    params = {"market": "KRW-BTC", "to": to_str, "count": 24}
    resp   = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    result = {}
    for c in data:
        dt = datetime.strptime(c["candle_date_time_utc"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        if start_utc <= dt < end_utc:
            result[dt] = float(c["trade_price"])
    return result

# ── 비트겟 1시간 캔들 (USDT-FUTURES) ─────────────────────────────
def fetch_bitget_hourly():
    start_ms = int(start_utc.timestamp() * 1000)
    end_ms   = int(end_utc.timestamp() * 1000)
    url      = "https://api.bitget.com/api/v2/mix/market/candles"
    params   = {
        "symbol":      "BTCUSDT",
        "productType": "USDT-FUTURES",
        "granularity": "1H",
        "startTime":   str(start_ms),
        "endTime":     str(end_ms),
        "limit":       "24",
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "00000":
        raise RuntimeError(f"Bitget 오류: {data}")
    result = {}
    for c in data["data"]:
        dt = datetime.fromtimestamp(int(c[0]) / 1000, tz=timezone.utc)
        result[dt] = float(c[4])   # close price
    return result

# ── USD/KRW 환율 (현재 기준 사용) ────────────────────────────────
def fetch_usd_krw():
    """dunamu 우선, 백업 upbit crix. 국제 API는 ~13원 오차로 사용 금지."""
    for url in [
        "https://quotation-api-cdn.dunamu.com/v1/forex/recent?codes=FRX.KRWUSD",
        "https://crix-api-endpoint.upbit.com/v1/forex/recent?codes=FRX.KRWUSD",
    ]:
        try:
            resp = requests.get(url, timeout=5)
            data = resp.json()
            if isinstance(data, list) and "basePrice" in data[0]:
                return float(data[0]["basePrice"])
        except Exception:
            continue
    raise RuntimeError("환율 API 실패 (dunamu/upbit crix 모두 응답 없음)")

# ── 데이터 수집 ───────────────────────────────────────────────────
print("업비트 데이터 조회 중...")
upbit_data  = fetch_upbit_hourly()
print(f"  → {len(upbit_data)}개 캔들 수신")

print("비트겟 데이터 조회 중...")
bitget_data = fetch_bitget_hourly()
print(f"  → {len(bitget_data)}개 캔들 수신")

print("환율 조회 중...")
usd_krw = fetch_usd_krw()
print(f"  → USD/KRW = {usd_krw:,.0f}")

# ── 공통 시각 기준으로 김프 계산 ─────────────────────────────────
times, premiums, upbit_prices, bitget_krw_prices = [], [], [], []

for dt in sorted(upbit_data.keys()):
    if dt not in bitget_data:
        continue
    up_px      = upbit_data[dt]
    bg_px      = bitget_data[dt]
    bg_krw     = bg_px * usd_krw
    premium    = (up_px - bg_krw) / bg_krw * 100
    times.append(dt.astimezone(KST))
    premiums.append(premium)
    upbit_prices.append(up_px)
    bitget_krw_prices.append(bg_krw)

if not times:
    print("❌ 데이터 매칭 실패 — 두 거래소 공통 시각이 없습니다.")
    sys.exit(1)

print(f"\n총 {len(times)}개 데이터 포인트")
print(f"김프 범위: {min(premiums):.2f}% ~ {max(premiums):.2f}%")
print(f"평균 김프: {sum(premiums)/len(premiums):.2f}%")

# ── 그래프 ────────────────────────────────────────────────────────
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib import rcParams

rcParams["font.family"] = ["Malgun Gothic", "AppleGothic", "DejaVu Sans"]
rcParams["axes.unicode_minus"] = False

fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 8), sharex=True,
                                gridspec_kw={"height_ratios": [2, 1]})
fig.suptitle(
    f"BTC 김치 프리미엄  |  {start_kst.strftime('%Y년 %m월 %d일')} (KST)\n"
    f"USD/KRW {usd_krw:,.0f}  |  평균 김프 {sum(premiums)/len(premiums):.2f}%",
    fontsize=13, fontweight="bold", y=0.98
)

# — 상단: 김프 라인 —
colors = ["#e74c3c" if p >= 0 else "#3498db" for p in premiums]
ax1.axhline(0, color="#555", linewidth=0.8, linestyle="--")
ax1.plot(times, premiums, color="#e67e22", linewidth=2, zorder=3)
ax1.fill_between(times, premiums, 0,
                 where=[p >= 0 for p in premiums], alpha=0.25, color="#e74c3c", label="양의 김프")
ax1.fill_between(times, premiums, 0,
                 where=[p < 0  for p in premiums], alpha=0.25, color="#3498db", label="음의 김프")

# 최고/최저 표시
max_idx = premiums.index(max(premiums))
min_idx = premiums.index(min(premiums))
ax1.annotate(f"{premiums[max_idx]:.2f}%",
             xy=(times[max_idx], premiums[max_idx]),
             xytext=(0, 10), textcoords="offset points",
             ha="center", fontsize=9, color="#e74c3c",
             arrowprops=dict(arrowstyle="->", color="#e74c3c", lw=1))
ax1.annotate(f"{premiums[min_idx]:.2f}%",
             xy=(times[min_idx], premiums[min_idx]),
             xytext=(0, -15), textcoords="offset points",
             ha="center", fontsize=9, color="#3498db",
             arrowprops=dict(arrowstyle="->", color="#3498db", lw=1))

ax1.set_ylabel("김치 프리미엄 (%)", fontsize=11)
ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:.1f}%"))
ax1.legend(fontsize=9, loc="upper right")
ax1.grid(axis="y", alpha=0.3)
ax1.set_facecolor("#fafafa")

# — 하단: 업비트 vs 비트겟 가격 —
ax2.plot(times, [p/1_000_000 for p in upbit_prices],
         label="업비트 (KRW)", color="#e74c3c", linewidth=1.8)
ax2.plot(times, [p/1_000_000 for p in bitget_krw_prices],
         label=f"비트겟×환율 (KRW)", color="#3498db", linewidth=1.8, linestyle="--")
ax2.set_ylabel("가격 (백만원)", fontsize=11)
ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:.1f}M"))
ax2.legend(fontsize=9, loc="upper right")
ax2.grid(axis="y", alpha=0.3)
ax2.set_facecolor("#fafafa")

# — x축 —
ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=KST))
ax2.xaxis.set_major_locator(mdates.HourLocator(interval=2, tz=KST))
plt.xticks(rotation=30, ha="right", fontsize=9)
ax2.set_xlabel(f"{start_kst.strftime('%Y-%m-%d')} KST", fontsize=10)

plt.tight_layout()
out_path = os.path.join(os.path.dirname(__file__), "kimp_yesterday.png")
plt.savefig(out_path, dpi=150, bbox_inches="tight")
print(f"\n✅ 저장 완료: {out_path}")
