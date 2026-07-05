"""
모의거래 웹 대시보드
실행: python -m paper_trading.dashboard
접속: http://localhost:5000  (SSH 터널: ssh -L 5000:localhost:5000 <USER>@서버IP -N)
"""

import os, csv, json
from datetime import datetime, timezone
from flask import Flask, render_template_string

LOG_DIR   = os.path.join(os.path.dirname(__file__), "logs")
TRADE_LOG = os.path.join(LOG_DIR, "trades.csv")

app = Flask(__name__)

TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="30">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kimp Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0d1117;color:#e6edf3;font-family:'Segoe UI',sans-serif;font-size:14px;padding:20px}
  h1{font-size:18px;font-weight:600;margin-bottom:4px}
  .sub{color:#8b949e;font-size:12px;margin-bottom:20px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px}
  .card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px}
  .card .label{color:#8b949e;font-size:11px;margin-bottom:6px}
  .card .val{font-size:22px;font-weight:700}
  .card .val.pos{color:#3fb950}
  .card .val.neg{color:#f85149}
  .card .val.neu{color:#58a6ff}
  .section{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;margin-bottom:16px}
  .section h2{font-size:13px;font-weight:600;color:#8b949e;margin-bottom:12px;text-transform:uppercase;letter-spacing:.5px}
  table{width:100%;border-collapse:collapse}
  th{color:#8b949e;font-size:11px;font-weight:500;text-align:left;padding:6px 8px;border-bottom:1px solid #30363d}
  td{padding:6px 8px;border-bottom:1px solid #21262d;font-size:13px}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:#1c2128}
  .pos{color:#3fb950}
  .neg{color:#f85149}
  .charts{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
  @media(max-width:700px){.charts{grid-template-columns:1fr}}
  canvas{max-height:220px}
  .refresh{color:#8b949e;font-size:11px;text-align:right;margin-top:12px}
  .tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
  .tag-pos{background:#1a3a1a;color:#3fb950}
  .tag-neg{background:#3a1a1a;color:#f85149}
</style>
</head>
<body>

<h1>📈 Kimp Trading Dashboard</h1>
<div class="sub">모의거래 · 10초 폴링 · 페이지 30초 자동 갱신 · {{ now }}</div>

<!-- 요약 카드 -->
<div class="grid">
  <div class="card">
    <div class="label">누적 순수익</div>
    <div class="val {{ 'pos' if summary.net_total >= 0 else 'neg' }}">
      {{ '{:+,.0f}'.format(summary.net_total) }}원
    </div>
  </div>
  <div class="card">
    <div class="label">총 익절 건수</div>
    <div class="val neu">{{ summary.total_trades }}건</div>
  </div>
  <div class="card">
    <div class="label">승률</div>
    <div class="val {{ 'pos' if summary.win_rate >= 50 else 'neg' }}">
      {{ '%.1f'|format(summary.win_rate) }}%
    </div>
  </div>
  <div class="card">
    <div class="label">평균 보유 시간</div>
    <div class="val neu">{{ '%.1f'|format(summary.avg_hold) }}h</div>
  </div>
  <div class="card">
    <div class="label">오늘 수익</div>
    <div class="val {{ 'pos' if summary.today_net >= 0 else 'neg' }}">
      {{ '{:+,.0f}'.format(summary.today_net) }}원
    </div>
  </div>
  <div class="card">
    <div class="label">오늘 거래</div>
    <div class="val neu">{{ summary.today_trades }}건</div>
  </div>
</div>

<!-- 차트 -->
<div class="charts">
  <div class="section">
    <h2>누적 순수익 추이</h2>
    <canvas id="pnlChart"></canvas>
  </div>
  <div class="section">
    <h2>코인별 순수익</h2>
    <canvas id="coinChart"></canvas>
  </div>
</div>

<!-- 코인별 통계 -->
<div class="section">
  <h2>코인별 성과</h2>
  <table>
    <tr>
      <th>코인</th><th>건수</th><th>순수익</th><th>Gross</th><th>수수료</th><th>건당 순수익</th><th>평균보유</th>
    </tr>
    {% for r in coin_stats %}
    <tr>
      <td><b>{{ r.coin }}</b></td>
      <td>{{ r.count }}</td>
      <td class="{{ 'pos' if r.net >= 0 else 'neg' }}">{{ '{:+,.0f}'.format(r.net) }}원</td>
      <td class="pos">{{ '{:+,.0f}'.format(r.gross) }}원</td>
      <td style="color:#8b949e">{{ '{:,.0f}'.format(r.fee) }}원</td>
      <td class="{{ 'pos' if r.net_per >= 0 else 'neg' }}">{{ '{:+,.0f}'.format(r.net_per) }}원</td>
      <td style="color:#8b949e">{{ '%.2f'|format(r.avg_hold) }}h</td>
    </tr>
    {% endfor %}
  </table>
</div>

<!-- 최근 거래 -->
<div class="section">
  <h2>최근 거래 20건</h2>
  <table>
    <tr>
      <th>#</th><th>코인</th><th>진입김프</th><th>청산김프</th><th>보유</th><th>순수익</th><th>청산시각</th>
    </tr>
    {% for r in recent %}
    <tr>
      <td style="color:#8b949e">{{ r.trade_id }}</td>
      <td><b>{{ r.coin }}</b></td>
      <td style="color:#8b949e">{{ r.entry_premium }}%</td>
      <td style="color:#8b949e">{{ r.exit_premium }}%</td>
      <td style="color:#8b949e">{{ r.hold_hours }}h</td>
      <td class="{{ 'pos' if r.net_pnl >= 0 else 'neg' }}">{{ '{:+,.0f}'.format(r.net_pnl) }}원</td>
      <td style="color:#8b949e;font-size:11px">{{ r.exit_dt }}</td>
    </tr>
    {% endfor %}
  </table>
</div>

<div class="refresh">⟳ 30초마다 자동 갱신</div>

<script>
const pnlData = {{ pnl_chart | tojson }};
const coinData = {{ coin_chart | tojson }};

// 누적 수익 차트
new Chart(document.getElementById('pnlChart'), {
  type: 'line',
  data: {
    labels: pnlData.labels,
    datasets: [{
      data: pnlData.values,
      borderColor: '#3fb950',
      backgroundColor: 'rgba(63,185,80,0.08)',
      borderWidth: 2,
      pointRadius: 0,
      fill: true,
      tension: 0.3,
    }]
  },
  options: {
    plugins: { legend: { display: false } },
    scales: {
      x: { ticks: { color: '#8b949e', maxTicksLimit: 6 }, grid: { color: '#21262d' } },
      y: { ticks: { color: '#8b949e', callback: v => (v/10000).toFixed(1)+'만' }, grid: { color: '#21262d' } }
    }
  }
});

// 코인별 수익 차트
const colors = coinData.values.map(v => v >= 0 ? '#3fb950' : '#f85149');
new Chart(document.getElementById('coinChart'), {
  type: 'bar',
  data: {
    labels: coinData.labels,
    datasets: [{
      data: coinData.values,
      backgroundColor: colors,
      borderRadius: 4,
    }]
  },
  options: {
    plugins: { legend: { display: false } },
    scales: {
      x: { ticks: { color: '#8b949e' }, grid: { color: '#21262d' } },
      y: { ticks: { color: '#8b949e', callback: v => (v/10000).toFixed(1)+'만' }, grid: { color: '#21262d' } }
    }
  }
});
</script>
</body>
</html>
"""


def load_trades():
    if not os.path.exists(TRADE_LOG):
        return []
    with open(TRADE_LOG, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build_context(rows):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not rows:
        empty = {"net_total":0,"total_trades":0,"win_rate":0,"avg_hold":0,"today_net":0,"today_trades":0}
        return dict(now=now, summary=type("S",(),empty)(), coin_stats=[], recent=[],
                    pnl_chart={"labels":[],"values":[]}, coin_chart={"labels":[],"values":[]})

    today_str = datetime.now().strftime("%Y-%m-%d")
    nets, holds, wins = [], [], []
    coin_map = {}
    cumulative, pnl_labels = [], []
    running = 0

    for r in rows:
        try:
            net  = float(r["net_pnl"])
            gross= float(r.get("gross_pnl", 0))
            fee  = float(r.get("fee_krw", 0))
            hold = float(r.get("hold_hours", 0))
            coin = r["coin"]
            exit_dt_raw = r.get("exit_dt","")

            nets.append(net); holds.append(hold)
            if net > 0: wins.append(1)

            running += net
            label = exit_dt_raw[11:16] if len(exit_dt_raw) >= 16 else ""
            cumulative.append(running)
            pnl_labels.append(label)

            if coin not in coin_map:
                coin_map[coin] = {"count":0,"net":0,"gross":0,"fee":0,"hold":0}
            coin_map[coin]["count"] += 1
            coin_map[coin]["net"]   += net
            coin_map[coin]["gross"] += gross
            coin_map[coin]["fee"]   += fee
            coin_map[coin]["hold"]  += hold
        except:
            pass

    # 오늘 거래
    today_rows = [r for r in rows if r.get("exit_dt","").startswith(today_str)]
    today_net  = sum(float(r["net_pnl"]) for r in today_rows if r.get("net_pnl"))
    today_cnt  = len(today_rows)

    # 요약
    total = len(nets)
    class Summary: pass
    Summary.net_total    = running
    Summary.total_trades = total
    Summary.win_rate     = len(wins)/total*100 if total else 0
    Summary.avg_hold     = sum(holds)/total if total else 0
    Summary.today_net    = today_net
    Summary.today_trades = today_cnt

    # 코인별
    coin_stats = []
    for c, v in sorted(coin_map.items(), key=lambda x: -x[1]["net"]):
        class Row: pass
        r = Row()
        r.coin=c; r.count=v["count"]; r.net=v["net"]; r.gross=v["gross"]
        r.fee=v["fee"]; r.net_per=v["net"]/v["count"]; r.avg_hold=v["hold"]/v["count"]
        coin_stats.append(r)

    # 최근 20건
    recent = []
    for r in reversed(rows[-20:]):
        try:
            class T: pass
            t = T()
            t.trade_id    = r["trade_id"]
            t.coin        = r["coin"]
            t.entry_premium = r.get("entry_premium","")
            t.exit_premium  = r.get("exit_premium","")
            t.hold_hours  = r.get("hold_hours","")
            t.net_pnl     = float(r["net_pnl"])
            raw = r.get("exit_dt","")
            t.exit_dt = raw[5:16].replace("T"," ") if len(raw)>=16 else raw
            recent.append(t)
        except:
            pass

    # 차트 데이터 (최대 200포인트 샘플링)
    step = max(1, len(cumulative)//200)
    pnl_chart = {
        "labels": pnl_labels[::step],
        "values": cumulative[::step],
    }
    coin_chart = {
        "labels": [c["coin"] if isinstance(c,dict) else c.coin for c in coin_stats],
        "values": [c["net"]  if isinstance(c,dict) else c.net  for c in coin_stats],
    }

    return dict(now=now, summary=Summary(), coin_stats=coin_stats, recent=recent,
                pnl_chart=pnl_chart, coin_chart=coin_chart)


@app.route("/")
def index():
    rows = load_trades()
    ctx  = build_context(rows)
    return render_template_string(TEMPLATE, **ctx)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
