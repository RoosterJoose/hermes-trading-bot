#!/usr/bin/env python3

import json, os, sys
from datetime import datetime, timezone
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from dashboard_helpers import *

FRONTEND_DIR = Path(__file__).parent / "hermes-ui" / "out"
PORT = int(os.environ.get("PORT", 8502))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FRONTEND_DIR), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/dashboard":
            self._send_json(self._build_dashboard())
        elif path == "/api/trades":
            self._send_json(self._build_trades_endpoint(parsed))
        elif path == "/api/skip-analysis":
            data = get_skip_analysis()
            self._send_json(data.to_dict(orient="records") if not data.empty else [])
        elif path == "/api/scores":
            self._send_json(get_score_distribution())
        elif path == "/api/leaderboard/detail":
            data = get_leaderboard()
            self._send_json(data.to_dict(orient="records") if not data.empty else [])
        elif path == "/api/performance":
            self._send_json(get_performance_metrics() or {})
        elif path == "/api/readiness":
            rd = get_readiness()
            self._send_json(rd if "error" not in rd else {})
        elif path == "/api/market":
            self._send_json(get_market_context() or {})
        elif path == "/api/activity":
            all_events = get_activity_events(100)
            self._send_json(all_events)
        elif path == "/interactive":
            self._serve_interactive()
        elif path.startswith("/api/exports/"):
            filename = path.split("/api/exports/")[-1]
            exports_dir = Path(__file__).parent / "exports"
            filepath = exports_dir / filename
            if ".." in filename or "/" in filename:
                self._send_json({"error": "invalid path"}, 400)
            elif filepath.exists() and filepath.is_file():
                self.send_response(200)
                if filename.endswith(".csv"):
                    self.send_header("Content-Type", "text/csv")
                    self.send_header("Content-Disposition", 'attachment; filename="' + filename + '"')
                elif filename.endswith(".json"):
                    self.send_header("Content-Type", "application/json")
                elif filename.endswith(".md"):
                    self.send_header("Content-Type", "text/markdown")
                else:
                    self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(filepath.stat().st_size))
                self.end_headers()
                with open(filepath, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self._send_json({"error": "file not found"}, 404)
        elif path.startswith("/api/"):
            self._send_json({"error": "endpoint not found"}, 404)
        else:
            super().do_GET()

    def _build_trades_endpoint(self, parsed):
        from urllib.parse import parse_qs
        params = parse_qs(parsed.query)
        all_trades = get_all_trades()
        af = params.get("asset", [None])[0]
        if af:
            af = af.upper().replace("_USDT", "").replace("-PERP", "")
            all_trades = [t for t in all_trades if t.get("asset", "").upper().startswith(af)]
        limit = params.get("limit", [None])[0]
        if limit:
            try:
                all_trades = all_trades[:int(limit)]
            except ValueError:
                pass
        return {"trades": all_trades, "count": len(all_trades)}

    def _serve_interactive(self):
        hp = Path("/opt/data/hermes-trading/interactive_dashboard.html")
        if not hp.exists():
            self._send_json({"error": "not found"}, 404)
            return
        c = hp.read_text(encoding="utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(c.encode())))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(c.encode())

    def _build_dashboard(self):
        hb = get_heartbeat()
        hl = get_health()
        rd = get_readiness()
        pm = get_performance_metrics()
        ev = get_activity_events(30)
        mk = get_market_context()
        lb = get_leaderboard()
        ga = get_gate_attribution()
        ok = "error" not in hb
        stale = hl.get("status") == "stale"
        dd_data = mk.get("max_drawdown", {})
        btc_ctx = hb.get("btc_context", {}) if ok else {}
        fg = hb.get("fear_greed", {}) if ok else {}
        tr = hb.get("trust_state", {}) if ok else {}
        balance = pm.get("balance", 1000) if pm else 1000
        total_pnl = pm.get("total_pnl_pct", 0) or 0
        metrics_list = [
            rd.get("paper_days_met", False) if "error" not in rd else False,
            rd.get("min_trade_count_met", False) if "error" not in rd else False,
            not rd.get("sharpe_insufficient_data", True) if "error" not in rd else False,
            rd.get("max_drawdown_ok", False) if "error" not in rd else False,
            rd.get("uptime_met", False) if "error" not in rd else False,
            rd.get("stop_loss_ok", False) if "error" not in rd else False,
            rd.get("extremes_ok", True) if "error" not in rd else False,
            rd.get("data_integrity_ok", False) if "error" not in rd else False,
        ]
        met = sum(1 for m in metrics_list if m)
        readiness_score = int(met / len(metrics_list) * 100) if metrics_list else 0
        paused = {}
        for e in ev[:30]:
            if e.get("type") == "RISK_BLOCK" and e.get("asset"):
                a = e.get("asset", "")
                if a not in paused:
                    paused[a] = (e.get("message", "") or "")[:30]
        all_trades = get_all_trades()
        closed = [t for t in all_trades if t.get("exit_time")]
        closed_sorted = sorted(closed, key=lambda t: t.get("exit_time", ""), reverse=True)
        consec = 0
        for t in closed_sorted:
            if get_pnl(t) <= 0:
                consec += 1
            else:
                break
        today_str = datetime.now(timezone.utc).date().isoformat()
        today_t = [t for t in closed if t.get("exit_time", "").startswith(today_str)]
        daily_pnl = sum(get_pnl(t) for t in today_t)
        return {
            "mode": hb.get("mode", "paper") if ok else "paper",
            "paperBalance": round(balance, 2),
            "totalPnlPct": round(total_pnl, 2),
            "grossPnlPct": round(pm.get("gross_pnl", 0), 2) if pm else 0,
            "feesPct": round(pm.get("fees", 0), 2) if pm else 0,
            "fundingPct": round(pm.get("funding", 0), 2) if pm else 0,
            "profitFactor": pm.get("profit_factor", 0) if pm else 0,
            "expectancy": pm.get("expectancy", 0) if pm else 0,
            "winRate": round(pm.get("win_rate", 0), 1) if pm else 0,
            "totalTrades": pm.get("total_trades", 0) if pm else 0,
            "wins": pm.get("wins", 0) if pm else 0,
            "losses": pm.get("losses", 0) if pm else 0,
            "maxDrawdown": round(abs(dd_data.get("highest_dd_pct", 0) or 0), 1),
            "currentDrawdown": round(abs(dd_data.get("current_dd_pct", 0) or 0), 1),
            "btcPrice": round(btc_ctx.get("btc_price", 0), 2),
            "btcRsi": btc_ctx.get("btc_1h_rsi"),
            "fearGreedValue": fg.get("value", "-"),
            "fearGreedLabel": fg.get("classification", ""),
            "trustLabel": (tr.get("label", "-") or "-").upper(),
            "trustMultiplier": tr.get("multiplier", 1),
            "openPositionsCount": sum(1 for v in hb.get("positions", {}).values() if v is not None) if ok else 0,
            "maxPositions": 5,
            "portfolioHeat": 0,
            "dailyPnl": round(daily_pnl, 2),
            "dailyWins": sum(1 for t in today_t if get_pnl(t) > 0),
            "dailyLosses": sum(1 for t in today_t if get_pnl(t) <= 0),
            "consecutiveLosses": consec,
            "killSwitchActive": False,
            "dailyLossUsed": round(abs(daily_pnl) if daily_pnl < 0 else 0, 2),
            "dailyLossLimit": 3.0,
            "correlationExp": "PASS",
            "dataFreshness": "Healthy" if not stale else "Stale",
            "btcVolRegime": "Normal",
            "uptimePct": round((1 - ((hl.get("uptime_seconds", 0) or 0) / ((hl.get("uptime_seconds", 0) or 0) + 300))) * 100, 1),
            "paperDays": rd.get("paper_days_elapsed", 0) if "error" not in rd else 0,
            "requiredPaperDays": 30,
            "closedTradesReadiness": rd.get("total_trades", 0) if "error" not in rd else 0,
            "minTradeCount": rd.get("min_trade_count", 100) if "error" not in rd else 100,
            "sharpeRatio": rd.get("realized_sharpe") if "error" not in rd else None,
            "maxDrawdownPct": rd.get("max_drawdown_pct", 0) if "error" not in rd else 0,
            "maxDrawdownLimit": rd.get("max_drawdown_limit", 10) if "error" not in rd else 10,
            "uptimeHours": rd.get("uptime_hours", 0) if "error" not in rd else 0,
            "minUptimeHours": rd.get("min_uptime_hours", 168) if "error" not in rd else 168,
            "stopLossRatio": rd.get("stop_loss_ratio", 0) if "error" not in rd else 0,
            "stopLossLimit": rd.get("stop_loss_ratio_limit", 0.40) if "error" not in rd else 0.40,
            "dataIntegrity": rd.get("data_integrity_ok", False) if "error" not in rd else False,
            "extremeLosses": rd.get("extreme_losses", 0) if "error" not in rd else 0,
            "readinessScore": readiness_score,
            "isReady": rd.get("live_ready", False) if "error" not in rd else False,
            "blockers": rd.get("blockers", []) if "error" not in rd else [],
            "stale": stale,
            "pausedAssets": {k: v for k, v in list(paused.items())[:5]},
            "tunnelUrl": self._get_tunnel_url(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "positions": [
                {
                    "asset": k.replace("_USDT", "-PERP"),
                    "direction": "LONG",
                    "entryPrice": v.get("entry_price", 0),
                    "currentPrice": v.get("current_price"),
                    "exitPrice": v.get("stop_loss") or v.get("chandelier_exit"),
                    "pnlPct": v.get("pnl_pct"),
                    "unrealizedPnl": abs(v.get("unrealized_pnl", 0)) if v.get("unrealized_pnl") else 0,
                    "unrealizedPnlPct": abs(v.get("unrealized_pnl_pct", 0)) if v.get("unrealized_pnl_pct") else 0,
                    "size": v.get("position_size", 0),
                    "stopLoss": v.get("stop_loss", 0),
                    "takeProfit": v.get("take_profit", 0),
                    "takeProfit2": v.get("take_profit_2", 0),
                    "confidence": v.get("confidence", 0) or v.get("enter_confidence", 0) or 0,
                }
                for k, v in (hb.get("positions", {}) if ok else {}).items()
                if v is not None
            ] + [
                {
                    "asset": k.replace("_USDT", "-PERP"),
                    "direction": (v.get("signal") or v.get("strategy") or "LONG").upper(),
                    "entryPrice": v.get("entry_price", 0),
                    "currentPrice": v.get("current_price"),
                    "exitPrice": v.get("chandelier_exit"),
                    "pnlPct": v.get("pnl_pct"),
                    "unrealizedPnl": abs(v.get("unrealized_pnl", 0)) if v.get("unrealized_pnl") else 0,
                    "unrealizedPnlPct": abs(v.get("unrealized_pnl_pct", 0)) if v.get("unrealized_pnl_pct") else 0,
                    "size": v.get("position_size", 0),
                    "stopLoss": v.get("stop_loss", 0),
                    "takeProfit": v.get("take_profit", 0),
                    "takeProfit2": v.get("take_profit_2", 0),
                    "confidence": v.get("confidence", 0) or v.get("enter_confidence", 0) or 0,
                }
                for k, v in (hb.get("trend_positions", {}) if ok else {}).items()
            ],
            "equityCurve": self._build_equity_curve(),
            "activityEvents": [
                {
                    "timestamp": e.get("timestamp", ""),
                    "type": e.get("type", ""),
                    "asset": e.get("asset", ""),
                    "message": (e.get("message", "") or "")[:60],
                }
                for e in ev[:25]
            ],
            "leaderboard": [
                {
                    "asset": r.get("Asset", ""),
                    "trades": r.get("Trades", 0),
                    "netPnl": r.get("Net PnL", "0%"),
                    "winRate": r.get("Win Rate", "0%"),
                    "pf": r.get("PF", 0),
                    "status": r.get("Status", "STANDBY"),
                }
                for _, r in lb.head(10).iterrows()
            ] if not lb.empty else [],
            "gateAttribution": ga[:8] if ga else [],
        }

    def _build_equity_curve(self):
        try:
            eq = get_equity_curve()
            if eq.empty:
                return {"points": [], "drawdownPoints": []}
            points = [
                {"date": row["time"].strftime("%b %d") if hasattr(row["time"], "strftime") else str(row["time"])[:10],
                 "equity": round(float(row["equity"]), 2)}
                for _, row in eq.iterrows()
            ]
            peak = eq["equity"].cummax()
            dd_s = (eq["equity"] - peak) / peak * 100
            drawdownPoints = [
                {"date": points[i]["date"], "value": round(float(dd_s.iloc[i]), 2)}
                for i in range(len(dd_s))
            ]
            return {"points": points, "drawdownPoints": drawdownPoints}
        except Exception:
            return {"points": [], "drawdownPoints": []}

    def _send_json(self, data, status=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def _get_tunnel_url(self):
        try:
            f = self.directory
            parent = Path(f).parent.parent
            url_file = parent / "state" / "tunnel_url.txt"
            if url_file.exists():
                return url_file.read_text().strip()
        except Exception:
            pass
        return ""

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Hermes API Server -> http://0.0.0.0:{PORT}")
    print(f"  Frontend: serving {FRONTEND_DIR}")
    print(f"  API: endpoints at /api/")
    print(f"  Interactive: /interactive")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
