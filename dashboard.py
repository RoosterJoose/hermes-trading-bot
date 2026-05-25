#!/usr/bin/env python3
"""
Hermes Command Center — tab-based dashboard, clean and reliable.
"""
import sys, os, math
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dashboard_helpers import *

st.set_page_config(page_title="Hermes Command Center", page_icon="⚡", layout="wide",
                   initial_sidebar_state="collapsed")

REFRESH = 30

# ── Theme ──
C_BG = "#0a0c10"
C_PANEL = "#111318"
C_BORDER = "#262a30"
C_TEXT = "#e8eaed"
C_MUTED = "#8b949e"
C_DIM = "#6e7681"
C_GREEN = "#3fb950"
C_RED = "#f85149"
C_BLUE = "#58a6ff"
C_ORANGE = "#d29922"

st.markdown(f"""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
    * {{ font-family: 'Inter', -apple-system, sans-serif; }}
    .stApp {{ background: {C_BG}; }}
    .main .block-container {{ padding: 0.6rem 1rem; max-width: 1600px; }}
    h1, h2, h3 {{ color: {C_TEXT} !important; font-weight: 500; }}
    .panel {{ background: {C_PANEL}; border: 1px solid {C_BORDER}; border-radius: 7px; padding: 0.55rem 0.75rem; margin-bottom: 0.5rem; }}
    .p-title {{ color: {C_MUTED}; font-size: 0.6rem; text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600; margin-bottom: 0.35rem; }}
    .mc {{ background: #0d0f13; border: 1px solid {C_BORDER}; border-radius: 5px; padding: 0.35rem 0.55rem; }}
    .ml {{ color: {C_MUTED}; font-size: 0.55rem; text-transform: uppercase; letter-spacing: 0.3px; font-weight: 500; }}
    .mv {{ color: {C_TEXT}; font-size: 1rem; font-weight: 600; line-height: 1.3; }}
    .ms {{ color: {C_DIM}; font-size: 0.6rem; }}
    .pos {{ color: {C_GREEN}; }}
    .neg {{ color: {C_RED}; }}
    .warn {{ color: {C_ORANGE}; }}
    .info {{ color: {C_BLUE}; }}
    .tag {{ display: inline-block; padding: 0.07rem 0.35rem; border-radius: 3px; font-size: 0.55rem; font-weight: 600; letter-spacing: 0.3px; }}
    .tg-green {{ background: #122d1a; color: {C_GREEN}; border: 1px solid #1f4a2a; }}
    .tg-red {{ background: #2d1215; color: {C_RED}; border: 1px solid #4a1f22; }}
    .tg-yellow {{ background: #2d2a12; color: {C_ORANGE}; border: 1px solid #4a471f; }}
    .tg-cyan {{ background: #121d2d; color: {C_BLUE}; border: 1px solid #1f304a; }}
    .tg-gray {{ background: #161b22; color: {C_MUTED}; border: 1px solid {C_BORDER}; }}
    .act-row {{ border-bottom: 1px solid #1c2025; padding: 0.25rem 0; font-size: 0.72rem; display: flex; gap: 0.3rem; align-items: center; }}
    .act-row:last-child {{ border-bottom: none; }}
    .blocker {{ color: {C_RED}; font-size: 0.75rem; padding: 0.12rem 0; }}
    .blocker::before {{ content: "⚠ "; }}
    .stTabs [data-baseweb="tab-list"] {{ gap: 0; border-bottom: 1px solid {C_BORDER}; }}
    .stTabs [data-baseweb="tab"] {{ color: {C_MUTED}; font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.4px; padding: 0.4rem 0.8rem; }}
    .stTabs [aria-selected="true"] {{ color: {C_BLUE}; border-bottom: 2px solid {C_BLUE}; }}
    [data-testid="stMetricLabel"] {{ font-size: 0.6rem; color: {C_MUTED}; }}
    [data-testid="stMetricValue"] {{ font-size: 1rem; font-weight: 600; }}
    [data-testid="stDataFrame"] {{ font-size: 0.7rem; }}
    .metric-row {{ display: flex; justify-content: space-between; padding: 0.15rem 0; border-bottom: 1px solid #1c2025; font-size: 0.7rem; }}
    .metric-row:last-child {{ border-bottom: none; }}
    hr {{ border-color: {C_BORDER}; margin: 0.5rem 0; }}
</style>
""", unsafe_allow_html=True)

# ── Data ──
hb = get_heartbeat()
hl = get_health()
rd = get_readiness()
pm = get_performance_metrics()
eq = get_equity_curve()
po = get_positions()
ev = get_activity_events(20)
mk = get_market_context()
lb = get_leaderboard()
ga = get_gate_attribution()

ok = "error" not in hb
stale = hl.get("status") == "stale"
mode = (hb.get("mode","paper") if ok else "paper").upper()
dd_data = mk.get("max_drawdown", {})
btc_ctx = hb.get("btc_context", {}) if ok else {}
fg = hb.get("fear_greed", {}) if ok else {}
tr = hb.get("trust_state", {}) if ok else {}
op_data = hb.get("optimizer", {}) if ok else {}

def fmt(ts):
    if not ts: return "—"
    try:
        d = datetime.fromisoformat(ts)
        s = (datetime.now(timezone.utc) - d).total_seconds()
        if s < 60: return f"{int(s)}s"
        if s < 3600: return f"{int(s//60)}m"
        if s < 86400: return f"{int(s//3600)}h"
        return d.strftime("%b %d %H:%M")
    except: return ts[-8:] if ts and len(ts) >= 8 else "?"

def tag(t, label):
    m = {"ENTRY": "tg-green", "EXIT": "tg-gray", "SKIP": "tg-yellow",
         "RISK_BLOCK": "tg-red", "SIGNAL": "tg-cyan", "SYSTEM": "tg-cyan"}
    return f"<span class='tag {m.get(t,'tg-gray')}'>{label or t[:5]}</span>"

def safe_rsi(v):
    if v is None: return "—"
    try: return f"{v:.0f}"
    except: return "—"

# ═══════════════════════ HEADER ═══════════════════════
st.markdown("## ⚡ HERMES COMMAND CENTER")

c1, c2 = st.columns([2, 1])
with c1:
    st.markdown(
        f"<div style='display:flex;gap:0.4rem;font-size:0.7rem;color:{C_MUTED};flex-wrap:wrap;'>"
        f"<span class='tag {'tg-green' if not stale else 'tg-red'}'>"
        f"{'STALE' if stale else 'RUNNING'}</span>"
        f"<span>♥ {fmt(hb.get('timestamp','')) if ok else '—'}</span>"
        f"<span>↑ {(hl.get('uptime_seconds',0) or 0)/3600:.0f}h</span>"
        f"<span class='tag {'tg-green' if rd.get('live_ready',False) else 'tg-red'}'>"
        f"{'READY' if rd.get('live_ready',False) else 'LOCKED'}</span>"
        f"<span class='tag tg-yellow'>{mode}</span>"
        f"</div>", unsafe_allow_html=True)
with c2:
    st.caption(f"Auto-refresh every {REFRESH}s")

st.markdown("<hr>", unsafe_allow_html=True)

# ═══════════════════════ TABS ═══════════════════════
tab1, tab2, tab3, tab4, tab5 = st.tabs(["📊 Overview", "🚀 Readiness", "📈 Positions", "📋 Activity", "🔬 Research"])

# ─── TAB 1: OVERVIEW ───
with tab1:
    if ok and pm:
        b = pm.get("balance", 0)
        tp = pm.get("total_pnl_pct", 0) or 0
        wr = pm.get("win_rate", 0)
        pf = pm.get("profit_factor", 0)
        tt = pm.get("total_trades", 0)
        dd = abs(dd_data.get("highest_dd_pct", 0) or 0)
        k1,k2,k3,k4,k5,k6 = st.columns(6)
        k1.markdown(f"<div class='mc'><div class='ml'>Balance</div><div class='mv'>${b:,.0f}</div></div>", unsafe_allow_html=True)
        k2.markdown(f"<div class='mc'><div class='ml'>Net PnL</div><div class='mv {'pos' if tp>=0 else 'neg'}'>{tp:+.2f}%</div></div>", unsafe_allow_html=True)
        k3.markdown(f"<div class='mc'><div class='ml'>Win Rate</div><div class='mv'>{wr:.1f}%</div></div>", unsafe_allow_html=True)
        k4.markdown(f"<div class='mc'><div class='ml'>PF</div><div class='mv'>{pf}</div></div>", unsafe_allow_html=True)
        k5.markdown(f"<div class='mc'><div class='ml'>Trades</div><div class='mv'>{tt}</div></div>", unsafe_allow_html=True)
        k6.markdown(f"<div class='mc'><div class='ml'>Max DD</div><div class='mv neg'>{dd:.1f}%</div></div>", unsafe_allow_html=True)

    # Charts row
    cc, dc = st.columns([2, 1.1])
    with cc:
        st.markdown("<div class='p-title'>📈 Equity Curve</div>", unsafe_allow_html=True)
        if not eq.empty:
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=eq["time"], y=eq["equity"], mode="lines",
                line=dict(color=C_BLUE, width=2), fill="tozeroy", fillcolor="rgba(88,166,255,0.05)"))
            s = eq["equity"].iloc[0]
            fig.add_hline(y=s, line_dash="dash", line_color="#30363d",
                annotation_text=f"start ${s:.0f}", annotation_font_color=C_DIM)
            fig.update_layout(height=210, margin=dict(l=0,r=0,t=0,b=0),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                xaxis=dict(showgrid=False, color=C_DIM),
                yaxis=dict(showgrid=True, gridcolor="#1c2025", color=C_DIM, tickprefix="$"),
                hovermode="x unified", showlegend=False)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.markdown("<div class='panel' style='text-align:center;padding:1.5rem;color:#6e7681;'>No trades yet</div>", unsafe_allow_html=True)

    with dc:
        st.markdown("<div class='p-title'>📉 Drawdown</div>", unsafe_allow_html=True)
        if not eq.empty and len(eq) > 1:
            peak = eq["equity"].cummax()
            dd_s = (eq["equity"] - peak) / peak * 100
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=eq["time"], y=dd_s, mode="lines",
                line=dict(color=C_RED, width=1.5), fill="tozeroy", fillcolor="rgba(248,81,73,0.08)"))
            fig.add_hline(y=dd_s.min(), line_dash="dash", line_color=C_RED,
                annotation_text=f"{dd_s.min():.1f}%", annotation_font_color=C_RED)
            fig.update_layout(height=130, margin=dict(l=0,r=0,t=0,b=0),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                xaxis=dict(showgrid=False, color=C_DIM),
                yaxis=dict(showgrid=True, gridcolor="#1c2025", color=C_DIM, ticksuffix="%"),
                hovermode="x unified", showlegend=False)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.markdown("<div class='panel' style='text-align:center;padding:1rem;color:#6e7681;'>No data</div>", unsafe_allow_html=True)

        dds = dd_data
        c1,c2 = st.columns(2)
        c1.markdown(f"<div class='mc'><div class='ml'>Max DD</div><div class='mv neg'>{abs(dds.get('highest_dd_pct',0)):.1f}%</div></div>", unsafe_allow_html=True)
        c2.markdown(f"<div class='mc'><div class='ml'>Current DD</div><div class='mv warn'>{abs(dds.get('current_dd_pct',0)):.1f}%</div></div>", unsafe_allow_html=True)

    st.markdown("<hr>", unsafe_allow_html=True)

    # Risk + Activity + Positions
    rk, ac, pc = st.columns([1.1, 1.4, 2])
    with rk:
        st.markdown("<div class='p-title'>🛡️ Risk Cockpit</div>", unsafe_allow_html=True)
        if ok:
            tl = (tr.get("label","—")).upper()
            bp = btc_ctx.get("btc_price",0)
            rs = safe_rsi(btc_ctx.get("btc_1h_rsi"))
            st.markdown(
                f"<div class='mc'><div class='ml'>Trust</div><div><span class='{'pos' if tl=='HIGH' else 'warn'}' style='font-weight:600;'>{tl}</span>"
                f" <span class='ms'>×{tr.get('multiplier',1)}</span></div></div>"
                f"<div class='mc'><div class='ml'>₿ BTC</div><div style='font-weight:500;'>${bp:,.0f}</div><div class='ms'>RSI {rs}</div></div>"
                f"<div class='mc'><div class='ml'>😱 Fear & Greed</div><div style='font-weight:500;'>{fg.get('value','—')} <span class='ms'>{fg.get('classification','')}</span></div></div>"
                f"<div class='mc'><div class='ml'>🎲 MC 95% DD</div><div style='font-weight:500;'>{hb.get('monte_carlo',{}).get('dd_95_pct','—')}%</div></div>"
                f"<div class='mc'><div class='ml'>📦 Optimizer</div><div><span class='info'>{op_data.get('status','—').upper()}</span></div>"
                f"<div class='ms'>{op_data.get('trades_needed',0)} trades needed</div></div>", unsafe_allow_html=True)
        else:
            st.markdown("<div class='panel' style='color:#da3633;'>No data</div>", unsafe_allow_html=True)

    with ac:
        st.markdown("<div class='p-title'>📋 Recent Activity</div>", unsafe_allow_html=True)
        if ev:
            h = ""
            for e in ev[:10]:
                h += (f"<div class='act-row'>"
                      f"<span style='color:{C_DIM};white-space:nowrap;'>{fmt(e.get('timestamp',''))}</span>"
                      f"{tag(e.get('type',''), e.get('type',''))}"
                      f"{'<span style=color:#58a6ff;>'+e['asset']+'</span>' if e.get('asset') else ''}"
                      f"<span>{(e.get('message','') or '')[:55]}</span></div>")
            st.markdown(h, unsafe_allow_html=True)
        else:
            st.markdown("<div class='panel' style='color:#6e7681;'>No activity yet</div>", unsafe_allow_html=True)

    with pc:
        st.markdown("<div class='p-title'>📊 Open Positions</div>", unsafe_allow_html=True)
        op = {k:v for k,v in hb.get("positions",{}).items() if v} if ok else {}
        tp = hb.get("trend_positions",{}) if ok else {}
        if not op and not tp:
            st.markdown("<div class='panel' style='color:#6e7681;text-align:center;'>No open positions</div>", unsafe_allow_html=True)
        else:
            allp = {**op, **tp}
            cols = st.columns(min(len(allp), 3))
            for i, (ak, p) in enumerate(allp.items()):
                with cols[i % len(cols)]:
                    ep = p.get("entry_price",0)
                    sig = (p.get("signal") or p.get("strategy") or "long").upper()
                    sl = "TREND" if ak in tp else "REV"
                    st.markdown(
                        f"<div style='background:#0d0f13;border:1px solid {C_BORDER};border-radius:6px;padding:0.5rem 0.65rem;'>"
                        f"<div style='display:flex;justify-content:space-between;'>"
                        f"<span style='font-weight:600;'>{ak.replace('_USDT','')}</span>"
                        f"<div>{tag('SYSTEM',sig)} <span class='tag tg-cyan'>{sl}</span></div></div>"
                        f"<div style='font-size:1.1rem;font-weight:600;margin:0.2rem 0;'>${ep:.4f}</div>"
                        f"<div class='ms'>entry</div></div>", unsafe_allow_html=True)

# ─── TAB 2: READINESS ───
with tab2:
    if "error" in rd:
        st.markdown("<div class='panel' style='color:#da3633;padding:2rem;text-align:center;'>No readiness data</div>", unsafe_allow_html=True)
    else:
        rdy = rd.get("live_ready", False)
        metrics = [rd.get("paper_days_met",False), rd.get("min_trade_count_met",False),
                   not rd.get("sharpe_insufficient_data",True), rd.get("max_drawdown_ok",False),
                   rd.get("uptime_met",False), rd.get("stop_loss_ok",False),
                   rd.get("extremes_ok",True), rd.get("data_integrity_ok",False)]
        met = sum(1 for m in metrics if m)
        score = int(met/len(metrics)*100)

        c1, c2 = st.columns([1, 2])
        with c1:
            fig = go.Figure(go.Indicator(
                mode="gauge+number+delta", value=score,
                number={"suffix": "/100", "font":{"color":C_TEXT,"size":32}},
                gauge={"axis":{"range":[None,100],"tickcolor":C_DIM},
                       "bar":{"color":C_BLUE},"bgcolor":"#0d0f13",
                       "steps":[{"range":[0,50],"color":"#2d1215"},{"range":[50,80],"color":"#2d2a12"},{"range":[80,100],"color":"#122d1a"}],
                       "threshold":{"line":{"color":C_RED,"width":4},"thickness":0.75,"value":rd.get("required_percent",80)}}))
            fig.update_layout(height=250, margin=dict(l=0,r=0,t=0,b=0),
                paper_bgcolor="rgba(0,0,0,0)", font=dict(color=C_MUTED))
            st.plotly_chart(fig, use_container_width=True)

        with c2:
            st.markdown(f"<div style='font-size:1.1rem;font-weight:600;color:{C_GREEN if rdy else C_RED};'>{'✓ READY FOR LIVE' if rdy else '⚠ NOT READY'}</div>", unsafe_allow_html=True)
            st.markdown(f"<div style='font-size:0.7rem;color:{C_MUTED};margin-bottom:0.5rem;'>Paper assessment • {met}/{len(metrics)} gates passed</div>", unsafe_allow_html=True)

            m_rows = ""
            m_items = [
                ("Paper Days", rd.get("paper_days_elapsed",0), rd.get("required_paper_days",30), rd.get("paper_days_met",False)),
                ("Closed Trades", rd.get("total_trades",0), rd.get("min_trade_count",100), rd.get("min_trade_count_met",False)),
                ("Sharpe", f"{rd.get('realized_sharpe','—')}", "0.80", rd.get("sharpe_met",False)),
                ("Max Drawdown", f"{rd.get('max_drawdown_pct',0):.1f}%", f"{rd.get('max_drawdown_limit',10):.0f}%", rd.get("max_drawdown_ok",False)),
                ("Uptime", f"{rd.get('uptime_hours',0):.0f}h", f"{rd.get('min_uptime_hours',168)}h", rd.get("uptime_met",False)),
                ("Stop-Loss Ratio", f"{rd.get('stop_loss_ratio',0)*100:.0f}%", f"{rd.get('stop_loss_ratio_limit',0.40)*100:.0f}%", rd.get("stop_loss_ok",False)),
                ("Data Integrity", "PASS" if rd.get("data_integrity_ok",False) else "FAIL", "PASS", rd.get("data_integrity_ok",False)),
                ("Extreme Losses", str(rd.get("extreme_losses",0)), "0", rd.get("extremes_ok",True)),
            ]
            for label, current, target, met in m_items:
                c = C_GREEN if met else (C_TEXT if label in ("Sharpe","Max Drawdown","Uptime") else C_RED)
                m_rows += f"<div class='metric-row'><span class='ms'>{label}</span><span style='color:{c};'>{current}</span><span style='color:{C_DIM};'>{target}</span></div>"

            st.markdown(
                f"<div class='panel'><div class='metric-row' style='font-size:0.55rem;color:{C_MUTED};text-transform:uppercase;'>"
                f"<span>Metric</span><span>Current</span><span>Target</span></div>{m_rows}</div>", unsafe_allow_html=True)

        blockers = rd.get("blockers",[])
        if blockers:
            st.markdown("<hr>", unsafe_allow_html=True)
            st.markdown("<div class='p-title'>⚠ Blockers</div>", unsafe_allow_html=True)
            for b in blockers:
                st.markdown(f"<div class='blocker'>{b}</div>", unsafe_allow_html=True)
            st.markdown(f"<div style='margin-top:0.3rem;font-size:0.65rem;color:{C_DIM};'>🔒 Live trading locked until all requirements met.</div>", unsafe_allow_html=True)

# ─── TAB 3: POSITIONS ───
with tab3:
    if ok:
        op = {k:v for k,v in hb.get("positions",{}).items() if v}
        tp = hb.get("trend_positions",{})
        if not op and not tp:
            st.markdown("<div class='panel' style='text-align:center;padding:2rem;color:#6e7681;'>No open positions</div>", unsafe_allow_html=True)
        else:
            allp = {**op, **tp}
            for ak, p in allp.items():
                ep = p.get("entry_price",0)
                sig = (p.get("signal") or p.get("strategy") or "long").upper()
                sl = "TREND" if ak in tp else "REV"
                with st.container():
                    st.markdown(
                        f"<div style='background:#0d0f13;border:1px solid {C_BORDER};border-radius:6px;padding:0.6rem;margin-bottom:0.4rem;'>"
                        f"<div style='display:flex;justify-content:space-between;'>"
                        f"<span style='font-weight:600;'>{ak.replace('_USDT','')} — {sig}</span>"
                        f"<span class='tag tg-cyan'>{sl}</span></div>"
                        f"<div style='font-size:1.2rem;font-weight:600;margin:0.3rem 0;'>${ep:.4f}</div>"
                        f"<div class='ms'>entry price</div></div>", unsafe_allow_html=True)

        if not lb.empty:
            st.markdown("<hr>", unsafe_allow_html=True)
            st.markdown("<div class='p-title'>📊 Per-Asset Performance</div>", unsafe_allow_html=True)
            st.dataframe(lb, use_container_width=True, hide_index=True)
    else:
        st.markdown("<div class='panel' style='color:#da3633;padding:2rem;text-align:center;'>No heartbeat data</div>", unsafe_allow_html=True)

# ─── TAB 4: ACTIVITY ───
with tab4:
    if ev:
        st.markdown("<div class='p-title'>📋 Full Activity Log</div>", unsafe_allow_html=True)
        h = ""
        for e in ev:
            h += (f"<div class='act-row'>"
                  f"<span style='color:{C_DIM};white-space:nowrap;'>{fmt(e.get('timestamp',''))}</span>"
                  f"{tag(e.get('type',''), e.get('type',''))}"
                  f"{'<span style=color:#58a6ff;>'+e['asset']+'</span>' if e.get('asset') else ''}"
                  f"<span>{(e.get('message','') or '')[:80]}</span></div>")
        st.markdown(h, unsafe_allow_html=True)
    else:
        st.markdown("<div class='panel' style='text-align:center;padding:2rem;color:#6e7681;'>No activity yet</div>", unsafe_allow_html=True)

    # Gate attribution
    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown("<div class='p-title'>🚧 Gate Attribution</div>", unsafe_allow_html=True)
    if ga and ga[0].get("count",0) > 0:
        names = [g["gate"][:25] for g in ga[:8]]
        counts = [g["count"] for g in ga[:8]]
        pcts = [g["pct"] for g in ga[:8]]
        fig = go.Figure()
        fig.add_trace(go.Bar(y=names, x=counts, orientation="h",
            marker_color=C_ORANGE,
            text=[f"{c} ({p}%)" for c,p in zip(counts,pcts)],
            textposition="outside"))
        fig.update_layout(height=250, margin=dict(l=0,r=0,t=0,b=0),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(showgrid=False, color=C_DIM),
            yaxis=dict(showgrid=False, color=C_MUTED, autorange="reversed"),
            font=dict(color=C_MUTED), showlegend=False)
        st.plotly_chart(fig, use_container_width=True)
        st.markdown(f"<div class='ms'>Total blocks: {sum(g['count'] for g in ga)}</div>", unsafe_allow_html=True)
    else:
        st.markdown("<div class='panel' style='color:#6e7681;text-align:center;'>No block data yet</div>", unsafe_allow_html=True)

# ─── TAB 5: RESEARCH ───
with tab5:
    st.markdown("<div class='p-title'>🔬 Skip Analysis</div>", unsafe_allow_html=True)
    sa = get_skip_analysis()
    if not sa.empty:
        st.dataframe(sa, use_container_width=True, hide_index=True)
    else:
        st.markdown("<div class='panel' style='color:#6e7681;text-align:center;'>No skip data</div>", unsafe_allow_html=True)

    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown("<div class='p-title'>📊 Confidence Score Distribution</div>", unsafe_allow_html=True)
    sd = get_score_distribution()
    if sd.get("scores"):
        fig = go.Figure()
        fig.add_trace(go.Histogram(x=sd["scores"], nbinsx=15, marker_color=C_BLUE))
        fig.update_layout(height=200, margin=dict(l=0,r=0,t=0,b=0),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(showgrid=False, color=C_DIM, title="Confidence"),
            yaxis=dict(showgrid=True, gridcolor="#1c2025", color=C_DIM),
            font=dict(color=C_MUTED))
        st.plotly_chart(fig, use_container_width=True)
        st.markdown(f"<div class='ms'>Total entries with scores: {sd['count']}</div>", unsafe_allow_html=True)
    else:
        st.markdown("<div class='panel' style='color:#6e7681;text-align:center;'>No confidence score data</div>", unsafe_allow_html=True)

    if not lb.empty:
        st.markdown("<hr>", unsafe_allow_html=True)
        st.markdown("<div class='p-title'>📈 Asset Leaderboard</div>", unsafe_allow_html=True)
        st.dataframe(lb, use_container_width=True, hide_index=True)
