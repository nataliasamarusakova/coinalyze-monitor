"""make_dashboard.py — генерирует docs/index.html (дашборд сделок) из trades.jsonl
+ живые открытые позиции из watchlist.json + упущенные движения из market_history.jsonl.

Данные инлайнятся → самодостаточный HTML (GitHub Pages + локально без сервера).
Закрытые сделки = trades.jsonl. Открытые = watchlist.json (блок LIVE).
Упущенные = market_history.jsonl (монеты с ростом > порога без нашей сделки).

ФИКСЫ:
  - F6: load_trades() считает и печатает битые строки.
  - F7: assert заменён на raise RuntimeError (не отключается -O).
  - Статистика по дням (открыто/закрыто/плюс/минус/win%/PnL).
  - Упущенные движения за 24ч.
  - Порядок: Статистика по дням поставлена перед Win-rate графиками.

Запуск:  python make_dashboard.py
"""
import json
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
TRADES = BASE / "trades.jsonl"
WATCHLIST = BASE / "watchlist.json"
MARKET_HISTORY = BASE / "market_history.jsonl"
OUT = BASE / "docs" / "index.html"

MISSED_THRESHOLD = 5.0   # порог "значимого роста" (% за 24ч) для упущенных

try:
    from monitor import TRADE_TIMEOUT_MIN
except Exception as e:
    TRADE_TIMEOUT_MIN = 240
    print(f"WARNING: не удалось импортировать TRADE_TIMEOUT_MIN из monitor.py "
          f"({e}); используется fallback=240")

FIELDS = [
    "symbol", "name", "asset_class", "entry_ts", "entry_price", "exit_price",
    "strategy_pnl_pct", "gross_pnl_pct", "return_60m", "return_120m", "return_240m",
    "hold_min", "exit_reason", "exit_state", "closed_before_60m", "entry_path",
    "entry_pattern", "entry_momentum", "entry_cvd_momentum", "entry_earliness_label",
    "entry_divergence", "entry_market_phase", "max_pnl_pct", "min_pnl_pct",
    "drawdown_from_peak_pct", "signal_age_min", "entry_price_chg24",
    "signal_logic_version", "pending_finalize_reason",
    "exit_price_source", "exit_price_stale_min",
]


def load_trades():
    """[FIX F6] Считает битые строки и печатает warning."""
    if not TRADES.exists():
        return [], 0
    out, bad = [], 0
    for ln in TRADES.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            bad += 1
            continue
        out.append({k: r.get(k) for k in FIELDS})
    if bad:
        print(f"WARNING: trades.jsonl — {bad} битых строк пропущено")
    return out, bad


def load_open():
    if not WATCHLIST.exists():
        return []
    try:
        data = json.loads(WATCHLIST.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"WARNING: watchlist.json повреждён ({e}) — live-позиции недоступны")
        return []
    out = []
    for sym, rec in data.items():
        ot = rec.get("open_trade")
        if not ot:
            continue
        ep = ot.get("entry_price")
        lp = ot.get("last_price")
        cur_pnl = round((lp - ep) / ep * 100, 2) if (ep and lp) else None
        ets = ot.get("entry_ts", 0)
        lts = ot.get("last_price_ts", ets)
        hold = round((lts - ets) / 60, 1) if lts > ets else 0.0
        timeout_pct = min(hold / TRADE_TIMEOUT_MIN * 100, 100) if TRADE_TIMEOUT_MIN else 0
        out.append({
            "symbol": sym,
            "name": ot.get("name", sym),
            "asset_class": ot.get("asset_class", "crypto"),
            "state": rec.get("state"),
            "entry_ts": ets,
            "last_price_ts": lts,
            "entry_price": ep,
            "last_price": lp,
            "cur_pnl_pct": cur_pnl,
            "hold_min": hold,
            "timeout_pct": round(timeout_pct, 1),
            "max_pnl_pct": ot.get("max_pnl_pct", 0.0),
            "min_pnl_pct": ot.get("min_pnl_pct", 0.0),
            "entry_path": ot.get("entry_path"),
            "entry_pattern": ot.get("entry_pattern"),
            "entry_momentum": ot.get("entry_momentum"),
            "entry_cvd_momentum": ot.get("entry_cvd_momentum"),
            "entry_earliness_label": ot.get("entry_earliness_label"),
            "entry_divergence": ot.get("entry_divergence"),
            "signal_logic_version": ot.get("signal_logic_version"),
        })
    out.sort(key=lambda x: (x["cur_pnl_pct"] if x["cur_pnl_pct"] is not None else -999),
             reverse=True)
    return out


def load_market_history():
    """Последний снимок каждой монеты из market_history.jsonl (глубина ~24ч)."""
    if not MARKET_HISTORY.exists():
        return []
    latest = {}
    for ln in MARKET_HISTORY.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        sym = r.get("symbol")
        ts = r.get("ts", 0)
        if not sym:
            continue
        if sym not in latest or ts > latest[sym]["ts"]:
            latest[sym] = {
                "symbol": sym,
                "name": r.get("name", sym),
                "price": r.get("price"),
                "price_chg24": r.get("price_chg24"),
                "ts": ts,
            }
    return list(latest.values())


def compute_missed(market_data, trades, open_positions, threshold_pct=MISSED_THRESHOLD):
    """Упущенные: монеты с ростом > порога, по которым нет сделки (открытой или закрытой за 24ч)."""
    now = time.time()
    cutoff_24h = now - 24 * 3600
    active_symbols = set()
    for op in open_positions:
        active_symbols.add(op["symbol"])
    for t in trades:
        if t.get("entry_ts") and t["entry_ts"] >= cutoff_24h:
            active_symbols.add(t["symbol"])

    missed = []
    for m in market_data:
        chg = m.get("price_chg24")
        if chg is not None and chg > threshold_pct and m["symbol"] not in active_symbols:
            missed.append(m)
    missed.sort(key=lambda x: x.get("price_chg24") or 0, reverse=True)
    return missed


HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trade Journal — Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root{
    --bg:#14171f; --bg-deep:#0e1118; --panel:#1d2230; --panel-2:#232a3b;
    --line:#2c3447; --txt:#eef2f8; --mut:#93a0b8; --mut-2:#6b7790;
    --grn:#34d399; --red:#fb7185; --amb:#fbbf24; --teal:#2dd4bf;
    --display:'Space Grotesk',system-ui,sans-serif;
    --body:'IBM Plex Sans',system-ui,sans-serif;
    --mono:'IBM Plex Mono',ui-monospace,monospace;
  }
  *{box-sizing:border-box;}
  html{scroll-behavior:smooth;}
  body{
    margin:0;color:var(--txt);font:15px/1.5 var(--body);
    background:
      radial-gradient(1200px 600px at 80% -10%, rgba(45,212,191,.07), transparent 60%),
      radial-gradient(900px 500px at 0% 100%, rgba(251,113,133,.06), transparent 55%),
      radial-gradient(circle at 50% 40%, var(--bg) 0%, var(--bg-deep) 100%);
    background-attachment:fixed;
  }
  body::before{
    content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
    background-image:radial-gradient(rgba(147,160,184,.06) 1px, transparent 1px);
    background-size:26px 26px;
    mask-image:radial-gradient(circle at 50% 30%, #000 30%, transparent 85%);
  }
  .wrap{position:relative;z-index:1;max-width:1240px;margin:0 auto;padding:28px 22px 80px;}
  .mast{display:flex;align-items:flex-end;justify-content:space-between;
    gap:20px;flex-wrap:wrap;margin-bottom:26px;padding-bottom:18px;
    border-bottom:1px solid var(--line);}
  .mast h1{font-family:var(--display);font-weight:700;font-size:clamp(26px,4vw,40px);
    margin:0;letter-spacing:-.02em;line-height:1;}
  .mast h1 .tick{color:var(--teal);}
  .mast .sub{color:var(--mut);font-size:13px;margin-top:8px;letter-spacing:.01em;}
  .live{display:flex;align-items:center;gap:8px;font-family:var(--mono);
    font-size:12px;color:var(--mut);background:var(--panel);border:1px solid var(--line);
    padding:8px 14px;border-radius:999px;}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--grn);
    box-shadow:0 0 0 0 rgba(52,211,153,.6);animation:pulse 2s infinite;}
  .dot.idle{background:var(--mut-2);animation:none;box-shadow:none;}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(52,211,153,.5);}
    70%{box-shadow:0 0 0 9px rgba(52,211,153,0);}100%{box-shadow:0 0 0 0 rgba(52,211,153,0);}}
  .controls{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:22px;}
  .ctl{display:flex;flex-direction:column;gap:5px;}
  .ctl label{font-size:11px;text-transform:uppercase;letter-spacing:.12em;color:var(--mut-2);}
  select{appearance:none;background:var(--panel) url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' fill='%2393a0b8'><path d='M2 4l4 4 4-4'/></svg>") no-repeat right 12px center;
    color:var(--txt);border:1px solid var(--line);border-radius:10px;
    padding:9px 34px 9px 13px;font:500 13px var(--body);cursor:pointer;
    transition:border-color .2s,transform .15s;}
  select:hover{border-color:var(--teal);}
  select:focus{outline:none;border-color:var(--teal);transform:translateY(-1px);}

  /* ── DAILY STATS ── */
  .daygrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));
    gap:12px;margin-bottom:20px;}
  .daycard{background:var(--panel);border:1px solid var(--line);border-radius:14px;
    padding:14px 16px;transition:transform .2s,border-color .2s;animation:pop .4s both;}
  .daycard:hover{transform:translateY(-2px);border-color:var(--teal);}
  .daycard.today{border-color:var(--teal);box-shadow:0 0 12px -4px rgba(45,212,191,.3);}
  .daycard .dlabel{font-size:11px;text-transform:uppercase;letter-spacing:.1em;
    color:var(--mut-2);margin-bottom:6px;}
  .daycard .dcount{font-family:var(--display);font-weight:700;font-size:28px;line-height:1;}
  .daycard .dbreak{display:flex;gap:10px;margin-top:8px;font-family:var(--mono);font-size:11.5px;}
  .daycard .dbreak .pos{color:var(--grn);}
  .daycard .dbreak .neg{color:var(--red);}
  .daycard .dbreak .neu{color:var(--mut);}
  .daycard .dmeta{margin-top:6px;font-family:var(--mono);font-size:11px;color:var(--mut);}

  .dailytbl{width:100%;border-collapse:collapse;font:12px/1.3 var(--mono);margin-top:12px;}
  .dailytbl th,.dailytbl td{padding:6px 8px;text-align:right;border-bottom:1px solid var(--line);}
  .dailytbl th{color:var(--mut-2);text-transform:uppercase;font-size:10px;letter-spacing:.06em;
    font-family:var(--body);font-weight:600;}
  .dailytbl td.l,.dailytbl th.l{text-align:left;}
  .dailytbl tr:hover td{background:var(--panel-2);}

  /* ── LIVE POSITIONS ── */
  .livehead{display:flex;align-items:center;gap:11px;margin:6px 0 14px;}
  .livehead h2{font-family:var(--display);font-weight:700;font-size:15px;margin:0;
    letter-spacing:.18em;text-transform:uppercase;color:var(--txt);}
  .livehead .pdot{width:9px;height:9px;border-radius:50%;background:var(--grn);
    animation:pulse 1.6s infinite;}
  .livehead .pdot.idle{background:var(--mut-2);animation:none;}
  .livehead .cnt{font-family:var(--mono);font-size:12px;color:var(--mut);
    background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:2px 10px;}
  .livegrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));
    gap:14px;margin-bottom:30px;}
  .pos-card{position:relative;background:linear-gradient(155deg,var(--panel-2),var(--panel));
    border:1px solid var(--line);border-radius:16px;padding:16px 18px 16px 22px;
    overflow:hidden;transition:transform .22s cubic-bezier(.2,.7,.3,1),border-color .22s,box-shadow .22s;
    animation:pop .5s both;}
  .pos-card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--teal);}
  .pos-card.eq::before{background:var(--amb);}
  .pos-card:hover{transform:translateY(-3px);border-color:var(--teal);
    box-shadow:0 16px 34px -18px rgba(45,212,191,.45);}
  .pos-card .row1{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;}
  .pos-card .sym{font-family:var(--display);font-weight:700;font-size:20px;letter-spacing:-.01em;line-height:1;}
  .pos-card .nm{color:var(--mut);font-size:11.5px;margin-top:3px;}
  .pos-card .pnl{font-family:var(--display);font-weight:700;font-size:26px;line-height:1;
    font-variant-numeric:tabular-nums;letter-spacing:-.02em;text-align:right;}
  .pos-card .stg{display:inline-flex;align-items:center;gap:5px;margin-top:7px;
    font-family:var(--mono);font-size:11px;color:var(--mut);}
  .pos-card .stg .se{font-size:13px;}
  .pos-card .bars{margin:13px 0 4px;}
  .pos-card .barlab{display:flex;justify-content:space-between;font-family:var(--mono);
    font-size:10.5px;color:var(--mut-2);margin-bottom:4px;}
  .pos-card .track{height:5px;border-radius:4px;background:var(--bg-deep);overflow:hidden;}
  .pos-card .fill{height:100%;border-radius:4px;background:linear-gradient(90deg,var(--teal),#22a89a);
    width:0;transition:width 1s cubic-bezier(.2,.7,.3,1);}
  .pos-card .fill.warn{background:linear-gradient(90deg,var(--amb),#d99a16);}
  .pos-card .metrics{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:12px;
    font-family:var(--mono);font-size:11px;color:var(--mut);}
  .pos-card .metrics b{color:var(--txt);font-weight:600;}
  .pos-card .tags{display:flex;flex-wrap:wrap;gap:5px;margin-top:11px;}
  .emptybox{color:var(--mut-2);font-style:italic;padding:22px 4px;font-size:13.5px;}

  .bento{display:grid;grid-template-columns:repeat(4,1fr);
    grid-auto-rows:minmax(96px,auto);gap:14px;margin-bottom:26px;}
  .kpi{position:relative;background:linear-gradient(160deg,var(--panel-2),var(--panel));
    border:1px solid var(--line);border-radius:16px;padding:16px 18px;overflow:hidden;
    transition:transform .22s cubic-bezier(.2,.7,.3,1),border-color .22s,box-shadow .22s;
    animation:pop .5s both;}
  .kpi:hover{transform:translateY(-3px);border-color:var(--teal);
    box-shadow:0 14px 30px -16px rgba(45,212,191,.4);}
  .kpi::after{content:"";position:absolute;inset:0;border-radius:16px;
    background:radial-gradient(120px 80px at 100% 0%, rgba(255,255,255,.05), transparent 70%);
    pointer-events:none;}
  .kpi.big{grid-column:span 2;grid-row:span 2;display:flex;flex-direction:column;justify-content:center;}
  .kpi .l{font-size:11px;text-transform:uppercase;letter-spacing:.13em;color:var(--mut-2);}
  .kpi .v{font-family:var(--display);font-weight:700;line-height:1;margin-top:8px;
    font-variant-numeric:tabular-nums;letter-spacing:-.02em;}
  .kpi .v.s{font-size:30px;}
  .kpi.big .v{font-size:clamp(44px,7vw,72px);}
  .kpi .m{font-family:var(--mono);font-size:12px;color:var(--mut);margin-top:8px;}
  .pos2{color:var(--grn);} .neg{color:var(--red);} .neu{color:var(--amb);}
  @keyframes pop{from{opacity:0;transform:translateY(10px) scale(.98);}to{opacity:1;transform:none;}}

  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:18px;}
  @media(max-width:860px){.grid2{grid-template-columns:1fr;}.bento{grid-template-columns:repeat(2,1fr);}.kpi.big{grid-column:span 2;}}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px 20px;}
  .panel h3{margin:0 0 14px;font-family:var(--display);font-weight:600;font-size:14px;
    color:var(--txt);display:flex;align-items:center;gap:9px;}
  .panel h3::before{content:"";width:4px;height:15px;border-radius:3px;background:var(--teal);}
  .reveal{opacity:0;transform:translateY(22px);transition:opacity .6s ease,transform .6s cubic-bezier(.2,.7,.3,1);}
  .reveal.in{opacity:1;transform:none;}

  table.main{width:100%;border-collapse:collapse;font:12.5px/1.3 var(--mono);}
  table.main th,table.main td{padding:7px 9px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap;}
  table.main th{color:var(--mut-2);cursor:pointer;user-select:none;position:sticky;top:0;
    background:var(--panel);text-transform:uppercase;letter-spacing:.06em;font-size:10.5px;
    font-family:var(--body);font-weight:600;transition:color .15s;}
  table.main th:hover{color:var(--teal);}
  table.main td.l,table.main th.l{text-align:left;}
  table.main tbody tr{transition:background .15s;}
  table.main tbody tr:hover td{background:var(--panel-2);}
  .tblwrap{max-height:540px;overflow:auto;border-radius:14px;border:1px solid var(--line);}
  .tblwrap::-webkit-scrollbar{width:9px;height:9px;}
  .tblwrap::-webkit-scrollbar-thumb{background:var(--line);border-radius:9px;}
  .tag{display:inline-block;padding:1px 7px;border-radius:6px;background:var(--panel-2);
    border:1px solid var(--line);font:500 10.5px var(--body);color:var(--mut);}
  .tag.eq{color:var(--amb);border-color:rgba(251,191,36,.3);}
  .tag.co{color:var(--teal);border-color:rgba(45,212,191,.3);}
  .toprow{display:flex;justify-content:space-between;align-items:center;
    padding:7px 2px;border-bottom:1px solid var(--line);font-size:12.5px;gap:10px;
    transition:background .15s;}
  .toprow:hover{background:var(--panel-2);}
  .toprow .nm{font-family:var(--body);font-weight:600;}
  .toprow .meta{font-family:var(--mono);color:var(--mut);font-size:11.5px;text-align:right;}
  .empty{color:var(--mut-2);padding:34px;text-align:center;font-style:italic;}
  .foot{margin-top:34px;color:var(--mut-2);font-size:12px;text-align:center;
    font-family:var(--mono);}
  .sec-title{font-family:var(--display);font-weight:700;font-size:15px;margin:30px 0 14px;
    letter-spacing:.14em;text-transform:uppercase;color:var(--txt);display:flex;align-items:center;gap:10px;}
  .sec-title::before{content:"";width:4px;height:16px;border-radius:3px;background:var(--teal);}
</style>
</head>
<body>
<div class="wrap">
  <div class="mast">
    <div>
      <h1>Trade <span class="tick">Journal</span></h1>
      <div class="sub">исследовательский журнал сигналов · live-позиции + signal vs strategy outcome · schema v2</div>
    </div>
    <div class="live"><span class="dot idle" id="mastDot"></span><span id="liveTxt">—</span></div>
  </div>

  <div class="controls">
    <div class="ctl"><label>Класс актива</label>
      <select id="asset">
        <option value="crypto" selected>crypto</option>
        <option value="all">все (вкл. equity/commodity)</option>
        <option value="equity">equity</option>
        <option value="commodity">commodity</option>
      </select></div>
    <div class="ctl"><label>Жизнь сделки (архив)</label>
      <select id="life">
        <option value="all" selected>все</option>
        <option value="long">дожили ≥60м</option>
        <option value="short">умерли &lt;60м</option>
      </select></div>
  </div>

  <!-- ═══ LIVE POSITIONS ═══ -->
  <div class="livehead">
    <span class="pdot idle" id="liveDot"></span>
    <h2>Live positions</h2>
    <span class="cnt" id="liveCnt">0</span>
  </div>
  <div class="livegrid" id="liveGrid"></div>

  <!-- ═══ KPI BENTO ═══ -->
  <div class="bento" id="bento"></div>

  <!-- ═══ DAILY STATS (перед Win-rate) ═══ -->
  <div class="sec-title">Статистика по дням</div>
  <div class="daygrid" id="dayGrid"></div>
  <div class="panel reveal" style="margin-bottom:26px">
    <h3>История по дням (последние 14 дней)</h3>
    <div style="overflow-x:auto"><table class="dailytbl" id="dailyTbl"></table></div>
  </div>

  <!-- ═══ MISSED OPPORTUNITIES (перед Win-rate) ═══ -->
  <div class="sec-title">Упущенные движения · 24ч</div>
  <div class="panel reveal" style="margin-bottom:26px">
    <h3>Рост > __MISSED_THRESHOLD__% без нашей сделки</h3>
    <div style="overflow-x:auto"><table class="dailytbl" id="missedTbl"></table></div>
  </div>

  <!-- ═══ WIN-RATE CHARTS ═══ -->
  <div class="grid2">
    <div class="panel reveal"><h3>Win-rate ≥1% по MOMENTUM входа</h3><canvas id="chMom"></canvas></div>
    <div class="panel reveal"><h3>Win-rate ≥1% по CVD_MOMENTUM входа</h3><canvas id="chCvd"></canvas></div>
    <div class="panel reveal"><h3>CVD_MOMENTUM × return@60m</h3><canvas id="chScatter"></canvas></div>
    <div class="panel reveal"><h3>Накопленный strategy PnL</h3><canvas id="chEquity"></canvas></div>
  </div>

  <!-- ═══ TOP 10 ═══ -->
  <div class="grid2">
    <div class="panel reveal"><h3>TOP 10 — лучший сигнал (return@60m)</h3><div id="topSig"></div></div>
    <div class="panel reveal"><h3>TOP 10 — лучшая стратегия (PnL)</h3><div id="topStr"></div></div>
  </div>
  <div class="panel reveal" style="margin-bottom:18px">
    <h3>Расхождение: отличный вход, плохой выход</h3><div id="topDiv"></div></div>

  <!-- ═══ TRADE HISTORY TABLE ═══ -->
  <div class="panel reveal">
    <h3>Архив сделок <span style="color:var(--mut-2);font-weight:400;font-size:12px">· клик по заголовку — сортировка</span></h3>
    <div class="tblwrap"><table class="main" id="tbl"></table></div>
  </div>

  <div class="foot" id="foot"></div>
</div>

<script>
const TRADES = __DATA__;
const OPEN   = __OPEN__;
const MISSED = __MISSED__;
const MOM_B=[3,5,7], CVD_B=[0,3,6,10];
const LOW=20;
const STATE_EMOJI={NEUTRAL:"⚪",ACCUMULATION:"🔍",EARLY_MOVE:"🌱",CONFIRMED_TREND:"🟢",
  ACCELERATION:"🚀",EXHAUSTION:"🟠",DISTRIBUTION:"🔴",INVALIDATED:"❌"};
let charts={};

const fmt=(v,d=1)=> v==null?'—':(+v).toFixed(d);
const trimZ=s=> s.replace(/\.?0+$/,'');
const pricef=v=>{
  if(v==null) return '—';
  const n=+v;
  if(!isFinite(n)) return '—';
  return Math.abs(n)>=0.01 ? trimZ(n.toFixed(4)) : trimZ(n.toPrecision(4));
};
const pctf=v=> v==null?'—':(v>0?'+':'')+v.toFixed(1)+'%';
const cls=v=> v==null?'':(v>0?'pos2':(v<0?'neg':'neu'));
const bucket=(v,e)=>{ if(v==null)return 'n/a'; for(const x of e){if(v<x)return '<'+x;} return '>='+e[e.length-1]; };
function bucketSortKey(k){
  if(k==='n/a') return [3,0,k];
  let m=/^<(-?\d+(?:\.\d+)?)$/.exec(k);
  if(m) return [0,parseFloat(m[1]),0];
  m=/^>=(-?\d+(?:\.\d+)?)$/.exec(k);
  if(m) return [0,parseFloat(m[1]),1];
  const n=parseFloat(k);
  if(!isNaN(n) && isFinite(n) && String(n)===k) return [0,n,0];
  return [2,0,k];
}
function sortBucketKeys(keys){
  return [...keys].sort((a,b)=>{
    const ka=bucketSortKey(a), kb=bucketSortKey(b);
    if(ka[0]!==kb[0]) return ka[0]-kb[0];
    if(ka[1]!==kb[1]) return ka[1]-kb[1];
    if(ka[2]!==kb[2]) return (ka[2]>kb[2]?1:-1);
    return 0;
  });
}
const median=a=>{ if(!a.length)return null; const s=[...a].sort((x,y)=>x-y); const m=(s.length-1)/2;
  const f=Math.floor(m),c=Math.min(f+1,s.length-1); return f===c?s[f]:s[f]+(s[c]-s[f])*(m-f); };
const mean=a=> a.length? a.reduce((x,y)=>x+y,0)/a.length : null;
const sum=a=> a.reduce((x,y)=>x+y,0);
const winrate=(a,lvl)=> a.length? a.filter(x=>x>=lvl).length/a.length*100 : null;
const dstr=ts=> ts? new Date(ts*1000).toISOString().slice(0,16).replace('T',' ') : '—';
const dayStr=ts=> ts? new Date(ts*1000).toISOString().slice(0,10) : null;
const acClass=a=> a==='equity'?'eq':a==='commodity'?'co':'';
function assetOk(a){ const v=document.getElementById('asset').value; return v==='all'||a===v; }
function filtered(){
  const l=document.getElementById('life').value;
  return TRADES.filter(t=>{
    if(!assetOk(t.asset_class)) return false;
    if(l==='long' && t.closed_before_60m!==false) return false;
    if(l==='short' && t.closed_before_60m!==true) return false;
    return true;
  });
}
function openFiltered(){ return OPEN.filter(o=>assetOk(o.asset_class)); }

/* ═══════════════════════════════════════════════════════════
   DAILY STATISTICS
   ═══════════════════════════════════════════════════════════ */
function computeDaily(rows){
  const days={};
  const now=new Date();
  const todayStr=now.toISOString().slice(0,10);
  const yest=new Date(now); yest.setDate(yest.getDate()-1);
  const yestStr=yest.toISOString().slice(0,10);

  for(const t of rows){
    const d=dayStr(t.entry_ts);
    if(!d) continue;
    if(!days[d]) days[d]={opened:0,closed:0,pos:0,neg:0,zero:0,pnl:0,rets:[]};
    days[d].opened++;
    if(t.strategy_pnl_pct!=null){
      days[d].closed++;
      days[d].pnl+=t.strategy_pnl_pct;
      if(t.strategy_pnl_pct>0) days[d].pos++;
      else if(t.strategy_pnl_pct<0) days[d].neg++;
      else days[d].zero++;
    }
    if(t.return_60m!=null) days[d].rets.push(t.return_60m);
  }
  for(let i=0;i<14;i++){
    const dt=new Date(now); dt.setDate(dt.getDate()-i);
    const k=dt.toISOString().slice(0,10);
    if(!days[k]) days[k]={opened:0,closed:0,pos:0,neg:0,zero:0,pnl:0,rets:[]};
  }
  return {days, todayStr, yestStr};
}

function renderDaily(rows){
  const {days, todayStr, yestStr}=computeDaily(rows);
  const grid=document.getElementById('dayGrid');
  const tbl=document.getElementById('dailyTbl');

  const periods=[
    {label:'Сегодня', filter:d=>d===todayStr},
    {label:'Вчера', filter:d=>d===yestStr},
    {label:'7 дней', filter:d=>{const diff=(Date.now()-new Date(d).getTime())/864e5; return diff<7;}},
    {label:'30 дней', filter:d=>{const diff=(Date.now()-new Date(d).getTime())/864e5; return diff<30;}},
    {label:'Всё время', filter:()=>true},
  ];

  let cardsHtml='';
  periods.forEach((p,i)=>{
    let opened=0,closed=0,pos=0,neg=0,pnl=0;
    for(const [d,v] of Object.entries(days)){
      if(!p.filter(d)) continue;
      opened+=v.opened; closed+=v.closed; pos+=v.pos; neg+=v.neg; pnl+=v.pnl;
    }
    const wr=closed? Math.round(pos/closed*100) : null;
    const isToday=i===0;
    cardsHtml+=`<div class="daycard${isToday?' today':''}" style="animation-delay:${i*60}ms">
      <div class="dlabel">${p.label}</div>
      <div class="dcount">${opened}</div>
      <div class="dbreak">
        <span class="pos">▲${pos}</span>
        <span class="neg">▼${neg}</span>
        <span class="neu">закрыто ${closed}</span>
      </div>
      <div class="dmeta">
        ${wr!=null?`win ${wr}% · `:''}PnL <span class="${pnl>=0?'pos2':'neg'}">${pnl>=0?'+':''}${pnl.toFixed(1)}%</span>
      </div>
    </div>`;
  });
  grid.innerHTML=cardsHtml;

  const sorted=Object.keys(days).sort().reverse().slice(0,14);
  let thead=`<tr><th class="l">Дата</th><th>Открыто</th><th>Закрыто</th>
    <th>▲ Плюс</th><th>▼ Минус</th><th>Win%</th><th>PnL</th><th>Медиана r60</th><th>Лучшая</th><th>Худшая</th></tr>`;
  let tbody='';
  for(const d of sorted){
    const v=days[d];
    if(v.opened===0 && v.closed===0) continue;
    const wr=v.closed? Math.round(v.pos/v.closed*100)+'%' : '—';
    const med=v.rets.length? median(v.rets) : null;
    const best=v.rets.length? Math.max(...v.rets) : null;
    const worst=v.rets.length? Math.min(...v.rets) : null;
    const isToday=d===todayStr;
    const label=isToday? 'Сегодня' : d===yestStr? 'Вчера' : d;
    tbody+=`<tr${isToday?' style="background:var(--panel-2)"':''}>
      <td class="l">${label}</td>
      <td>${v.opened}</td><td>${v.closed}</td>
      <td class="pos2">${v.pos}</td><td class="neg">${v.neg}</td>
      <td>${wr}</td>
      <td class="${v.pnl>=0?'pos2':'neg'}">${v.pnl>=0?'+':''}${v.pnl.toFixed(1)}%</td>
      <td>${med!=null?pctf(med):'—'}</td>
      <td class="pos2">${best!=null?pctf(best):'—'}</td>
      <td class="neg">${worst!=null?pctf(worst):'—'}</td>
    </tr>`;
  }
  tbl.innerHTML=thead+(tbody||'<tr><td class="l empty" colspan="10">Нет данных за последние 14 дней</td></tr>');
}

/* ═══════════════════════════════════════════════════════════
   MISSED OPPORTUNITIES
   ═══════════════════════════════════════════════════════════ */
function renderMissed(){
  const tbl=document.getElementById('missedTbl');
  if(!MISSED.length){
    tbl.innerHTML='<tr><td class="l empty">Нет упущенных движений за последние 24ч</td></tr>';
    return;
  }
  let head=`<tr><th class="l">Монета</th><th>Рост 24ч</th><th>Цена</th><th>Последний снимок</th></tr>`;
  let body='';
  for(const m of MISSED){
    body+=`<tr>
      <td class="l">${m.name||m.symbol} (${m.symbol})</td>
      <td class="pos2">${pctf(m.price_chg24)}</td>
      <td>${pricef(m.price)}</td>
      <td>${dstr(m.ts)}</td>
    </tr>`;
  }
  tbl.innerHTML=head+body;
}

/* ═══════════════════════════════════════════════════════════
   LIVE POSITIONS
   ═══════════════════════════════════════════════════════════ */
function renderLive(){
  const ops=openFiltered();
  const grid=document.getElementById('liveGrid');
  const dotL=document.getElementById('liveDot'), dotM=document.getElementById('mastDot');
  document.getElementById('liveCnt').textContent=ops.length;
  const alive=ops.length>0;
  dotL.classList.toggle('idle',!alive); dotM.classList.toggle('idle',!alive);
  if(!ops.length){
    grid.innerHTML='<div class="emptybox">Нет открытых позиций · система ждёт подтверждения тренда (CONFIRMED / ACCELERATION).</div>';
    return;
  }
  grid.innerHTML=ops.map((o,i)=>{
    const pnl=o.cur_pnl_pct;
    const warn=o.timeout_pct>=80;
    const se=STATE_EMOJI[o.state]||'·';
    return `<div class="pos-card ${acClass(o.asset_class)}" style="animation-delay:${i*50}ms">
      <div class="row1">
        <div>
          <div class="sym">${o.symbol}</div>
          <div class="nm">${o.name||''} · вход ${pricef(o.entry_price)} → ${pricef(o.last_price)}</div>
        </div>
        <div class="pnl ${cls(pnl)}" data-num="${pnl==null?'':pnl}">${pctf(pnl)}</div>
      </div>
      <div class="stg"><span class="se">${se}</span>${o.state||'—'} · ${o.entry_path||'—'} · держим ${fmt(o.hold_min,0)}м</div>
      <div class="bars">
        <div class="barlab"><span>удержание / таймаут __TIMEOUT_MIN__м</span><span>${o.timeout_pct}%</span></div>
        <div class="track"><div class="fill ${warn?'warn':''}" data-w="${o.timeout_pct}"></div></div>
      </div>
      <div class="metrics">
        <span>пик <b class="${cls(o.max_pnl_pct)}">${pctf(o.max_pnl_pct)}</b></span>
        <span>дно <b class="${cls(o.min_pnl_pct)}">${pctf(o.min_pnl_pct)}</b></span>
        <span>mom <b>${fmt(o.entry_momentum,0)}</b></span>
        <span>cvd_m <b>${fmt(o.entry_cvd_momentum,0)}</b></span>
      </div>
      <div class="tags">
        <span class="tag ${acClass(o.asset_class)}">${o.asset_class||''}</span>
        ${o.entry_pattern && o.entry_pattern!=='—' ? `<span class="tag">${o.entry_pattern}</span>` : ''}
        <span class="tag">${o.entry_earliness_label||'—'}</span>
        ${o.entry_divergence && o.entry_divergence!=='none' ? `<span class="tag">div ${o.entry_divergence}</span>` : ''}
      </div>
    </div>`;
  }).join('');
  requestAnimationFrame(()=>{
    grid.querySelectorAll('.fill').forEach(f=>{ f.style.width=f.dataset.w+'%'; });
    grid.querySelectorAll('.pnl[data-num]').forEach(el=>{
      const tgt=parseFloat(el.dataset.num); if(isNaN(tgt))return;
      const sign=tgt>=0?'+':'', t0=performance.now();
      const step=now=>{ const p=Math.min(1,(now-t0)/700), e=1-Math.pow(1-p,3);
        el.textContent=sign+(tgt*e).toFixed(1)+'%'; if(p<1)requestAnimationFrame(step); };
      requestAnimationFrame(step);
    });
  });
}

/* ═══════════════════════════════════════════════════════════
   KPI BENTO
   ═══════════════════════════════════════════════════════════ */
function kpi(label,valHtml,rawNum,opts={}){
  const d=document.createElement('div');
  d.className='kpi'+(opts.big?' big':'');
  d.style.animationDelay=(opts.delay||0)+'ms';
  const c=cls(rawNum);
  d.innerHTML = `<div class="l">${label}</div><div class="v ${opts.big?'':'s'} ${c}">${valHtml}</div>`
    + (opts.meta ? `<div class="m">${opts.meta}</div>` : '');
  return d;
}
function renderBento(rows,openN){
  const r60=rows.map(t=>t.return_60m).filter(v=>v!=null);
  const strat=rows.map(t=>t.strategy_pnl_pct).filter(v=>v!=null);
  const miss=rows.length-r60.length;
  const cov=h=> rows.length? Math.round(rows.filter(t=>t['return_'+h+'m']!=null).length/rows.length*100):0;
  const med=median(r60);
  const wr1=winrate(r60,1);
  const avgS=mean(strat);
  const dd=rows.map(t=>t.drawdown_from_peak_pct).filter(v=>v!=null);
  const ddMed=dd.length?median(dd):null;
  const last=[...rows].sort((a,b)=>(b.entry_ts||0)-(a.entry_ts||0))[0];
  const b=document.getElementById('bento'); b.innerHTML='';
  const heroVal = strat.length ? sum(strat) : null;
  const heroTxt = heroVal==null ? '—' : (heroVal>=0?'+':'') + heroVal.toFixed(1) + '%';
  const heroMeta = heroVal==null ? 'нет закрытых сделок'
    : `среднее ${pctf(avgS)} на сделку · ${strat.length} закрытых`;
  const hero=kpi('Накопленный strategy PnL', heroTxt, heroVal, {big:true, meta:heroMeta});
  b.appendChild(hero);
  b.appendChild(kpi('Открыто сейчас', openN, openN, {delay:60, meta: openN?'live-позиции':'ждём входа'}));
  const wr1Txt = wr1==null ? '—' : wr1.toFixed(0)+'%';
  b.appendChild(kpi('Win ≥1% @60m', wr1Txt, wr1, {delay:120, meta:`n=${r60.length}`}));
  b.appendChild(kpi('Медиана @60m', pctf(med), med, {delay:180}));
  b.appendChild(kpi('Coverage r60', cov(60)+'%', cov(60), {delay:240, meta:`miss ${miss}`}));
  b.appendChild(kpi('Coverage r120', cov(120)+'%', cov(120), {delay:300}));
  b.appendChild(kpi('Недобор от пика', pctf(ddMed), ddMed, {delay:360}));
  b.querySelectorAll('.kpi .v').forEach((el)=>{
    const txt=el.textContent.trim();
    const m=txt.match(/^([+-]?)(\d+(?:\.\d+)?)(%?)$/);
    if(m){ const sign=m[1], num=parseFloat(m[2]), suf=m[3];
      el.textContent=sign+'0'+suf;
      const t0=performance.now();
      const step=now=>{ const p=Math.min(1,(now-t0)/700); const e=1-Math.pow(1-p,3);
        el.textContent=sign+(num*e).toFixed(suf&&num>=10?0:1)+suf; if(p<1)requestAnimationFrame(step); };
      requestAnimationFrame(step); }
  });
  const liveLast=openFiltered()[0];
  document.getElementById('liveTxt').textContent = openN
    ? `${openN} открыто` + (liveLast ? ` · ${liveLast.symbol} ${pctf(liveLast.cur_pnl_pct)}` : '')
    : (last ? `last ${last.symbol} ${pctf(last.strategy_pnl_pct)} · ${dstr(last.entry_ts)}` : 'нет сделок');
}

/* ═══════════════════════════════════════════════════════════
   CHARTS
   ═══════════════════════════════════════════════════════════ */
function barByBucket(rows,key,edges,canvas){
  const g={};
  for(const r of rows){ const k=bucket(r[key],edges); (g[k]=g[k]||[]).push(r.return_60m); }
  const labels=sortBucketKeys(Object.keys(g));
  const wr=labels.map(k=>winrate(g[k].filter(v=>v!=null),1));
  const cnt=labels.map(k=>g[k].filter(v=>v!=null).length);
  const bg=cnt.map(n=> n<LOW ? '#5a6577' : '#2dd4bf');
  const lbl=labels.map((k,i)=> cnt[i]<LOW ? k+' *' : k);
  if(charts[canvas])charts[canvas].destroy();
  charts[canvas]=new Chart(document.getElementById(canvas),{type:'bar',
    data:{labels:lbl,datasets:[
      {label:'win≥1% %',data:wr,backgroundColor:bg,borderRadius:6,yAxisID:'y'},
      {label:'n (* = low sample)',data:cnt,type:'line',borderColor:'#93a0b8',
       backgroundColor:'#93a0b8',yAxisID:'y1',tension:.3,pointRadius:3}]},
    options:{responsive:true,animation:{duration:800},
      scales:{y:{ticks:{color:'#93a0b8',callback:v=>v+'%'},grid:{color:'#2c3447'}},
        y1:{position:'right',ticks:{color:'#6b7790'},grid:{drawOnChartArea:false}},
        x:{ticks:{color:'#93a0b8'},grid:{display:false}}},
      plugins:{legend:{labels:{color:'#93a0b8',font:{family:'IBM Plex Sans'}}}}}});
}
function scatter(rows,canvas){
  const pts=rows.filter(r=>r.entry_cvd_momentum!=null&&r.return_60m!=null)
    .map(r=>({x:r.entry_cvd_momentum,y:r.return_60m,
      bg:r.return_60m>=1?'#34d399':(r.return_60m<0?'#fb7185':'#fbbf24'),s:r.symbol}));
  if(charts[canvas])charts[canvas].destroy();
  charts[canvas]=new Chart(document.getElementById(canvas),{type:'scatter',
    data:{datasets:[{label:'сделки',data:pts,backgroundColor:pts.map(p=>p.bg),pointRadius:5,pointHoverRadius:8}]},
    options:{responsive:true,animation:{duration:800},
      scales:{x:{title:{display:true,text:'cvd_momentum',color:'#93a0b8'},ticks:{color:'#93a0b8'},grid:{color:'#2c3447'}},
        y:{title:{display:true,text:'return@60m %',color:'#93a0b8'},ticks:{color:'#93a0b8',callback:v=>v+'%'},grid:{color:'#2c3447'}}},
      plugins:{legend:{display:false},
        tooltip:{callbacks:{label:c=>`${c.raw.s}: cvd_m ${c.raw.x}, r60 ${c.raw.y}%`}}}}});
}
function equity(rows,canvas){
  const s=rows.filter(r=>r.strategy_pnl_pct!=null).sort((a,b)=>a.entry_ts-b.entry_ts);
  let cum=0; const labels=[],data=[];
  for(const r of s){ cum+=r.strategy_pnl_pct; labels.push(dstr(r.entry_ts)); data.push(+cum.toFixed(2)); }
  if(charts[canvas])charts[canvas].destroy();
  charts[canvas]=new Chart(document.getElementById(canvas),{type:'line',
    data:{labels,datasets:[{label:'накопленный PnL %',data,
      borderColor:'#34d399',backgroundColor:'rgba(52,211,153,.12)',fill:true,
      tension:.25,pointRadius:2,pointHoverRadius:6,borderWidth:2}]},
    options:{responsive:true,animation:{duration:900},
      scales:{x:{ticks:{color:'#93a0b8',maxTicksLimit:8,font:{family:'IBM Plex Mono',size:10}},grid:{display:false}},
        y:{ticks:{color:'#93a0b8',callback:v=>v+'%'},grid:{color:'#2c3447'}}},
      plugins:{legend:{labels:{color:'#93a0b8'}}}}});
}

/* ═══════════════════════════════════════════════════════════
   TABLE + TOP
   ═══════════════════════════════════════════════════════════ */
const COLS=[
  ['entry_ts','Дата',t=>dstr(t.entry_ts),1],
  ['symbol','Монета',t=>t.symbol,1],
  ['asset_class','Класс',t=>`<span class="tag ${acClass(t.asset_class)}">${t.asset_class||''}</span>`,1],
  ['entry_path','Путь',t=>t.entry_path||'—',1],
  ['entry_pattern','Паттерн',t=>t.entry_pattern||'—',1],
  ['entry_momentum','Mom',t=>fmt(t.entry_momentum,0)],
  ['entry_cvd_momentum','CVDm',t=>fmt(t.entry_cvd_momentum,0)],
  ['entry_earliness_label','Ранность',t=>t.entry_earliness_label||'—',1],
  ['entry_price','Вход',t=>pricef(t.entry_price)],
  ['exit_price','Выход',t=>pricef(t.exit_price)],
  ['strategy_pnl_pct','Strat%',t=>`<span class="${cls(t.strategy_pnl_pct)}">${pctf(t.strategy_pnl_pct)}</span>`],
  ['return_60m','r60%',t=>`<span class="${cls(t.return_60m)}">${pctf(t.return_60m)}</span>`],
  ['max_pnl_pct','Пик%',t=>pctf(t.max_pnl_pct)],
  ['closed_before_60m','<60м',t=> t.closed_before_60m?'<span class="neg">да</span>':'нет',1],
  ['hold_min','Держали',t=>fmt(t.hold_min,0)+'м'],
  ['exit_reason','Выход',t=>t.exit_reason||'—',1],
];
let sortKey='entry_ts', sortDir=-1;
function renderTable(rows){
  const data=[...rows].sort((a,b)=>{
    const x=a[sortKey],y=b[sortKey];
    if(x==null)return 1; if(y==null)return -1;
    return (x>y?1:x<y?-1:0)*sortDir;
  });
  const head='<tr>'+COLS.map(([k,l,,isL])=>
    `<th class="${isL?'l':''}" data-k="${k}">${l}${sortKey===k?(sortDir>0?' ▲':' ▼'):''}</th>`).join('')+'</tr>';
  const body=data.map(t=>'<tr>'+COLS.map(([k,,fn,isL])=>
    `<td class="${isL?'l':''}">${fn(t)}</td>`).join('')+'</tr>').join('');
  document.getElementById('tbl').innerHTML=head+(body||'<tr><td class="l empty">Нет сделок под фильтром</td></tr>');
  document.querySelectorAll('#tbl th').forEach(th=>th.onclick=()=>{
    const k=th.dataset.k; if(sortKey===k)sortDir*=-1; else{sortKey=k;sortDir=-1;} renderTable(filtered());
  });
}
function topList(rows,key,elId){
  const have=rows.filter(r=>r[key]!=null).sort((a,b)=>b[key]-a[key]).slice(0,10);
  document.getElementById(elId).innerHTML = have.length? have.map(r=>{
    return `<div class="toprow">
      <span class="nm">${r.symbol} <span class="tag">${r.entry_path||''}</span> <span class="tag">${r.entry_pattern||''}</span></span>
      <span class="meta">r60 <b class="${cls(r.return_60m)}">${pctf(r.return_60m)}</b> · strat <b class="${cls(r.strategy_pnl_pct)}">${pctf(r.strategy_pnl_pct)}</b> · mom ${fmt(r.entry_momentum,0)}</span>
    </div>`;
  }).join('') : '<div class="empty">Нет данных</div>';
}
function topDiv(rows){
  const have=rows.filter(r=>r.return_60m!=null&&r.strategy_pnl_pct!=null)
    .map(r=>({...r,_d:r.return_60m-r.strategy_pnl_pct})).sort((a,b)=>b._d-a._d).slice(0,8);
  document.getElementById('topDiv').innerHTML = have.length? have.map(r=>{
    return `<div class="toprow">
      <span class="nm">${r.symbol} <span class="tag">${r.exit_reason||''}</span></span>
      <span class="meta">r60 <b class="${cls(r.return_60m)}">${pctf(r.return_60m)}</b> → strat <b class="${cls(r.strategy_pnl_pct)}">${pctf(r.strategy_pnl_pct)}</b> · Δ <b class="neg">${r._d>0?'+':''}${r._d.toFixed(1)}%</b></span>
    </div>`;
  }).join('') : '<div class="empty">Нет данных</div>';
}

/* ═══════════════════════════════════════════════════════════
   RENDER ALL
   ═══════════════════════════════════════════════════════════ */
function renderAll(){
  const rows=filtered();
  const ops=openFiltered();
  renderLive();
  renderBento(rows,ops.length);
  renderDaily(rows);
  renderMissed();
  barByBucket(rows,'entry_momentum',MOM_B,'chMom');
  barByBucket(rows,'entry_cvd_momentum',CVD_B,'chCvd');
  scatter(rows,'chScatter');
  equity(rows,'chEquity');
  topList(rows,'return_60m','topSig');
  topList(rows,'strategy_pnl_pct','topStr');
  topDiv(rows);
  renderTable(rows);
  document.getElementById('foot').textContent =
    `live: ${ops.length} · архив: ${rows.length} · упущено 24ч: ${MISSED.length} · LOW SAMPLE < ${LOW}`;
}

const io = new IntersectionObserver(entries=>{
  entries.forEach(e=>{ if(e.isIntersecting){e.target.classList.add('in');io.unobserve(e.target);} });
},{threshold:.12});
function observeReveals(){ document.querySelectorAll('.reveal:not(.in)').forEach(el=>io.observe(el)); }

document.getElementById('asset').addEventListener('change', renderAll);
document.getElementById('life').addEventListener('change', renderAll);
renderAll(); observeReveals();
</script>
</body>
</html>"""


def main():
    trades, bad_lines = load_trades()
    open_positions = load_open()
    market_data = load_market_history()
    missed = compute_missed(market_data, trades, open_positions)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    html = (HTML
            .replace("__DATA__", json.dumps(trades, ensure_ascii=False))
            .replace("__OPEN__", json.dumps(open_positions, ensure_ascii=False))
            .replace("__TIMEOUT_MIN__", str(TRADE_TIMEOUT_MIN))
            .replace("__MISSED__", json.dumps(missed, ensure_ascii=False))
            .replace("__MISSED_THRESHOLD__", str(MISSED_THRESHOLD)))
    # [FIX F7] raise вместо assert (не отключается -O)
    for placeholder in ("__DATA__", "__OPEN__", "__TIMEOUT_MIN__",
                        "__MISSED__", "__MISSED_THRESHOLD__"):
        if placeholder in html:
            raise RuntimeError(
                f"make_dashboard.py: плейсхолдер {placeholder} не подставлен — "
                f"генерация остановлена, чтобы не закоммитить битый HTML")
    OUT.write_text(html, encoding="utf-8")
    bad_note = f" · ⚠ {bad_lines} битых строк" if bad_lines else ""
    print(f"Dashboard: {OUT}  ({len(trades)} закрытых · {len(open_positions)} открытых"
          f" · {len(missed)} упущенных{bad_note})")


if __name__ == "__main__":
    main()
