"""
loop.py — Async trading loop, 60s interval.
Every cycle:
  1. Pull data from all adapters
  2. Fetch BTC market context (1h RSI) once per cycle
  3. Validate adapter schemas
  4. Calculate RSI from candle data
  5. Check BTC gate + Fear & Greed gate before entry
  6. Enter only if RSI < threshold AND no position AND cooldown elapsed AND market conditions OK
  7. Check stop loss / take profit for open positions
  8. Log heartbeat
"""

import asyncio
import json
import os
import sqlite3
import sys
import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

from hermes_trading import state as hm_state

from hermes_trading.adaptive import (
    compute_dynamic_rsi_threshold,
    compute_hurst_exponent,
    compute_cusum_regime,
    compute_rsi_percentile_threshold,
    check_vol_sanity,
)

from hermes_trading.universe import (
    fetch_hl_meta_and_ctx,
    parse_universe,
    screen_and_rank,
    format_universe_report,
)

from hermes_trading.risk import (
    PortfolioTracker,
    correlation_allows_entry,
    compute_correlations,
    compute_var,
    var_position_cap,
)
from hermes_trading.trust_state import compute_trust_state, status as trust_status
from hermes_trading.optimizer import trades_ready, check_and_optimize

from hermes_trading.schema import SchemaError, validate_adapter_output


class TradingLoop:
    def __init__(
        self,
        assets: List[Dict],
        state_dir: Path,
        base_dir: Path,
        mode: str = "paper",
        initial_balance: float = 10000.0,
        replay_mode: bool = False,
        quiet: bool = False,
    ):
        self.assets = assets
        self.state_dir = state_dir
        self.base_dir = base_dir
        self.mode = mode
        self.replay_mode = replay_mode
        self.quiet = quiet
        self.cycle_interval = 60  # seconds — check every minute
        self.max_consecutive_failures = 5
        self.user_risk_accepted = (
            os.environ.get("HERMES_TRADING_I_ACCEPT_RISK", "").lower() == "true"
        )

        # Open positions per asset
        self.positions: Dict[str, Optional[Dict]] = {a["key"]: None for a in assets}

        # Cooldown: cycles since last trade close (prevents rapid re-entry)
        self.cycles_since_last_trade: Dict[str, int] = {a["key"]: 999 for a in assets}

        # Trade count tracker (for reflection trigger)
        self.trade_count_since_reflection: Dict[str, int] = {
            a["key"]: 0 for a in assets
        }

        # Cached BTC context (refreshed once per cycle)
        self.btc_context: dict = {}
        self.last_fear_greed: dict = {}

        # Phase 4: Hyperliquid market context (funding, OI, universe)
        self.hl_context: dict = {}
        self.universe_ranked: list = []
        self.universe_scan_counter: int = 0

        # Cumulative market context log for reflection analysis
        self.market_snapshots: list = []

        # Step 5: Kill switches state
        self.daily_pnl_pct: Optional[float] = 0.0
        self.last_daily_reset: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.last_prices: Dict[str, Optional[float]] = {a["key"]: None for a in assets}
        self.stale_price_cycles: Dict[str, int] = {a["key"]: 0 for a in assets}
        self.consecutive_losses: Dict[str, int] = {a["key"]: 0 for a in assets}

        # P3: Auto-pause after max_consecutive_losses
        self.paused_assets: Dict[str, str] = {}  # asset_key -> timestamp when paused

        # Z-score cooling chamber (z-score < threshold triggers N-cycle block)
        self.zscore_cooldown: Dict[
            str, int
        ] = {}  # asset_key -> remaining blocked cycles

        # BTC 1h realized vol history for surge detection
        self.btc_vol_history: List[float] = []  # rolling absolute 1h returns
        self.btc_vol_surge_blocked: bool = False
        self.btc_vol_surge_reason: str = ""

        # OI Velocity tracking (hourly snapshots for 48h window)
        self.oi_cycle_counter: int = 0
        self.oi_snapshots: Dict[str, List[float]] = {}  # symbol -> [hourly OI values]

        # Phase 6: Advanced Risk Management
        self.correlations: dict = {}
        self.correlation_window = 90
        self.portfolio_tracker = PortfolioTracker(
            initial_equity=10000.0, max_drawdown_pct=15.0
        )
        self.var_cache: dict = {}
        self.risk_log: list = []

        # Rolling win-rate tracking (Phase 2: 20-trade per asset, 50-trade portfolio)
        self.asset_trade_results: Dict[str, list] = {
            a["key"]: [] for a in assets
        }  # bool: True=win, False=loss
        self.portfolio_trade_results: list = []  # global rolling trade outcomes

        # Same-candle entry guard — prevents TP/SL on entry candle
        self._just_entered: Set[str] = set()

        # BTC correlation sector cap — rolling 1h return correlation per asset
        self._btc_correlations: Dict[str, float] = {}  # asset_key → rolling Pearson r
        self._btc_hourly_return_log: list = []  # rolling 24 entries of BTC 1h returns
        self._asset_hourly_return_logs: Dict[str, list] = {
            a["key"]: [] for a in assets
        }  # asset_key → [24 1h returns]
        self._last_corr_update: float = 0  # timestamp of last correlation update
        self._last_btc_price_for_corr: Optional[float] = None
        self._last_asset_prices_for_corr: Dict[str, float] = {}

        # ADX danger zone gate (resamples 1m to 15m/1h, checks ADX + EMA200)
        self.adx_danger_blocked: Dict[str, bool] = {a["key"]: False for a in assets}

        # Phase 3: Trend-following sleeve (60/40 dual sleeve architecture)
        self.trend_positions: Dict[
            str, Optional[Dict]
        ] = {}  # asset_key -> position dict
        self.trend_1h_candles: Dict[str, list] = {}  # asset_key -> [1h OHLC dicts]
        self.trend_1h_ready: Dict[str, bool] = {a["key"]: False for a in assets}
        self.trend_cooldown: Dict[str, int] = {}  # cycles since last trend entry
        self.max_concurrent_total = 3  # max 3-4 total across both sleeves
        self.trend_universe = {"BTC_USDT", "ETH_USDT", "SOL_USDT", "XRP_USDT", "BNB_USDT", "DOGE_USDT", "AVAX_USDT", "NEAR_USDT"}

        # Portfolio-level daily loss hard halt (stops ALL entries)
        self.portfolio_hard_halt_pct = 4.0  # -4% portfolio daily → global halt
        self.portfolio_loss_halted = False  # Current halt state
        self.portfolio_halt_latched = False  # Hard latch — once set, stays until UTC day reset

        # Event-risk calendar kill switch
        self.event_cal_blackout: Optional[Dict] = None  # Blocked state from is_near_macro_event()
        self._init_event_calendar()

        # Paper balance tracking (dollar PnL) — persist start date across restarts
        self.initial_balance = initial_balance
        self.paper_balance = initial_balance
        start_date_file = self.state_dir / "paper_start_date.txt"
        if start_date_file.exists():
            self.paper_start_date = start_date_file.read_text().strip()
        else:
            self.paper_start_date = datetime.now(timezone.utc).date().isoformat()
            start_date_file.write_text(self.paper_start_date)

        # Replay historical trades into paper balance (survives crashes/restarts)
        self._replay_historical_trades()

        # Session start balance — persists across crashes so daily PnL kill-switch survives
        # Format: JSON with date field ({"date": "2026-05-25", "balance": 962.36})
        # If stored date doesn't match today, session resets (handles crash across UTC midnight)
        ssf = self.state_dir / "session_start_balance.json"
        if ssf.exists():
            try:
                import json as _json
                data = _json.loads(ssf.read_text())
                stored_date = data.get("date", "")
                stored_balance = float(data.get("balance", self.initial_balance))
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if stored_date == today:
                    self.session_start_balance = stored_balance
                else:
                    # Date mismatch — new UTC day since last write (or crash across midnight)
                    self.session_start_balance = self.paper_balance
                    hm_state.atomic_write_json(ssf, {"date": today, "balance": round(self.session_start_balance, 2)})
            except (ValueError, OSError, _json.JSONDecodeError):
                self.session_start_balance = self.paper_balance
                hm_state.atomic_write_json(ssf, {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "balance": round(self.session_start_balance, 2)})
        else:
            self.session_start_balance = self.paper_balance
            hm_state.atomic_write_json(ssf, {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "balance": round(self.session_start_balance, 2)})

        # Trust-state scaling (unified risk score)
        self.trust_multiplier: float = 1.0
        self.trust_label: str = "high"

        # Monte Carlo 95% DD threshold (loaded from backtest data)
        self.mc_dd_threshold: Optional[float] = None
        self._load_mc_dd_threshold()

        # Optimizer readiness
        self.optimizer_status: dict = {"status": "dormant"}
        self.optimizer_logs: dict = {}  # asset_key → list of optimization results

    def _init_event_calendar(self):
        """Load event calendar config and build events list."""
        from hermes_trading.event_calendar import load_event_calendar
        goal_path = self.state_dir / "goal.yaml"
        self._event_calendar_events = load_event_calendar(goal_path)

    def _check_event_calendar(self) -> Dict:
        """Check if we're in a macro-event blackout window.
        
        Returns {'blocked': bool, 'reason': str or None, ...}
        """
        from hermes_trading.event_calendar import is_near_macro_event
        # Load config from goal.yaml
        goal_path = self.state_dir / "goal.yaml"
        flatten_mins = 120  # default: 2h before
        hold_mins = 60      # default: 1h after
        try:
            import yaml
            if goal_path.exists():
                with open(goal_path) as f:
                    cfg = yaml.safe_load(f) or {}
                ec = cfg.get("event_calendar", {})
                if not ec.get("enabled", True):
                    return {"blocked": False, "reason": None,
                            "nearest_event": None, "minutes_until_event": None}
                flatten_mins = ec.get("flatten_minutes_before", flatten_mins)
                hold_mins = ec.get("hold_minutes_after", hold_mins)
        except Exception:
            pass
        return is_near_macro_event(
            self._event_calendar_events,
            flatten_minutes_before=flatten_mins,
            hold_minutes_after=hold_mins,
        )

    def _flatten_all_positions(self, reason: str, price_data: dict):
        """Flatten ALL sleeves — MR and trend positions — for a given reason.
        
        Called by event-risk calendar kill switch to exit before macro events.
        """
        from datetime import datetime, timezone
        ctx = self._current_market_context()
        now_price = price_data.get("price", 0)
        # Flatten MR positions
        for asset_key, pos in list(self.positions.items()):
            if pos is not None:
                price = pos.get("current_price", now_price)
                entry = pos["entry_price"]
                pnl = ((price - entry) / entry) * 100 if price and entry else 0
                self._close_position(asset_key, price, pnl, f"flatten_{reason}",
                                     price_data, ctx)
                print(f"  {asset_key}: 🚨 FLATTENED (MR) — {reason}")
        # Flatten trend positions
        for asset_key, pos in list(self.trend_positions.items()):
            if pos is not None:
                price = pos.get("current_price", now_price)
                entry = pos["entry_price"]
                pnl = ((price - entry) / entry) * 100 if price and entry else 0
                self._close_position(asset_key, price, pnl, f"flatten_{reason}",
                                     price_data, ctx)
                self.trend_positions[asset_key] = None
                print(f"  {asset_key}: 🚨 FLATTENED (TREND) — {reason}")

    async def run(self):
        """Main loop — runs until cancelled."""
        sys.path.insert(0, str(self.base_dir))
        from hermes_trading.adapters import (
            PriceAdapter,
            OnChainAdapter,
            NewsAdapter,
            MacroAdapter,
        )

        price = PriceAdapter(mode=self.mode)
        onchain = OnChainAdapter(mode=self.mode)
        news = NewsAdapter(mode=self.mode)
        macro = MacroAdapter(mode=self.mode)

        print(f"🔄 Trading loop started — checking every {self.cycle_interval}s")
        print(
            f"   Mode: {self.mode.upper()} | Assets: {', '.join(a['key'] for a in self.assets)}"
        )
        print(f"   Strategy: RSI entry (< threshold) + BTC market gate + FnG filter")
        first_strat = self._load_strategy(self.assets[0]["key"])
        default_cooldown = first_strat.get("cooldown_cycles", 30) if first_strat else 30
        print(f"   Cooldown: {default_cooldown} cycles (configurable per-asset)")

        if self.mode == "live" and not self.user_risk_accepted:
            print(
                "⚠️  LIVE MODE: Set HERMES_TRADING_I_ACCEPT_RISK=true to enable live trading"
            )
            print("   Worker will observe-only until flag is set")

        try:
            while True:
                # ── Fetch BTC market context once per cycle ──
                self.btc_context = await self._fetch_btc_context(price)

                # ── Fetch Fear & Greed once per cycle ──
                fng_data = await macro.fetch(self.assets[0]["key"])
                if fng_data.get("available"):
                    self.last_fear_greed = fng_data.get("indicators", {}).get(
                        "fear_greed", {}
                    )

                # Phase 4: Fetch Hyperliquid market context (funding, OI, universe)
                await self._fetch_hl_context()

                # Log market snapshot
                self._log_market_snapshot()

                # BTC 1m vol surge detection (updates rolling vol history)
                self._update_btc_vol_history()

                # OI Velocity snapshot (once per 60 cycles ≈ hourly)
                self._update_oi_snapshots()

                # Portfolio-level daily loss hard halt — stops ALL sleeves (hard latch)
                # Once triggered, stays blocked until the next UTC day reset.
                # Check if we've crossed into a new UTC day (daily PnL reset cycle)
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if today != self.last_daily_reset:
                    # New UTC day — clear both halt and latch, persist session_start_balance
                    if self.portfolio_halt_latched:
                        print(f"📅 New UTC day ({today}) — portfolio halt latch cleared")
                    self.portfolio_loss_halted = False
                    self.portfolio_halt_latched = False
                    # Persist new session start balance for crash survival
                    self.session_start_balance = self.paper_balance
                    try:
                        hm_state.atomic_write_json(
                            self.state_dir / "session_start_balance.json",
                            {"date": today, "balance": round(self.session_start_balance, 2)},
                        )
                    except OSError:
                        pass

                # Trigger halt if daily loss exceeds threshold
                if (
                    self.daily_pnl_pct is not None
                    and self.daily_pnl_pct <= -abs(self.portfolio_hard_halt_pct)
                ):
                    if not self.portfolio_halt_latched:
                        print(
                            f"⚠️ PORTFOLIO HALT: daily PnL {self.daily_pnl_pct:+.2f}% "
                            f"<= -{abs(self.portfolio_hard_halt_pct):.1f}% — entries blocked for rest of UTC day"
                        )
                    self.portfolio_loss_halted = True
                    self.portfolio_halt_latched = True

                # If latched but PnL drifted above threshold, keep halted until day reset
                if self.portfolio_halt_latched and not self.portfolio_loss_halted:
                    self.portfolio_loss_halted = True

                # Event-risk calendar check — flatten & block before macro events
                cal_check = self._check_event_calendar()
                if cal_check["blocked"]:
                    if not getattr(self, "_event_cal_warned", False):
                        print(f"📅 EVENT CAL KILL SWITCH: {cal_check['reason']}")
                        # Flatten once when blackout starts
                        btc_px = (self.btc_context or {}).get("btc_price", 0)
                        price_data = {"price": btc_px, "source": "event_calendar"}
                        self._flatten_all_positions("macro_event", price_data)
                        self._event_cal_warned = True
                    self._event_cal_blackout = cal_check
                else:
                    self._event_cal_blackout = None
                    self._event_cal_warned = False

                # Update BTC correlation matrix (sector cap data) once per ~hour
                self._update_btc_correlations(self.btc_context)

                # ── Cycle through each asset ──
                for asset_cfg in self.assets:
                    key = asset_cfg["key"]
                    strategy = self._load_strategy(key)
                    if not strategy:
                        continue

                    # Portfolio-level hard halt — skip all asset processing
                    if self.portfolio_loss_halted:
                        continue

                    # Event calendar blackout — skip entries (flatten already done)
                    if self._event_cal_blackout:
                        continue

                    await self._cycle(
                        key, asset_cfg, strategy, price, onchain, news, macro
                    )

                self._write_heartbeat()
                self._write_runtime()
                await asyncio.sleep(self.cycle_interval)

        finally:
            await price._close_exchanges()

    async def _fetch_btc_context(self, price_adapter) -> dict:
        """Fetch BTC 1h candles and compute RSI on 1h and 4h timeframes."""
        ctx = {
            "btc_1h_rsi": None,
            "btc_4h_rsi": None,
            "btc_price": None,
            "available": False,
        }

        try:
            # Fetch 1h BTC candles (need enough for RSI 14 on 4h sampling = ~60 1h candles)
            data = await price_adapter.fetch("BTC_USDT", timeframe="1h", limit=80)
            candles = data.get("candles", [])
            if len(candles) >= 30:
                closes_1h = [c["close"] for c in candles]
                ctx["btc_price"] = closes_1h[-1]
                ctx["btc_1h_rsi"] = self._calc_rsi(closes_1h, period=14)

                # 4h RSI: sample every 4th 1h close (need 14*4+1 = 57 candles minimum)
                if len(closes_1h) >= 57:
                    closes_4h = closes_1h[-(14 * 4) :][
                        ::4
                    ]  # every 4th close for last 56 closes
                    ctx["btc_4h_rsi"] = self._calc_rsi(closes_4h, period=14)
                else:
                    # Fallback: use RSI on 1h data with longer period as proxy
                    ctx["btc_4h_rsi"] = self._calc_rsi(closes_1h, period=56)

                ctx["available"] = True

            btc_status = (
                f"1h={ctx['btc_1h_rsi']:.0f}" if ctx["btc_1h_rsi"] else "1h=---"
            )
            btc_status += (
                f" 4h={ctx['btc_4h_rsi']:.0f}" if ctx["btc_4h_rsi"] else " 4h=---"
            )
            print(f"  BTC: ${ctx['btc_price']:.0f} | RSI {btc_status}")
        except Exception as e:
            print(f"  ⚠ BTC context fetch failed: {e}")

        return ctx

    async def _fetch_hl_context(self):
        """Fetch Hyperliquid market data (funding, OI, universe).

        Provides per-asset funding rates and OI data for all tracked assets.
        Runs full universe scan every 15 cycles to minimize API calls.
        """
        self.universe_scan_counter += 1
        run_scan = self.universe_scan_counter >= 15

        try:
            raw = await fetch_hl_meta_and_ctx()
            if raw is None:
                return

            parsed = parse_universe(raw)

            # Build a lookup dict by symbol for OI/funding data
            lookup = {}
            self.hl_context["assets"] = {}
            for a in parsed:
                lookup[a["symbol"]] = a
                self.hl_context["assets"][a["symbol"]] = a

            self.hl_context["available"] = True

            # Log funding for each tracked asset
            for asset_cfg in self.assets:
                symbol = asset_cfg["key"].split("_")[0]
                if symbol in lookup:
                    hla = lookup[symbol]
                    annualized = hla["funding_annualized_pct"]
                    oi_billions = hla["open_interest_usd"] / 1e9
                    print(
                        f"  📊 {symbol}: fund={annualized:+.3f}% APY | OI=${oi_billions:.2f}B"
                    )

            # Run universe scan every 15 cycles (not bandwidth-heavy but rate-limit aware)
            if run_scan:
                self.universe_scan_counter = 0
                ranked = screen_and_rank(parsed)
                self.universe_ranked = ranked
                if ranked:
                    print(format_universe_report(ranked))
                else:
                    print("  🌌 Universe scan: < 3 assets eligible — no-trade state")

        except Exception as e:
            print(f"  ⚠ HL context fetch failed: {e}")

    def _log_market_snapshot(self):
        """Store a periodic market snapshot for reflection analysis."""
        snapshot = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "btc_1h_rsi": self.btc_context.get("btc_1h_rsi"),
            "btc_4h_rsi": self.btc_context.get("btc_4h_rsi"),
            "fear_greed_value": self._fng_value(),
            "fear_greed_class": self._fng_class(),
        }
        self.market_snapshots.append(snapshot)
        # Keep last 1000 snapshots
        if len(self.market_snapshots) > 1000:
            self.market_snapshots = self.market_snapshots[-1000:]

    def _fng_value(self) -> Optional[int]:
        val = self.last_fear_greed.get("value")
        try:
            return int(val) if val is not None else None
        except (ValueError, TypeError):
            return None

    def _fng_class(self) -> str:
        return self.last_fear_greed.get("classification", "")

    def _btc_gate_allows_entry(self, strategy: dict) -> tuple:
        """Check if BTC market conditions allow long entries.

        Returns:
            (allowed: bool, reason: str)
        """
        btc_gate = strategy.get("btc_gate", {})

        btc_1h_rsi = self.btc_context.get("btc_1h_rsi")

        if btc_1h_rsi is not None and btc_1h_rsi < btc_gate.get("min_btc_1h_rsi", 20):
            return (
                False,
                f"BTC 1h RSI {btc_1h_rsi:.0f} < {btc_gate['min_btc_1h_rsi']} (deeply oversold)",
            )

        return (True, "")

    def _fng_gate_allows_entry(self, strategy: dict) -> tuple:
        """Check if Fear & Greed allows long entries.

        Returns:
            (allowed: bool, reason: str)
        """
        fng_gate = strategy.get("fng_gate", {})
        min_fng = fng_gate.get("min_value", 10)

        fng_val = self._fng_value()
        if fng_val is not None and fng_val < min_fng:
            return (False, f"Fear & Greed {fng_val} < {min_fng} (Extreme Fear)")

        return (True, "")

    def _evaluate_entry(self, asset_key: str, candles: list, strategy: dict) -> tuple:
        """Entry Evaluator — second-opinion technical check before entering.

        Checks:
          1. Lower low cascade — if last 3 lows each lower than prev, still dropping
          2. Volume panic — if current volume > 1.5x avg of last 10, panic selling
          3. Candle positioning — if price in bottom 30% of candle range, still weak
          4. Falling knife — if price below EMA20 - ATR*mult with volume spike

        Returns:
            (allowed: bool, reason: str, features: dict)
        """
        evaluator = strategy.get("evaluator", {})
        features = {
            "cascade_found": False,
            "cascade_check": 0,
            "volume_ratio": 0.0,
            "vol_check": 0.0,
            "candle_position": 0.0,
            "pos_check": 0.0,
            "falling_knife_check": 0.0,
            "falling_knife_vol": 0.0,
        }

        if not evaluator.get("enabled", True):
            return (True, "evaluator_disabled", features)

        if len(candles) < 12:
            return (True, "insufficient_data", features)

        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        volumes = [c.get("volume", 0) or 0 for c in candles]

        # 1. Lower low cascade — last N candles, each low lower than previous
        cascade_check = evaluator.get("lower_low_cascade", 3)
        features["cascade_check"] = cascade_check
        if cascade_check > 0 and len(lows) >= cascade_check + 1:
            recent_lows = lows[-(cascade_check + 1) :]
            is_cascading = all(
                recent_lows[i] > recent_lows[i + 1] for i in range(cascade_check)
            )
            features["cascade_found"] = is_cascading
            if is_cascading:
                return (
                    False,
                    f"lower_low_cascade: {cascade_check} consec lower lows",
                    features,
                )

        # 2. Volume panic — current volume vs average
        vol_check = evaluator.get("volume_spike_mult", 1.5)
        features["vol_check"] = vol_check
        if vol_check > 0 and len(volumes) >= 11 and volumes[-1] > 0:
            avg_vol = sum(volumes[-11:-1]) / 10
            features["volume_ratio"] = volumes[-1] / avg_vol if avg_vol > 0 else 0.0
            if avg_vol > 0 and volumes[-1] > avg_vol * vol_check:
                return (
                    False,
                    f"volume_spike: {volumes[-1] / avg_vol:.1f}x avg",
                    features,
                )

        # 3. Candle positioning — where is price within current candle range
        pos_check = evaluator.get("min_candle_position", 0.30)
        features["pos_check"] = pos_check
        if pos_check > 0 and len(candles) >= 1:
            current_high = highs[-1]
            current_low = lows[-1]
            candle_range = current_high - current_low
            if candle_range > 0:
                position_in_candle = (closes[-1] - current_low) / candle_range
                features["candle_position"] = round(position_in_candle, 4)
                if position_in_candle < pos_check:
                    return (
                        False,
                        f"candle_position: {position_in_candle:.0%} < {pos_check:.0%}",
                        features,
                    )

        # 4. Falling knife prevention — ATR-based price drop + volume surge
        if evaluator.get("falling_knife_enabled", True) and len(candles) >= 25:
            # Determine thresholds by asset tier
            if "BTC" in asset_key or "ETH" in asset_key:
                fk_drop_mult = evaluator.get("falling_knife_drop_btc", 2.0)
                fk_vol_mult = evaluator.get("falling_knife_vol_btc", 2.5)
            else:
                fk_drop_mult = evaluator.get("falling_knife_drop_alt", 3.5)
                fk_vol_mult = evaluator.get("falling_knife_vol_alt", 4.0)

            # Calculate EMA20 for reference level
            ema20 = self._calc_ema_value(closes, period=20)
            if ema20 is not None and ema20 > 0:
                atr_pct = self._calc_atr(candles, period=14)
                if atr_pct is not None:
                    atr_price = atr_pct * closes[-1]
                    # Threshold: EMA20 - ATR * drop_mult
                    fk_price_threshold = ema20 - (atr_price * fk_drop_mult)
                    current_price = closes[-1]

                    # Calculate drop distance in ATR units
                    drop_from_ema = (
                        (ema20 - current_price) / atr_price if atr_price > 0 else 0
                    )
                    features["falling_knife_check"] = round(drop_from_ema, 2)

                    if current_price < fk_price_threshold:
                        # Check volume surge
                        if len(volumes) >= 21:
                            avg_vol_fk = sum(volumes[-21:-1]) / 20
                        elif len(volumes) >= 11:
                            avg_vol_fk = sum(volumes[-11:-1]) / 10
                        else:
                            avg_vol_fk = 0

                        vol_ratio = volumes[-1] / avg_vol_fk if avg_vol_fk > 0 else 0
                        features["falling_knife_vol"] = round(vol_ratio, 2)

                        if vol_ratio >= fk_vol_mult:
                            return (
                                False,
                                f"falling_knife: price {drop_from_ema:.1f} ATR below EMA20, vol {vol_ratio:.1f}x",
                                features,
                            )

        return (True, "evaluator_passed", features)

    def _log_rejected_entry(
        self,
        asset_key: str,
        price: float,
        reason: str,
        rsi: Optional[float],
        threshold: float,
        eval_features: Optional[dict] = None,
    ):
        """Log a rejected entry to rejections.jsonl for reflection analysis."""
        entry = {
            "asset": asset_key,
            "price": round(price, 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "rsi": round(rsi, 1) if rsi is not None else None,
            "threshold": threshold,
            "decision": "rejected",
            "btc_1h_rsi": self.btc_context.get("btc_1h_rsi"),
            "btc_4h_rsi": self.btc_context.get("btc_4h_rsi"),
            "fear_greed": self._fng_value(),
            "eval_features": eval_features or {},
        }
        rej_file = self.state_dir / asset_key / "evaluator_log.jsonl"
        rej_file.parent.mkdir(parents=True, exist_ok=True)
        with open(rej_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _log_evaluator_pass(
        self,
        asset_key: str,
        price: float,
        rsi: Optional[float],
        threshold: float,
        eval_features: dict,
    ):
        """Log an evaluator pass (accepted entry) for analysis."""
        # Check benchmark targets for this asset
        from hermes_trading.optimizer import check_benchmarks

        trades_file = self.state_dir / asset_key / "trades.jsonl"
        bm_check = {"status": "insufficient_data"}
        if trades_file.exists():
            import json

            tlines = [l for l in trades_file.read_text().strip().split("\n") if l]
            if len(tlines) >= 10:
                recent = [json.loads(l) for l in tlines[-10:]]
                bm_check = check_benchmarks(recent)

        entry = {
            "asset": asset_key,
            "price": round(price, 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": "evaluator_passed",
            "rsi": round(rsi, 1) if rsi is not None else None,
            "threshold": threshold,
            "decision": "accepted",
            "btc_1h_rsi": self.btc_context.get("btc_1h_rsi"),
            "btc_4h_rsi": self.btc_context.get("btc_4h_rsi"),
            "fear_greed": self._fng_value(),
            "eval_features": eval_features,
            "benchmarks": {
                "sharpe_target": bm_check.get("sharpe_target"),
                "sharpe_current": bm_check.get("sharpe"),
                "sharpe_met": bm_check.get("sharpe_met"),
                "win_rate_target": bm_check.get("win_rate_target"),
                "win_rate_current": bm_check.get("win_rate"),
                "win_rate_met": bm_check.get("win_rate_met"),
                "max_dd_target": bm_check.get("max_dd_target"),
                "max_dd_current": bm_check.get("max_drawdown"),
                "max_dd_met": bm_check.get("max_dd_met"),
            }
            if bm_check.get("status") == "analyzed"
            else {},
        }
        log_file = self.state_dir / asset_key / "evaluator_log.jsonl"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _log_evaluator_flag(
        self,
        asset_key: str,
        price: float,
        reason: str,
        rsi: Optional[float],
        threshold: float,
        eval_features: dict,
        score_override: bool = False,
    ):
        """Log evaluator flag — evaluator noted something but confidence score overrode it.

        This replaces the old 'rejected_entry' log. The evaluator is informational
        only in the confidence score architecture — flags are logged for analysis,
        not as blocks.
        """
        entry = {
            "asset": asset_key,
            "price": round(price, 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "rsi": round(rsi, 1) if rsi is not None else None,
            "threshold": threshold,
            "decision": "flagged",
            "score_override": score_override,
            "btc_1h_rsi": self.btc_context.get("btc_1h_rsi"),
            "btc_4h_rsi": self.btc_context.get("btc_4h_rsi"),
            "fear_greed": self._fng_value(),
            "eval_features": eval_features,
            "note": "Informational only — confidence score overrides evaluator flags",
        }
        log_file = self.state_dir / asset_key / "evaluator_log.jsonl"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _current_market_context(self) -> dict:
        """Return current market context dict for logging in trades."""
        ctx = {
            "btc_1h_rsi": self.btc_context.get("btc_1h_rsi"),
            "btc_4h_rsi": self.btc_context.get("btc_4h_rsi"),
            "btc_price": self.btc_context.get("btc_price"),
            "fear_greed_value": self._fng_value(),
            "fear_greed_class": self._fng_class(),
        }
        # Add HL funding/OI data
        hl_assets = self.hl_context.get("assets", {})
        if hl_assets:
            ctx["hl_available"] = True
        return ctx

    async def _cycle(
        self,
        asset_key: str,
        goal: dict,
        strategy: dict,
        price_adapter,
        onchain_adapter,
        news_adapter,
        macro_adapter,
    ):
        """Run one trading cycle for a single asset."""

        # ── 1. Fetch data from all adapters ──
        price_data = await price_adapter.fetch(asset_key)
        onchain_data = await onchain_adapter.fetch(asset_key)
        news_data = await news_adapter.fetch(asset_key)
        macro_data = await macro_adapter.fetch(asset_key)

        # ── 2. Validate adapter schemas — halt loop on mismatch ──
        try:
            validate_adapter_output("price", price_data)
            validate_adapter_output("onchain", onchain_data)
            validate_adapter_output("news", news_data)
            validate_adapter_output("macro", macro_data)
        except SchemaError as e:
            print(f"❌ {asset_key}: Schema validation FAILED — {e}")
            print("   Halting loop: cannot trade on unreliable data")
            raise

        # ── 3. Check circuit breaker ──
        if price_adapter.consecutive_failures >= self.max_consecutive_failures:
            print(
                f"🔴 {asset_key}: Circuit breaker — {price_adapter.consecutive_failures} consecutive failures"
            )
            return

        current_price = price_data.get("current_price", 0.0)
        if current_price <= 0:
            return

        # ── Consecutive losses decay: reduce streak if asset has been idle ──
        cl = self.consecutive_losses.get(asset_key, 0)
        if cl > 0:
            idle_cycles = self.cycles_since_last_trade.get(asset_key, 0)
            decay_threshold = max(50, cl * 25)  # more aggressive decay for longer streaks
            if idle_cycles >= decay_threshold:
                self.consecutive_losses[asset_key] = max(0, cl - 1)
                self.paused_assets.pop(asset_key, None)
                print(f"🔁 {asset_key}: consec loss streak decayed {cl}→{cl-1} after {idle_cycles} idle cycles")
        # ── 4. Calculate RSI from candle data ──
        candles = price_data.get("candles", [])
        rsi = None
        if len(candles) >= 15:
            closes = [c["close"] for c in candles]
            rsi = self._calc_rsi(closes)

        # ── 5. Check market gates ──
        entry = strategy.get("entry", {})
        threshold = entry.get("threshold", 30)
        has_position = self.positions.get(asset_key) is not None
        cooldown_cycles = strategy.get("cooldown_cycles", 30)
        cooldown_remaining = cooldown_cycles - self.cycles_since_last_trade.get(
            asset_key, 999
        )
        rsi_status = f"RSI={rsi or 0:.1f}" if rsi is not None else "RSI=---"

        # Per-asset percentile RSI threshold (q05 majors / q10 alts from bars.db)
        # Takes priority over dynamic RSI when enough data accumulated
        dynamic = None
        pct_rsi = self._compute_rsi_percentile_threshold(asset_key)
        if pct_rsi.get("active"):
            effective_threshold = pct_rsi["threshold"]
            print(
                f"  {asset_key}: 📊 percentile RSI threshold={effective_threshold} ({pct_rsi['percentile']}th pctile, {pct_rsi['bars_used']} 1m bars)"
            )
        else:
            # Phase 3: Dynamic RSI Percentile (overrides fixed threshold when enough 1m bar data)
            dynamic = self._compute_dynamic_rsi(asset_key, strategy)
            effective_threshold = (
                dynamic["threshold"] if dynamic.get("active") else threshold
            )
            if dynamic.get("active"):
                print(
                    f"  {asset_key}: 📊 dynamic RSI threshold={effective_threshold} ({dynamic['percentile']}th pctile, {dynamic['bars_used']} 1m bars)"
                )
            elif dynamic.get("reason") and dynamic["reason"] not in ("not enabled",):
                pass  # silently accumulating data

        # Phase 3b: Hurst Exponent regime classification (1m bar R/S analysis)
        hurst = self._compute_hurst(asset_key, strategy)
        if hurst.get("active"):
            regime_label = hurst["regime"].replace("_", " ")
            print(f"  {asset_key}: 📈 Hurst H={hurst['hurst']:.4f} — {regime_label}")
        elif hurst.get("reason") and hurst["reason"] not in ("not enabled",):
            pass  # silently accumulating data

        # Directional Hurst — determine mode from H(t)
        hurst_mode = "mean_reversion"
        if not hurst.get("active"):
            hurst_mode = "random_walk"  # unknown regime → smaller MR size
        hurst_signal = None
        h = hurst.get("hurst")
        if h is not None and hurst.get("active"):
            regime = hurst.get("regime", "random_walk")
            if regime in ("trending", "strongly_trending"):
                hurst_mode = "trend"
                print(f"  {asset_key}: 📈 Trending regime (H={h:.3f}) — MR entries use trend scoring")
            elif regime in ("mean_reverting", "strongly_mean_reverting"):
                hurst_mode = "mean_reversion"
                hurst_signal = rsi is not None and rsi < effective_threshold
                print(
                    f"  {asset_key}: 🔄 MR regime (H={h:.3f}) — oversold entries (RSI<{effective_threshold})"
                )
            else:
                hurst_mode = "random_walk"
                hurst_signal = rsi is not None and rsi < effective_threshold
                print(f"  {asset_key}: ⏸️ Random walk (H={h:.3f}) — reduced MR entries")

        # Phase 3c: CUSUM regime detection (1m log return cumulative sum)
        cusum = self._compute_cusum(asset_key, strategy)
        if cusum.get("active"):
            regime_label = cusum["regime"].replace("_", " ")
            print(
                f"  {asset_key}: 🔄 CUSUM {regime_label} ({cusum['up_breaks']}↑/{cusum['down_breaks']}↓ breaks)"
            )
        elif cusum.get("reason") and cusum["reason"] not in ("not enabled",):
            pass  # silently accumulating data

        # ADX danger zone check (15m/1h trend extreme detection)
        self._update_adx_danger_zones(asset_key, candles, strategy)

        btc_allowed, btc_reason = self._btc_gate_allows_entry(strategy)
        fng_allowed, fng_reason = self._fng_gate_allows_entry(strategy)
        market_blocked = not btc_allowed or not fng_allowed
        market_gate_reasons = []
        if not btc_allowed:
            market_gate_reasons.append(btc_reason)
        if not fng_allowed:
            market_gate_reasons.append(fng_reason)

        # ── 6. Log current state ──
        if has_position:
            existing = self.positions[asset_key]
            pnl_pct = (
                (current_price - existing["entry_price"]) / existing["entry_price"]
            ) * 100
            print(
                f"  {asset_key}: {rsi_status} | POSITION @ {existing['entry_price']:.2f} | PnL: {pnl_pct:+.2f}%"
            )
        elif rsi is not None and rsi < effective_threshold:
            if market_blocked:
                print(
                    f"  {asset_key}: {rsi_status} < {effective_threshold} (⬇ oversold) → MARKET GATE BLOCKED: {'; '.join(market_gate_reasons)}"
                )
            elif hurst.get("block_entry", False):
                regime_label = hurst["regime"].replace("_", " ")
                print(
                    f"  {asset_key}: {rsi_status} < {effective_threshold} (⬇ oversold) → HURST REGIME BLOCKED: {regime_label} (H={hurst['hurst']:.4f})"
                )
            elif cusum.get("block_entry", False):
                regime_label = cusum["regime"].replace("_", " ")
                print(
                    f"  {asset_key}: {rsi_status} < {effective_threshold} (⬇ oversold) → CUSUM REGIME BLOCKED: {regime_label} ({cusum['up_breaks']}↑/{cusum['down_breaks']}↓)"
                )
            elif cooldown_remaining <= 0:
                print(
                    f"  {asset_key}: {rsi_status} < {effective_threshold} (⬇ oversold) → RSI MET, awaiting confidence score"
                )
            else:
                print(
                    f"  {asset_key}: {rsi_status} < {effective_threshold} (⬇ oversold) → signal ready, cooldown {cooldown_remaining}"
                )
        else:
            threshold_display = f"{effective_threshold}"
            if dynamic and dynamic.get("active"):
                threshold_display += f" (dynamic {dynamic['percentile']}th)"
            elif pct_rsi.get("active"):
                threshold_display += f" (pctile {pct_rsi['percentile']}th)"
            else:
                threshold_display += f" (fixed {threshold})"
            print(
                f"  {asset_key}: {rsi_status} ≥ {threshold_display} (waiting for oversold)"
            )

        # ── 6b. Track stale prices ──
        self._check_stale_prices(price_data, asset_key)

        # ── 6c. Trust-state + Monte Carlo DD status ──
        trust_label = self.trust_label
        if trust_label in ("low", "critical"):
            print(
                f"  {asset_key}: 🛡️ Trust-state={trust_label} (×{self.trust_multiplier:.2f})"
            )
        if self.mc_dd_threshold is not None:
            dd = self.portfolio_tracker.status().get("drawdown_pct", 0)
            print(
                f"  {asset_key}: 📉 MC DD threshold={self.mc_dd_threshold:.1f}% | current DD={dd:.1f}%"
            )

        # ── 7. Entry signal check (confidence score based) ──
        if not has_position:
            enough_cooldown = (
                self.cycles_since_last_trade.get(asset_key, 999) >= cooldown_cycles
            )

            # Trust-state (updated every cycle)
            trust = self._compute_trust_state(asset_key, strategy)
            mc_dd_allowed = True
            if self.mc_dd_threshold is not None:
                dd_val = self.portfolio_tracker.status().get("drawdown_pct", 0)
                mc_dd_allowed = dd_val < self.mc_dd_threshold

            # ── Safety gates (hard blocks — checked first) ──
            safety_skip_reason = None

            # Z-score cooling chamber (flash crash guard — blocks entries for 30 cycles)
            if self.zscore_cooldown.get(asset_key, 0) > 0:
                self.zscore_cooldown[asset_key] -= 1
                safety_skip_reason = (
                    f"zscore_cooldown_{self.zscore_cooldown[asset_key]}"
                )
            else:
                zs_blocked, zs_reason = self._check_zscore_flash(candles, asset_key)
                if zs_blocked:
                    self.zscore_cooldown[asset_key] = 30
                    safety_skip_reason = f"zscore: {zs_reason}"

            # BTC 1m vol surge check — global block on all entries
            if not safety_skip_reason and self.btc_vol_surge_blocked:
                safety_skip_reason = f"global: {self.btc_vol_surge_reason}"

            if not safety_skip_reason and not enough_cooldown:
                safety_skip_reason = f"cooldown {self.cycles_since_last_trade.get(asset_key, 999)}/{cooldown_cycles}"
            elif not mc_dd_allowed:
                dd_val = self.portfolio_tracker.status().get("drawdown_pct", 0)
                safety_skip_reason = (
                    f"mc_dd: {dd_val:.1f}% >= {self.mc_dd_threshold:.1f}%"
                )
            else:
                # Kill switches (consecutive losses, max positions, daily loss, stale prices)
                ks_allowed, ks_reason = self._kill_switch_allows_entry(
                    asset_key, strategy, candles
                )
                if not ks_allowed:
                    safety_skip_reason = f"kill_switch: {ks_reason}"
                else:
                    # Portfolio DD gate
                    portfolio_allowed, portfolio_reason = (
                        self.portfolio_tracker.allow_entry()
                    )
                    if not portfolio_allowed:
                        safety_skip_reason = f"portfolio_dd: {portfolio_reason}"
                    else:
                        # Correlation gate
                        if len(self.positions) > 1:
                            open_keys = [
                                k for k, v in self.positions.items() if v is not None
                            ]
                            if len(open_keys) >= 2 and asset_key not in open_keys:
                                corr_allowed, corr_reason = correlation_allows_entry(
                                    asset_key,
                                    self.correlations,
                                    self.positions,
                                    max_same_side_alt=2,
                                    correlation_threshold=0.70,
                                )
                                if not corr_allowed:
                                    safety_skip_reason = f"correlation: {corr_reason}"

                    # Hurst regime block: H > 0.55 = strictly disable MR
                    if not safety_skip_reason and hurst.get("block_entry", False):
                        regime_label = hurst.get("regime", "trending").replace("_", " ")
                        safety_skip_reason = (
                            f"hurst_regime: {regime_label} (H={hurst['hurst']:.4f})"
                        )

                    # Rolling win-rate gate (50-trade portfolio WR < 48% = halt)
                    if (
                        not safety_skip_reason
                        and len(self.portfolio_trade_results) >= 30
                    ):
                        recent_50 = self.portfolio_trade_results[-50:]
                        if len(recent_50) >= 30:
                            wr = sum(recent_50) / len(recent_50)
                            if wr < 0.48:
                                safety_skip_reason = f"wr_halt: portfolio WR {wr:.1%} < 48% ({len(recent_50)} trades)"

                    # ADX danger zone gate (15m ADX > 35 OR 1h ADX > 30 + price < EMA200)
                    if not safety_skip_reason and self.adx_danger_blocked.get(
                        asset_key, False
                    ):
                        safety_skip_reason = f"adx_danger: higher TF trend extreme"

                    # 1m vol sanity gate (annualized 1m vol > 3.0 = broken data)
                    if not safety_skip_reason:
                        vs = self._check_vol_sanity(asset_key)
                        if vs.get("active") and not vs["sane"]:
                            safety_skip_reason = f"vol_sanity: {vs['reason']}"

                    # Cross-sleeve net exposure: don't open MR if trend already has this asset
                    if not safety_skip_reason:
                        trend_pos = self.trend_positions.get(asset_key)
                        if trend_pos is not None:
                            print(
                                f"  {asset_key}: 🚫 Cross-sleeve block — trend position open for {asset_key}"
                            )
                            safety_skip_reason = "cross_sleeve: trend position open"

            if safety_skip_reason:
                self._log_skipped_setup(
                    asset_key,
                    current_price,
                    safety_skip_reason,
                    rsi,
                    effective_threshold,
                )
                if rsi is not None:
                    rsi_display = (
                        f"{rsi:.1f} < {effective_threshold}"
                        if rsi < effective_threshold
                        else f"{rsi:.1f} ≥ {effective_threshold}"
                    )
                    print(
                        f"  {asset_key}: {rsi_display} → SAFETY BLOCKED: {safety_skip_reason}"
                    )
                else:
                    print(
                        f"  {asset_key}: RSI=N/A → SAFETY BLOCKED: {safety_skip_reason}"
                    )
            else:
                # ── All safety gates passed → compute confidence score ──
                confidence = self._compute_confidence_score(
                    asset_key,
                    rsi,
                    effective_threshold,
                    candles,
                    strategy,
                    hurst,
                    hurst_mode,
                    btc_allowed,
                    fng_allowed,
                )
                cs = confidence["score"]
                decision = confidence["decision"]
                comp = confidence["components"]

                if decision == "full" or decision == "half":
                    # Log evaluator features for analysis (informational only — not a gate)
                    eval_allowed, eval_reason, eval_features = self._evaluate_entry(
                        asset_key, candles, strategy
                    )
                    eval_noted = not eval_allowed
                    if eval_noted:
                        self._log_evaluator_flag(
                            asset_key,
                            current_price,
                            eval_reason,
                            rsi,
                            effective_threshold,
                            eval_features,
                            score_override=True,
                        )
                    else:
                        self._log_evaluator_pass(
                            asset_key,
                            current_price,
                            rsi,
                            effective_threshold,
                            eval_features,
                        )

                    # Position sizing with VBPS (volatility-based)
                    position_size_r = self._calc_position_size(
                        asset_key, candles, strategy, hurst_mode
                    )
                    if decision == "half":
                        position_size_r *= 0.5

                    ctx = self._current_market_context()
                    signal_label = (
                        "rsi_oversold" if decision == "full" else "rsi_oversold_half"
                    )
                    confidence_score = (
                        confidence["score"] if decision in ("full", "half") else None
                    )
                    self._open_position(
                        asset_key,
                        current_price,
                        position_size_r,
                        signal_label,
                        price_data,
                        ctx,
                        confidence_score,
                    )

                    print(
                        f"  {asset_key}: RSI={rsi or 0:.1f} < {effective_threshold} -> ENTRY ({decision}, score={cs or 0:.3f}, rsi={comp.get("rsi",0):.2f} vol={comp.get("volume",0):.2f} reg={comp['regime']:.2f} adx={comp['adx']:.2f} fund={comp['funding']:.2f})"
                    )
                else:
                    self._log_skipped_setup(
                        asset_key,
                        current_price,
                        f"low_confidence:{cs:.3f}",
                        rsi,
                        effective_threshold,
                        confidence,
                    )
                    print(
                        f"  {asset_key}: RSI={rsi or 0:.1f} < {effective_threshold or 0} -> LOW CONFIDENCE (score={cs or 0:.3f}, below 0.55, comp: rsi={comp.get('rsi',0):.2f} vol={comp.get('volume',0):.2f} reg={comp.get('regime',0):.2f} fund={comp.get('funding', 0):.2f})"
                    )

        # ── 8. Exit check for existing position ──
        existing = self.positions.get(asset_key)
        if existing and current_price > 0:
            # Skip exit checks on same cycle as entry (prevents same-candle TP/SL
            # where TP1 can trigger on the entry candle before price moves).
            if asset_key in self._just_entered:
                self._just_entered.discard(asset_key)
            else:
                entry_price = existing["entry_price"]
                pnl_pct = ((current_price - entry_price) / entry_price) * 100
                stop_loss_pct = strategy.get("stop_loss_pct", 2.0)

                # Dynamic ATR-based stop loss (overrides static SL when data available)
                if len(candles) >= 15:
                    atr_pct = self._calc_atr(candles, period=14)
                    if atr_pct is not None and atr_pct > 0:
                        sl_mult = strategy.get("atr_sl_mult_alt", 3.0)
                        if "BTC" in asset_key or "ETH" in asset_key:
                            sl_mult = strategy.get("atr_sl_mult_major", 2.0)
                        atr_sl_pct = atr_pct * sl_mult * 100
                        # Clamp to sane range
                        sl_floor = strategy.get("atr_sl_floor_pct", 1.0)
                        sl_ceiling = strategy.get("atr_sl_ceiling_pct", 10.0)
                        stop_loss_pct = min(max(atr_sl_pct, sl_floor), sl_ceiling)

                # ── 8a. Hard stop loss — always active, full position ──
                if pnl_pct < -stop_loss_pct:
                    ctx = self._current_market_context()
                    self._close_position(
                        asset_key, current_price, pnl_pct, "stop_loss", price_data, ctx
                    )

                # ── 8b. Time-based exit — close if held beyond max cycles ──
                elif self._check_time_exit(asset_key, existing, current_price, strategy):
                    ctx = self._current_market_context()
                    pnl_exit = ((current_price - entry_price) / entry_price) * 100
                    self._close_position(
                        asset_key, current_price, pnl_exit, "time_exit", price_data, ctx
                    )
                    print(f"  {asset_key}: ⏰ TIME EXIT @ {current_price:.4f}")

                else:
                    # ── 8c. Update chandelier high-water mark ──
                    if existing.get("tp2_hit", False):
                        existing["runner_high"] = max(
                            existing.get("runner_high", current_price), current_price
                        )
                    else:
                        existing["chandelier_high"] = max(
                            existing.get("chandelier_high", entry_price), current_price
                        )

                    # ── 8d. Scale-out TP1 (50% at 20 EMA reversion, min profit required) ──
                    if not existing.get("scaled_out", False) and len(candles) >= 22:
                        ema_value = self._calc_ema_value(
                            [c["close"] for c in candles], period=20
                        )
                        prev_close = candles[-2]["close"]
                        scale_out_min_R = strategy.get("scale_out_min_R", 0.3)
                        min_profit_for_tp1 = scale_out_min_R * abs(stop_loss_pct)
                        if (
                            ema_value is not None
                            and prev_close < ema_value
                            and current_price >= ema_value
                            and pnl_pct >= min_profit_for_tp1
                        ):
                            ctx = self._current_market_context()
                            self._close_position(
                                asset_key,
                                current_price,
                                pnl_pct,
                                "scale_out_tp1",
                                price_data,
                                ctx,
                                partial=True,
                                slice_pct=0.5,
                            )
                            print(
                                f"  {asset_key}: ✅ SCALE OUT 50% @ {current_price:.4f} (EMA reversion)"
                            )

                    # ── 8e. Chandelier trailing stop on remaining position ──
                    if existing.get("scaled_out", False):
                        ch_mult = strategy.get("chandelier_mult_alts", 4.0)
                        if "BTC" in asset_key or "ETH" in asset_key:
                            ch_mult = strategy.get("chandelier_mult_major", 2.5)
                        trail_high = (
                            existing.get("runner_high")
                            if existing.get("tp2_hit", False) and existing.get("runner_high")
                            else existing["chandelier_high"]
                        )
                        chandelier = self._calc_chandelier_exit(
                            candles, trail_high, ch_mult
                        )
                        if chandelier is not None and current_price < chandelier:
                            ctx = self._current_market_context()
                            remaining_pnl = (
                                (current_price - entry_price) / entry_price
                            ) * 100
                            self._close_position(
                                asset_key,
                                current_price,
                                remaining_pnl,
                                "chandelier_exit",
                                price_data,
                                ctx,
                            )
                            print(
                                f"  {asset_key}: 🔚 CHANDELIER EXIT @ {current_price:.4f} (trailed from {existing.get('runner_high', existing['chandelier_high']):.4f})"
                            )

                    # ── 8f. TP2 — take another slice at target R ──
                    if existing.get("scaled_out", False) and not existing.get("tp2_hit", False):
                        tp2_target_R = strategy.get("tp2_target_R", 1.5)
                        tp2_slice = strategy.get("tp2_slice", 0.5)
                        tp2_price_target = tp2_target_R * abs(stop_loss_pct)
                        if pnl_pct >= tp2_price_target:
                            ctx = self._current_market_context()
                            self._close_position(
                                asset_key,
                                current_price,
                                pnl_pct,
                                "tp2",
                                price_data,
                                ctx,
                                partial=True,
                                slice_pct=tp2_slice,
                            )
                            print(
                                f"  {asset_key}: 🎯 TP2 HIT @ {current_price:.4f} (pnl={pnl_pct:+.2f}%, target={tp2_target_R}R)"
                            )

        # ── 8e. Check optimizer readiness (dormant until 200+ trades) ──
        self._check_optimizer_ready(asset_key)

        # ── 10. Trend-Following Sleeve (Phase 3 — BTC/ETH/SOL only) ──
        if asset_key in self.trend_universe:
            await self._manage_trend_sleeve(
                asset_key, current_price, strategy, price_data, hurst
            )

        # ── 9. Increment cooldown counter ──
        self.cycles_since_last_trade[asset_key] = (
            self.cycles_since_last_trade.get(asset_key, 0) + 1
        )

    def _calc_rsi(self, closes: List[float], period: int = 14) -> Optional[float]:
        """Calculate RSI from a list of close prices."""
        if len(closes) < period + 1:
            return None

        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [d if d > 0 else 0 for d in deltas[-period:]]
        losses = [-d if d < 0 else 0 for d in deltas[-period:]]

        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def _compute_dynamic_rsi(self, asset_key: str, strategy: dict) -> dict:
        """Phase 3: Dynamic RSI percentile from 1m bar store.

        Returns dict with:
          active (bool)         — True if dynamic threshold is active
          threshold (float|None) — the computed entry threshold
          exit_threshold        — the exit percentile (for future use)
          reason (str)          — status for logging
        Falls back to inactive with reason when insufficient data.
        """
        config = strategy.get("dynamic_rsi", {})
        result = compute_dynamic_rsi_threshold(asset_key, config)
        return result

    def _compute_rsi_percentile_threshold(self, asset_key: str) -> dict:
        """Per-asset percentile RSI threshold (q05 majors / q10 alts).

        Wraps compute_rsi_percentile_threshold from adaptive module.
        Uses asset type to automatically select 5th or 10th percentile.
        """
        return compute_rsi_percentile_threshold(asset_key)

    def _check_vol_sanity(self, asset_key: str) -> dict:
        """Check annualized 1m realized vol ≤ 3.0.

        Wraps check_vol_sanity from adaptive module.
        Blocks entries when data feed appears broken/corrupted.
        """
        return check_vol_sanity(asset_key)

    def _compute_hurst(self, asset_key: str, strategy: dict) -> dict:
        """Phase 3b: Hurst exponent regime classification from 1m bar store.

        Returns dict with:
          active       — bool, True if Hurst was computed (sufficient data)
          hurst        — float or None
          regime       — str: strongly_mean_reverting / mean_reverting /
                         random_walk / trending / strongly_trending
          block_entry  — bool, whether to block MR entries
          reason       — str, status message
        Falls back to inactive with insufficient data.
        """
        config = strategy.get("hurst", {})
        result = compute_hurst_exponent(asset_key, config)
        return result

    def _compute_cusum(self, asset_key: str, strategy: dict) -> dict:
        """Phase 3c: CUSUM regime detection from 1m bar store.

        Returns dict with:
          active       — bool, True if CUSUM was computed (sufficient data)
          regime       — str: normal / shifting_down / shifting_up / unstable
          up_breaks    — int, upward CUSUM break count
          down_breaks  — int, downward CUSUM break count
          total_breaks — int
          block_entry  — bool, whether to block MR entries
          reason       — str, status message
        Falls back to inactive with insufficient data.
        """
        config = strategy.get("cusum", {})
        result = compute_cusum_regime(asset_key, config)
        return result

    # ── Confidence Score (replaces linear gate chain) ──

    def _compute_confidence_score(
        self,
        asset_key: str,
        rsi: Optional[float],
        effective_threshold: float,
        candles: list,
        strategy: dict,
        hurst: dict,
        hurst_mode: str,
        btc_allowed: bool,
        fng_allowed: bool,
    ) -> dict:
        """Compute a weighted confidence score for entry decisions.

        Components (each 0-1):
          0.22 * RSI_score        — how well RSI aligns with strategy mode
          0.18 * volume_score     — volume confirmation
          0.14 * lower_low_score  — absence of lower-low cascade
          0.11 * candle_pos_score  — positioning within candle range
          0.12 * regime_score     — Hurst/CUSUM regime alignment
          0.13 * adx_score        — ADX favors mean-reversion (low = good)
          0.10 * funding_score    — negative funding = crowded short = squeeze potential

        Returns:
          {"score": 0.0-1.0, "components": {...}, "decision": "full"/"half"/"none"}
        """
        # Default weights
        w_rsi = 0.22
        w_vol = 0.18
        w_ll = 0.14
        w_pos = 0.11
        w_reg = 0.12
        w_adx = 0.13
        w_funding = 0.10

        # ── 1. RSI Score ──
        rsi_score = 0.0
        if rsi is not None:
            if hurst_mode == "trend":
                # Trend mode: high RSI = momentum strength
                rsi_score = min(1.0, max(0.0, (rsi - 55) / 20))
            else:
                # MR mode: distance-decay from extreme threshold up to RSI 35
                # Gives partial credit for near-extreme RSI values (e.g., RSI 23
                # with threshold 8.9 scores ~0.46 instead of 0.0). Previously used
                # (threshold - rsi) / 10 which hard-zeroed all realistic candidates.
                soft_ceiling = 35.0
                if rsi <= effective_threshold:
                    rsi_score = 1.0
                elif rsi >= soft_ceiling:
                    rsi_score = 0.0
                else:
                    rsi_score = 1.0 - ((rsi - effective_threshold) / (soft_ceiling - effective_threshold))

        # ── 2. Volume Score ──
        volume_score = 0.5  # default neutral when data unavailable
        volume_available = False
        if len(candles) >= 2:
            volumes = [c.get("volume", 0) or 0 for c in candles]
            recent_volumes = volumes[-11:] if len(volumes) >= 11 else volumes
            # yfinance doesn't provide crypto volume (all zeros) — detect this
            has_real_volume = any(
                v > 0 for v in recent_volumes[:-1] if v > 0
            )  # check prior candles
            if has_real_volume and volumes[-1] > 0:
                avg_vol = sum(recent_volumes[:-1]) / max(len(recent_volumes) - 1, 1)
                if avg_vol > 0:
                    vol_ratio = volumes[-1] / avg_vol
                    # Wider dynamic range: 0.0 at 0.5x avg volume, 1.0 at 2.0x avg.
                    # Previously clamped to ~0.5 range (vol_ratio / 2.0), making
                    # volume a near-constant half-credit with no discriminative power.
                    volume_score = max(0.0, min(1.0, (vol_ratio - 0.5) / 1.5))
                    volume_available = True

        # ── 3. Lower-Low Score (absence of cascade = good) ──
        lower_low_score = 1.0
        evaluator = strategy.get("evaluator", {})
        if evaluator.get("enabled", True) and len(candles) >= 4:
            cascade_check = evaluator.get("lower_low_cascade", 3)
            lows = [c["low"] for c in candles]
            if len(lows) >= cascade_check + 1:
                recent_lows = lows[-(cascade_check + 1) :]
                is_cascading = all(
                    recent_lows[i] > recent_lows[i + 1] for i in range(cascade_check)
                )
                if is_cascading:
                    lower_low_score = 0.0

        # ── 4. Candle Position Score ──
        candle_pos_score = 0.5  # default neutral
        if len(candles) >= 1:
            last = candles[-1]
            candle_range = last["high"] - last["low"]
            if candle_range > 0:
                pos = (last["close"] - last["low"]) / candle_range
                # Lower position = better for MR (price at bottom of range)
                candle_pos_score = max(
                    0.0, 1.0 - pos * 1.5
                )  # 0.0 pos→1.0, 0.5 pos→0.25, 0.67+→0.0

        # ── 5. Regime Score ──
        # Measures how suitable the current market regime is for MR strategy.
        # Calibrated for wider dynamic range: strong regimes score high,
        # unfavorable regimes score low, random_walk is neutral.
        regime_score = 0.5  # default neutral
        if hurst_mode == "trend":
            regime_score = 0.85 if hurst.get("active") else 0.50
        elif hurst_mode == "mean_reversion":
            regime_score = 0.95 if hurst.get("active") else 0.70
        elif hurst_mode == "random_walk":
            regime_score = 0.55  # neutral — no clear edge either way

        # ── 6. ADX Score (low ADX = good for mean reversion) ──
        adx_score = 0.5
        if len(candles) >= 28:
            tf = strategy.get("trend_filter", {})
            if tf.get("enabled", True):
                try:
                    adx = self._calc_adx(candles, period=tf.get("adx_period", 14))
                    if adx is not None:
                        adx_score = max(0.0, 1.0 - adx / 30)
                except Exception:
                    pass

        # ── 7. Funding Rate Score (FR_1h/0.001 per research spec) ──
        # Negative funding (shorts paying longs) = crowded short = squeeze potential
        # FR_1h / 0.001 identifies retail imbalances, capped at ±1.0
        # -0.1% per 8h (-109.5% APY) = maximum bullish signal
        funding_score = 0.0  # default neutral
        hl_assets = self.hl_context.get("assets", {})
        symbol = asset_key.split("_")[0]
        if symbol in hl_assets:
            fr_1h = hl_assets[symbol].get("funding_rate", 0)  # raw per-hour funding
            if fr_1h is not None:
                raw_score = (
                    fr_1h * 8
                ) / 0.001  # ×8: -0.1% per 8h maps to -1.0 (max signal per spec)
                raw_score = max(-1.0, min(1.0, raw_score))  # cap at ±1.0
                # Negative = bullish for MR longs → positive 0-1 score
                funding_score = max(0.0, -raw_score)

        # Market condition penalty
        market_penalty = 0.0
        if not btc_allowed:
            market_penalty += 0.15
        if not fng_allowed:
            market_penalty += 0.10

        # Compute weighted score
        components = {
            "rsi": round(rsi_score, 3),
            "volume": round(volume_score, 3),
            "lower_low": round(lower_low_score, 3),
            "candle_pos": round(candle_pos_score, 3),
            "regime": round(regime_score, 3),
            "adx": round(adx_score, 3),
            "funding": round(funding_score, 3),
        }
        raw_score = (
            w_rsi * rsi_score
            + w_vol * volume_score
            + w_ll * lower_low_score
            + w_pos * candle_pos_score
            + w_reg * regime_score
            + w_adx * adx_score
            + w_funding * funding_score
        )
        final_score = max(0.0, min(1.0, raw_score - market_penalty))

        # Decision (calibrated from 254-rejection-sample: max=0.614, 95th~0.50)
        if final_score >= 0.60:
            decision = "full"
        elif final_score >= 0.55:
            decision = "half"
        else:
            decision = "none"

        return {
            "score": round(final_score, 4),
            "components": components,
            "decision": decision,
            "market_penalty": round(market_penalty, 2),
        }

    # ── Skipped-setup logging ──

    def _log_skipped_setup(
        self,
        asset_key: str,
        price: float,
        reason: str,
        rsi: Optional[float],
        threshold: float,
        confidence: Optional[dict] = None,
    ):
        """Log every cycle where the bot chose not to enter.

        Writes to setups_log.jsonl per asset — the full picture of
        why setups are being skipped, not just rejections.
        """
        entry = {
            "asset": asset_key,
            "price": round(price, 6),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "rsi": round(rsi, 1) if rsi is not None else None,
            "threshold": threshold,
            "decision": "skipped",
            "btc_1h_rsi": self.btc_context.get("btc_1h_rsi"),
            "btc_4h_rsi": self.btc_context.get("btc_4h_rsi"),
            "fear_greed": self._fng_value(),
        }
        if confidence:
            entry["confidence_score"] = confidence.get("score")
            entry["confidence_components"] = confidence.get("components")
            entry["confidence_decision"] = confidence.get("decision")
            entry["market_penalty"] = confidence.get("market_penalty")

        log_file = self.state_dir / asset_key / "setups_log.jsonl"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _open_position(
        self,
        asset_key: str,
        price: float,
        size_r: float,
        signal: str,
        price_data: dict,
        market_ctx: dict,
        confidence_score: Optional[float] = None,
    ):
        """Open a paper position."""
        position = {
            "asset": asset_key,
            "entry_price": price,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "position_size_r": size_r,
            "signal": signal,
            "direction": "long",
            "entry_source": price_data.get("source", "unknown"),
            "market_context": market_ctx,
            "confidence_score": confidence_score,
            "scaled_out": False,  # TP1 (50% at EMA reversion) taken?
            "tp2_hit": False,  # TP2 (25% at target R) taken?
            "runner_high": None,  # highest price since TP2 (resets chandelier anchor)
            "chandelier_high": price,  # highest price since entry (for trailing)
        }
        self.positions[asset_key] = position
        self._just_entered.add(asset_key)
        print(
            f"📈 {asset_key}: OPEN at {price:.6f} | signal={signal} | size_r={size_r}"
        )

    def _close_position(
        self,
        asset_key: str,
        price: float,
        pnl_pct: float,
        reason: str,
        price_data: dict,
        market_ctx: dict = None,
        partial: bool = False,
        slice_pct: float = None,
    ):
        """Close a paper position and log the trade.

        If partial=True, closes `slice_pct` (default 50%) of position and keeps remaining open.
        Otherwise closes the full position.
        """
        position = self.positions.get(asset_key)
        if not position:
            return

        # Merge entry market context with exit context
        entry_ctx = position.get("market_context", {})
        exit_ctx = market_ctx or self._current_market_context()
        full_ctx = {**entry_ctx, **{"exit_" + k: v for k, v in exit_ctx.items()}}

        slice_ratio = slice_pct if slice_pct is not None else (0.5 if partial else 1.0)
        close_size = position["position_size_r"] * slice_ratio

        # ── Paper fidelity: Fee + Funding PnL adjustments ──
        TAKER_FEE_RATE = 0.00025  # Hyperliquid standard taker fee (0.025%)
        pos_value = close_size * self.paper_balance
        entry_fee = pos_value * TAKER_FEE_RATE
        exit_fee = pos_value * (1 + pnl_pct / 100) * TAKER_FEE_RATE
        fee_dollars = entry_fee + exit_fee

        symbol = asset_key.split("_")[0]
        hl_assets = self.hl_context.get("assets", {})
        funding_rate = hl_assets.get(symbol, {}).get("funding_rate", 0.0)
        entry_dt = datetime.fromisoformat(position["entry_time"])
        hours_held = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
        # Long position: positive funding = shorts pay longs → we receive → positive PnL
        # Actually: Hyperliquid — positive funding rate means LONGS PAY SHORTS.
        # So positive funding_rate = cost to longs = negative PnL for our long positions
        funding_dollars = -funding_rate * hours_held * pos_value

        gross_pnl_dollars = pnl_pct / 100 * pos_value
        net_pnl_dollars = gross_pnl_dollars - fee_dollars + funding_dollars

        trade = {
            "asset": asset_key,
            "entry_price": position["entry_price"],
            "exit_price": price,
            "entry_time": position["entry_time"],
            "exit_time": datetime.now(timezone.utc).isoformat(),
            "pnl_pct": round(pnl_pct, 4),
            "fee_pct": round(fee_dollars / self.paper_balance * 100, 4),
            "funding_pct": round(funding_dollars / self.paper_balance * 100, 4),
            "net_pnl_pct": round(net_pnl_dollars / self.paper_balance * 100, 4),
            "hours_held": round(hours_held, 2),
            "funding_rate_1h": round(funding_rate, 6),
            "signal": position["signal"],
            "direction": position["direction"],
            "exit_reason": reason,
            "position_size_r": close_size,
            "regime": self._classify_regime(price_data),
            "entry_source": position.get("entry_source"),
            "enter_confidence": position.get("confidence_score"),
            "btc_entry_1h_rsi": entry_ctx.get("btc_1h_rsi"),
            "btc_entry_4h_rsi": entry_ctx.get("btc_4h_rsi"),
            "btc_exit_1h_rsi": exit_ctx.get("btc_1h_rsi"),
            "btc_exit_4h_rsi": exit_ctx.get("btc_4h_rsi"),
            "fear_greed_entry": entry_ctx.get("fear_greed_value"),
            "fear_greed_exit": exit_ctx.get("fear_greed_value"),
            "market_regime_entry": entry_ctx.get("fear_greed_class"),
        }

        if partial:
            position["position_size_r"] = position["position_size_r"] * (1 - slice_ratio)
            if reason == "scale_out_tp1":
                position["scaled_out"] = True
            elif reason == "tp2":
                position["tp2_hit"] = True
                position["runner_high"] = price  # Reset chandelier anchor from TP2 spike
        else:
            self.positions[asset_key] = None

        # Log to trades.jsonl
        trades_file = self.state_dir / asset_key / "trades.jsonl"
        trades_file.parent.mkdir(parents=True, exist_ok=True)
        with open(trades_file, "a") as f:
            f.write(json.dumps(trade) + "\n")

        if not partial:
            # Reset cooldown counter
            self.cycles_since_last_trade[asset_key] = 0
            # Increment reflection counter
            self.trade_count_since_reflection[asset_key] += 1

        label = "PARTIAL" if partial else "CLOSE"
        print(
            f"📉 {asset_key}: {label} at {price:.6f} | PnL: {pnl_pct:+.2f}% | reason={reason}"
        )

        # ── Track daily PnL (Step 5: Kill Switches) ──
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.last_daily_reset:
            self.daily_pnl_pct = 0.0
            self.last_daily_reset = today
        self.daily_pnl_pct = (self.daily_pnl_pct or 0) + pnl_pct * close_size

        # ── Track consecutive losses (full closes only) ──
        if not partial:
            if pnl_pct < 0:
                self.consecutive_losses[asset_key] = (
                    self.consecutive_losses.get(asset_key, 0) + 1
                )
                # P3: Auto-pause check
                strategy = self._load_strategy(asset_key)
                ks = strategy.get("kill_switches", {})
                max_cl = ks.get("max_consecutive_losses", 5)
                if (
                    self.consecutive_losses[asset_key] >= max_cl
                    and asset_key not in self.paused_assets
                ):
                    self.paused_assets[asset_key] = datetime.now(
                        timezone.utc
                    ).isoformat()
                    print(
                        f"🔴 {asset_key}: AUTO-PAUSED after {self.consecutive_losses[asset_key]} consecutive losses"
                    )
            else:
                # Winning trade — reset streak and unpause
                was_paused = asset_key in self.paused_assets
                self.consecutive_losses[asset_key] = 0
                self.paused_assets.pop(asset_key, None)
                if was_paused:
                    print(f"🟢 {asset_key}: Unpaused after winning trade")

            # ── Rolling win-rate tracking (20 per asset, 50 portfolio) ──
            is_win = pnl_pct > 0
            self.asset_trade_results.setdefault(asset_key, []).append(is_win)
            self.portfolio_trade_results.append(is_win)
            # Keep rolling windows bounded
            if len(self.asset_trade_results[asset_key]) > 50:
                self.asset_trade_results[asset_key] = self.asset_trade_results[
                    asset_key
                ][-50:]
            if len(self.portfolio_trade_results) > 100:
                self.portfolio_trade_results = self.portfolio_trade_results[-100:]

        # ── Phase 6: Update portfolio tracker & paper balance ──
        # Use net PnL (gross price PnL - fees + funding accrual)
        net_account_pnl_pct = net_pnl_dollars / self.paper_balance * 100
        self.portfolio_tracker.update([{"pnl_pct": net_account_pnl_pct}])

        # Track paper balance with net PnL
        self.paper_balance += net_pnl_dollars
        print(
            f"💰 {asset_key}: Paper balance ${self.paper_balance:.2f} "
            f"(price=${gross_pnl_dollars:+.2f} fee=${fee_dollars:+.2f} "
            f"funding=${funding_dollars:+.2f} net=${net_pnl_dollars:+.2f})"
        )

    # ── Step 4: ADX Trend Filter ──────────────────────────────────────────

    def _calc_adx(self, candles: list, period: int = 14) -> Optional[float]:
        """Calculate ADX from candle data. ADX<20=weak, 20-30=moderate, >30=strong."""
        if len(candles) < period * 2 + 1:
            return None

        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        closes = [c["close"] for c in candles]

        tr_list, plus_dm_list, minus_dm_list = [], [], []
        for i in range(1, len(candles)):
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - closes[i - 1])
            lc = abs(lows[i] - closes[i - 1])
            tr_list.append(max(hl, hc, lc))

            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]
            if up_move > down_move and up_move > 0:
                plus_dm_list.append(up_move)
            else:
                plus_dm_list.append(0)
            if down_move > up_move and down_move > 0:
                minus_dm_list.append(down_move)
            else:
                minus_dm_list.append(0)

        if len(tr_list) < period * 2:
            return None

        # Wilder's smoothing
        tr_s = [sum(tr_list[:period]) / period]
        pd_s = [sum(plus_dm_list[:period]) / period]
        md_s = [sum(minus_dm_list[:period]) / period]
        for i in range(period, len(tr_list)):
            tr_s.append(tr_s[-1] * (period - 1) / period + tr_list[i] / period)
            pd_s.append(pd_s[-1] * (period - 1) / period + plus_dm_list[i] / period)
            md_s.append(md_s[-1] * (period - 1) / period + minus_dm_list[i] / period)

        plus_di = [100 * p / t if t > 0 else 0 for p, t in zip(pd_s, tr_s)]
        minus_di = [100 * m / t if t > 0 else 0 for m, t in zip(md_s, tr_s)]

        dx_list = []
        for p, m in zip(plus_di, minus_di):
            s = p + m
            dx_list.append(100 * abs(p - m) / s if s > 0 else 0)

        if len(dx_list) < period:
            return None
        return round(sum(dx_list[-period:]) / period, 2)

    def _calc_ema_slope(
        self, closes: list, period: int = 20, slope_bars: int = 3
    ) -> Optional[float]:
        """EMA slope as fraction of price per bar. Positive = uptrend."""
        if len(closes) < period + slope_bars:
            return None
        m = 2 / (period + 1)
        ema = [sum(closes[:period]) / period]
        for i in range(period, len(closes)):
            ema.append(closes[i] * m + ema[-1] * (1 - m))
        if len(ema) < slope_bars + 1:
            return None
        slope = (ema[-1] - ema[-(slope_bars + 1)]) / ema[-(slope_bars + 1)] / slope_bars
        return round(slope, 6)

    def _calc_atr(self, candles: list, period: int = 14) -> Optional[float]:
        """ATR as fraction of current price."""
        if len(candles) < period + 1:
            return None
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        closes = [c["close"] for c in candles]
        tr_list = []
        for i in range(1, len(candles)):
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - closes[i - 1])
            lc = abs(lows[i] - closes[i - 1])
            tr_list.append(max(hl, hc, lc))
        if len(tr_list) < period:
            return None
        atr = sum(tr_list[:period]) / period
        for i in range(period, len(tr_list)):
            atr = (atr * (period - 1) + tr_list[i]) / period
        cp = closes[-1]
        return round(atr / cp, 6) if cp > 0 else None

    def _trend_filter_allows_entry(self, candles: list, strategy: dict) -> tuple:
        """Tiered ADX trend filter. Returns (allowed, reason, features)."""
        tf = strategy.get("trend_filter", {})
        features = {"adx": None, "ema_slope": None, "atr": None}
        if not tf.get("enabled", True):
            return (True, "trend_filter_disabled", features)

        closes = [c["close"] for c in candles]
        adx = self._calc_adx(candles, period=tf.get("adx_period", 14))
        features["adx"] = adx
        if adx is None:
            return (True, "insufficient_data_adx", features)

        strong = tf.get("adx_threshold_strong", 30)
        moderate = tf.get("adx_threshold_moderate", 20)

        if adx >= strong:
            return (False, f"ADX {adx:.1f} >= {strong} (strong trend)", features)

        if adx >= moderate:
            ep = tf.get("ema_period", 20)
            sb = tf.get("ema_slope_period", 3)
            mx = tf.get("max_slope_for_mr", 0.001)
            slope = self._calc_ema_slope(closes, period=ep, slope_bars=sb)
            features["ema_slope"] = slope
            if slope is None:
                return (True, "insufficient_data_slope", features)
            if abs(slope) > mx:
                return (
                    False,
                    f"EMA slope {slope:.6f} > {mx} (moderate trend)",
                    features,
                )

        features["atr"] = self._calc_atr(candles, period=tf.get("atr_period", 14))
        return (True, f"ADX {adx:.1f} trend OK", features)

    # ── Step 5: Kill Switches ─────────────────────────────────────────────

    def _update_btc_correlations(self, price_data: dict):
        """Update rolling 1h return correlation between BTC and each asset.

        Samples 1h returns once per ~60 cycles (~1h), stores rolling 24-entry
        logs, and computes Pearson correlation. Used by sector cap in kill switch.
        """
        import time
        now = time.time()
        # Update at most once per 50 cycles (~50 min) to avoid redundant compute
        if now - self._last_corr_update < 3000:  # 50 min cooldown
            return
        self._last_corr_update = now

        btc_price = self.btc_context.get("btc_price", 0)
        if btc_price <= 0:
            return

        # Get current price for each asset from price_data
        cur_prices = {}
        if price_data:
            cur_prices["BTC_USDT"] = btc_price
            for asset_key in self.assets:
                k = asset_key["key"]
                # Try to get from stored context
                if k == "BTC_USDT":
                    continue
                cur_prices[k] = btc_price * 0.999  # fallback — will be updated by cycle

        # Compute this hour's return for BTC
        if len(self._btc_hourly_return_log) > 0:
            prev_btc = self._last_btc_price_for_corr
            if prev_btc and prev_btc > 0:
                btc_return = (btc_price - prev_btc) / prev_btc
                self._btc_hourly_return_log.append(btc_return)
                if len(self._btc_hourly_return_log) > 24:
                    self._btc_hourly_return_log.pop(0)

                # Compute returns for each asset using same 1h window
                for asset_key in self.assets:
                    k = asset_key["key"]
                    if k == "BTC_USDT":
                        continue
                    # Use the current cycle data from positions or fallback price
                    pos = self.positions.get(k)
                    cur = (pos.get("current_price", 0) if pos else 0)
                    if cur <= 0:
                        continue
                    prev = self._last_asset_prices_for_corr.get(k)
                    if prev and prev > 0:
                        asset_return = (cur - prev) / prev
                        log = self._asset_hourly_return_logs[k]
                        log.append(asset_return)
                        if len(log) > 24:
                            log.pop(0)

        self._last_btc_price_for_corr = btc_price
        self._last_asset_prices_for_corr = cur_prices

        # Compute Pearson correlation for each asset with >= 12 data points
        btc_log = self._btc_hourly_return_log
        if len(btc_log) >= 12:
            import statistics
            btc_mean = statistics.mean(btc_log)
            btc_stdev = statistics.stdev(btc_log) if len(btc_log) > 1 else 1
            for asset_key in self.assets:
                k = asset_key["key"]
                if k == "BTC_USDT":
                    self._btc_correlations[k] = 1.0
                    continue
                log = self._asset_hourly_return_logs[k]
                if len(log) >= 12 and len(log) == len(btc_log):
                    # Trim to same length
                    n = min(len(log), len(btc_log))
                    a = log[-n:]
                    b = btc_log[-n:]
                    try:
                        a_mean = statistics.mean(a)
                        b_mean = statistics.mean(b)
                        num = sum((x - a_mean) * (y - b_mean) for x, y in zip(a, b))
                        den = (sum((x - a_mean) ** 2 for x in a) ** 0.5
                               * sum((y - b_mean) ** 2 for y in b) ** 0.5)
                        r = num / den if den != 0 else 0
                        self._btc_correlations[k] = round(r, 4)
                    except (statistics.StatisticsError, ZeroDivisionError):
                        pass

    def _correlation_sector_allows_entry(self, asset_key: str) -> tuple:
        """Sector cap: max 2 open positions sharing BTC-beta > 0.7.
        
        Returns (allowed: bool, reason: str).
        """
        asset_r = self._btc_correlations.get(asset_key, 0)
        if asset_r < 0.7:
            return (True, "")  # Low-BTC-beta asset, no cap applied

        # Count open positions with high BTC correlation
        high_beta_count = 0
        for k, pos in self.positions.items():
            if pos is not None and k != asset_key:
                pos_r = self._btc_correlations.get(k, 0)
                if pos_r >= 0.7:
                    high_beta_count += 1

        if high_beta_count >= 2:
            return (
                False,
                f"SECTOR CAP: {asset_key} (β={asset_r:.2f}) + {high_beta_count} high-β open ≥ 2",
            )
        return (True, "")

    def _kill_switch_allows_entry(
        self, asset_key: str, strategy: dict, candles: list = None
    ) -> tuple:
        """Check kill switches before entry. Returns (allowed, reason)."""
        ks = strategy.get("kill_switches", {})
        if not ks.get("enabled", True):
            return (True, "")

        max_pos = ks.get("max_open_positions", 3)
        open_cnt = sum(1 for p in self.positions.values() if p is not None)
        if open_cnt >= max_pos:
            return (False, f"KILL: {open_cnt} open >= max {max_pos}")

        max_loss = ks.get("max_daily_loss_pct", 2.5)
        if self.daily_pnl_pct is not None and self.daily_pnl_pct <= -max_loss:
            return (
                False,
                f"KILL: daily PnL {self.daily_pnl_pct:+.2f}% <= -{max_loss}%",
            )

        max_stale = ks.get("stale_price_cycles", 5)
        stale_this = self.stale_price_cycles.get(asset_key, 0)
        if stale_this >= max_stale:
            return (False, f"KILL: stale price {stale_this} cycles")

        # P3: Consecutive loss pause
        max_cl = ks.get("max_consecutive_losses", 5)
        cl = self.consecutive_losses.get(asset_key, 0)
        if cl >= max_cl:
            return (
                False,
                f"KILL: {cl} consecutive losses (max {max_cl}) — asset paused",
            )

        # Microstructure volume filter: exclude assets with < $1M 24h volume (paper account)
        if self.hl_context.get("available"):
            symbol = asset_key.split("_")[0]
            hl_asset = self.hl_context.get("assets", {}).get(symbol)
            if hl_asset:
                vol_24h = hl_asset.get("volume_24h", 0)
                if vol_24h is not None and vol_24h < 1_000_000:
                    return (False, f"KILL: {symbol} vol ${vol_24h / 1e6:.1f}M < $1M")

        # OI Velocity gate: > 15% OI expansion in ~48h = institutional trend-building
        if len(self.oi_snapshots) > 0:
            symbol = asset_key.split("_")[0]
            snaps = self.oi_snapshots.get(symbol, [])
            if len(snaps) >= 48:
                earliest_oi = snaps[0]
                if earliest_oi > 0 and snaps[-1] > earliest_oi * 1.15:
                    return (
                        False,
                        f"KILL: OI {snaps[-1] / earliest_oi:.2f}x in ~48h (> 15%)",
                    )

        # Bid-ask spread proxy: avg (high-low)/close over last 5 candles
        # ≤ 0.15% = tight execution spreads (raised from 0.08% for volatile alt entries)
        if candles is not None and len(candles) >= 5:
            spreads = []
            for c in candles[-5:]:
                if c.get("close", 0) > 0:
                    spread_pct = (c["high"] - c["low"]) / c["close"]
                    spreads.append(spread_pct)
            if spreads and sum(spreads) / len(spreads) > 0.0015:
                avg_spread = sum(spreads) / len(spreads) * 100
                return (
                    False,
                    f"KILL: {asset_key.split('_')[0]} spread {avg_spread:.4f}% > 0.15%",
                )

        # BTC correlation sector cap: max 2 high-β positions concurrently
        sector_allowed, sector_reason = self._correlation_sector_allows_entry(asset_key)
        if not sector_allowed:
            return (False, f"KILL: {sector_reason}")

        return (True, "")

    def _check_stale_prices(self, price_data: dict, asset_key: str):
        """Track consecutive cycles with unchanged price."""
        cur = price_data.get("current_price", 0)
        last = self.last_prices.get(asset_key)
        if last is not None and cur == last:
            self.stale_price_cycles[asset_key] = (
                self.stale_price_cycles.get(asset_key, 0) + 1
            )
        else:
            self.stale_price_cycles[asset_key] = 0
        self.last_prices[asset_key] = cur

    # ── Step 6: Position Sizing ───────────────────────────────────────────

    def _calc_position_size(
        self,
        asset_key: str,
        candles: list,
        strategy: dict,
        hurst_mode: str = "mean_reversion",
    ) -> float:
        """Volatility-Based Position Sizing (VBPS) — research spec §5.

        R_base = 1.0% of account equity per trade.
        size_r = R_base / (atr_pct * sl_mult) so each trade risks exactly 1%
        if the ATR-based stop is hit. 0.45<H≤0.55 (random_walk) halves R_base.

        Still applies: streak scalar, heat scalar, trust-state, VaR cap.
        """
        ps = strategy.get("position_sizing", {})
        if not ps.get("enabled", True):
            return strategy.get("position_size_r", 0.5)

        # R_base from spec: 1.0% of account equity
        r_base = ps.get("r_base", 0.01)

        # Halve for random-walk regime
        if hurst_mode == "random_walk":
            r_base *= 0.5

        # ATR-based stop distance (VBPS core)
        atr_pct = self._calc_atr(candles, period=ps.get("atr_period", 14))
        if atr_pct and atr_pct > 0:
            sl_mult = strategy.get("atr_sl_mult_alt", 3.0)
            if "BTC" in asset_key or "ETH" in asset_key:
                sl_mult = strategy.get("atr_sl_mult_major", 2.0)
            stop_distance = atr_pct * sl_mult  # e.g. 0.011 for 3× ATR on alt
            if stop_distance > 0:
                size_r = r_base / stop_distance
            else:
                size_r = r_base / 0.02  # fallback 2% stop
        else:
            size_r = r_base / 0.02  # fallback when no ATR data

        # Streak scalar — shrink after consecutive losses
        ls = self.consecutive_losses.get(asset_key, 0)
        if ls > 0:
            sr = ps.get("streak_reduction", 0.5)
            ss = max(sr**ls, ps.get("max_streak_reduction", 0.25))
        else:
            ss = 1.0

        # Heat scalar — cap total exposure
        oc = sum(1 for p in self.positions.values() if p is not None)
        total_positions_after = oc + 1
        mte = ps.get("max_total_exposure", 0.8)
        capped_size = mte / total_positions_after if total_positions_after > 0 else mte

        # VaR position cap (Phase 6)
        closes = [c["close"] for c in candles]
        var_pct = compute_var(closes, confidence=0.95)
        if var_pct is not None:
            vr = ps.get("var_risk_fraction", 0.10)
            var_cap = var_position_cap(
                equity=self.paper_balance, var_pct=var_pct, max_var_exposure=vr
            )
            self.var_cache[asset_key] = {
                "var_pct": round(var_pct * 100, 2),
                "cap": round(var_cap, 4),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        else:
            var_cap = 1.0

        # Apply all scalars
        raw_size = size_r * ss * var_cap
        # Trust-state scaling
        trust_capped = capped_size * self.trust_multiplier
        final_size = min(raw_size, trust_capped)
        return round(final_size, 4)

    def _calc_ema_value(self, closes: list, period: int = 20) -> Optional[float]:
        """Calculate current EMA value from close prices."""
        if len(closes) < period + 1:
            return None
        m = 2 / (period + 1)
        ema = sum(closes[:period]) / period
        for i in range(period, len(closes)):
            ema = closes[i] * m + ema * (1 - m)
        return ema

    def _calc_chandelier_exit(
        self, candles: list, high_since_entry: float, multiplier: float = 4.0
    ) -> Optional[float]:
        """Calculate Chandelier Exit level for long positions.

        Chandelier Exit = Highest High (since entry) - ATR * multiplier

        Trails upward as price increases — never moves down.
        """
        atr_pct = self._calc_atr(candles, period=14)
        if atr_pct is None or high_since_entry <= 0:
            return None
        current_price = candles[-1]["close"] if candles else 0
        atr_price = atr_pct * current_price
        chandelier = high_since_entry - (atr_price * multiplier)
        return chandelier

    def _check_time_exit(
        self, asset_key: str, position: dict, current_price: float, strategy: dict
    ) -> bool:
        """Check if position has exceeded max holding period.

        Closes position at market price if held beyond configurable cycle limits.
        60 cycles (majors) / 45 cycles (alts) per research recommendation.
        Prevents capital getting trapped in dead-range positions.
        """
        entry_time_str = position.get("entry_time")
        if not entry_time_str:
            return False
        try:
            entry_dt = datetime.fromisoformat(entry_time_str)
            elapsed_cycles = (
                datetime.now(timezone.utc) - entry_dt
            ).total_seconds() / 60
            is_major = "BTC" in asset_key or "ETH" in asset_key
            max_cycles = strategy.get("time_exit_cycles", 60 if is_major else 45)
            return elapsed_cycles >= max_cycles
        except Exception:
            return False

    # ── Phase 3: Trend-Following Sleeve Manager ─────────────────────────

    async def _manage_trend_sleeve(
        self,
        asset_key: str,
        current_price: float,
        strategy: dict,
        price_data: dict,
        hurst: dict,
    ):
        """Run trend sleeve cycle for a major asset (BTC/ETH/SOL).

        Entry conditions (per spec):
          - H > 0.55 (trending regime — checked by caller context)
          - EMA 9 > EMA 21 on 1h candles
          - ADX(14) > 25 on 1h candles
          - Room in portfolio (max 3-4 total across both sleeves)

        Exit:
          - Chandelier Exit: 2.5x ATR for BTC/ETH, 4.0x ATR for SOL
          - No time-based exits (trend rides momentum)
        """
        # ── Cooldown tick ──
        self.trend_cooldown[asset_key] = self.trend_cooldown.get(asset_key, 0) + 1

        # ── Exit check for existing trend position ──
        trend_pos = self.trend_positions.get(asset_key)
        if trend_pos is not None:
            bars_1m = self._read_1m_bars(asset_key)
            candles_1h = self._resample_1h_candles(bars_1m)
            if len(candles_1h) >= 15:
                chandelier = self._calc_trend_chandelier(
                    asset_key, trend_pos, candles_1h
                )
                if chandelier is not None and current_price < chandelier:
                    entry = trend_pos["entry_price"]
                    pnl_pct = ((current_price - entry) / entry) * 100
                    ctx = self._current_market_context()
                    self._close_position(
                        asset_key,
                        current_price,
                        pnl_pct,
                        "trend_chandelier",
                        price_data,
                        ctx,
                    )
                    self.trend_positions[asset_key] = None
                    print(
                        f"  {asset_key}: 🔚 TREND CHANDELIER EXIT @ {current_price:.4f}"
                    )
                    return
            # Still in trend — log status
            entry = trend_pos["entry_price"]
            pnl_pct = ((current_price - entry) / entry) * 100
            print(
                f"  {asset_key}: 📈 TREND POSITION @ {entry:.2f} | PnL: {pnl_pct:+.2f}%"
            )
            return

        # ── Entry check ──
        # 1. Portfolio capacity (max 3-4 total across both sleeves)
        mr_open = sum(1 for p in self.positions.values() if p is not None)
        trend_open = sum(1 for p in self.trend_positions.values() if p is not None)
        total_open = mr_open + trend_open
        if total_open >= self.max_concurrent_total:
            return

        # 2. Must be in trending regime (H > 0.55)
        h = hurst.get("hurst")
        if h is None or h <= 0.55:
            return  # not trending — skip trend entries

        # 3. Check entry signal from 1h data
        entry_signal = self._check_trend_entry_signal(asset_key)
        if not entry_signal["entry"]:
            if entry_signal["reason"] != "insufficient_data":
                print(f"  {asset_key}: 📊 Trend setup — {entry_signal['reason']}")
            return

        # 4. Cooldown: at least 10 cycles between trend entries (reduced from 30
        #    to prevent trend sleeve being effectively inert)
        trend_last = self.trend_cooldown.get(asset_key, 999)
        if trend_last < 10:
            print(f"  {asset_key}: 📊 Trend cooldown {trend_last}/10")
            return

        # 5. Cross-sleeve net exposure: don't open trend if MR already has this asset
        mr_pos = self.positions.get(asset_key)
        if mr_pos is not None:
            print(f"  {asset_key}: 🚫 Cross-sleeve block — MR position open for {asset_key}")
            return

        # ── Entry confirmed ──
        # VBPS for trend: R_base=0.01, stop_distance=atr_pct*multiplier (wider = smaller size)
        candles_1m = price_data.get("candles", [])
        size_r = self._calc_position_size(
            asset_key, candles_1m, strategy, hurst_mode="trend"
        )
        # Open as trend position
        position = {
            "asset": asset_key,
            "entry_price": current_price,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "position_size_r": size_r,
            "signal": "trend_ema_cross",
            "direction": "long",
            "entry_source": price_data.get("source", "unknown"),
            "market_context": self._current_market_context(),
            "confidence_score": None,
            "scaled_out": False,
            "chandelier_high": current_price,
            "sleeve": "trend",
            "strategy": "ema_9_21_adx",
        }
        self.trend_positions[asset_key] = position
        self.trend_cooldown[asset_key] = 0
        print(
            f"📈 {asset_key}: TREND OPEN at {current_price:.6f} | size_r={size_r} | signal=ema_9_21_cross"
        )
        # Log to trades.jsonl as well (trend trades tracked separately)
        trades_file = self.state_dir / asset_key / "trades.jsonl"
        trades_file.parent.mkdir(parents=True, exist_ok=True)
        # Use distinct PnL prefix for trend trades

    # ── Phase 3: Trend Helper Methods ───────────────────────────────────

    def _read_1m_bars(self, asset_key: str) -> list:
        """Read full 1m OHLC from SQLite bar store."""
        from hermes_trading.adaptive import BAR_DB_PATH

        if not BAR_DB_PATH.exists():
            return []
        try:
            conn = sqlite3.connect(str(BAR_DB_PATH))
            rows = conn.execute(
                "SELECT timestamp, open, high, low, close, volume FROM bars "
                "WHERE asset = ? ORDER BY timestamp ASC",
                (asset_key,),
            ).fetchall()
            conn.close()
            return [
                {
                    "timestamp": r[0],
                    "open": r[1],
                    "high": r[2],
                    "low": r[3],
                    "close": r[4],
                    "volume": r[5],
                }
                for r in rows
            ]
        except Exception:
            return []

    def _resample_1h_candles(self, bars_1m: list) -> list:
        """Resample 1m OHLC bars to 1h candles.

        Groups every 60 consecutive 1m bars into one 1h candle.
        Returns list of dicts with keys: timestamp, open, high, low, close.
        Returns empty list if fewer than 60 bars available.
        """
        if len(bars_1m) < 60:
            return []
        candles_1h = []
        for i in range(0, len(bars_1m), 60):
            group = bars_1m[i : i + 60]
            if len(group) < 60:
                break  # incomplete final hour
            candle = {
                "timestamp": group[0]["timestamp"],
                "open": group[0]["open"],
                "high": max(b["high"] for b in group),
                "low": min(b["low"] for b in group),
                "close": group[-1]["close"],
            }
            candles_1h.append(candle)
        return candles_1h

    def _check_trend_entry_signal(self, asset_key: str) -> dict:
        """Check trend entry conditions: EMA 9/21 crossover + ADX > 25.

        Reads 1m bars from DB, resamples to 1h, computes indicators.
        Returns dict: {'entry': bool, 'direction': str, 'reason': str,
                       'ema9': float, 'ema21': float, 'adx': float}
        """
        result = {
            "entry": False,
            "direction": "none",
            "reason": "insufficient_data",
            "ema9": None,
            "ema21": None,
            "adx": None,
        }

        bars_1m = self._read_1m_bars(asset_key)
        candles_1h = self._resample_1h_candles(bars_1m)
        if len(candles_1h) < 22:
            result["reason"] = f"need 22 1h bars, have {len(candles_1h)}"
            return result

        closes = [c["close"] for c in candles_1h]
        highs = [c["high"] for c in candles_1h]
        lows = [c["low"] for c in candles_1h]

        # EMA 9 and EMA 21
        ema9 = self._calc_ema_value(closes, period=9)
        ema21 = self._calc_ema_value(closes, period=21)
        result["ema9"] = ema9
        result["ema21"] = ema21

        if ema9 is None or ema21 is None:
            result["reason"] = "EMA computation failed"
            return result

        # EMA crossover (bullish: fast EMA crosses above slow EMA)
        # Check latest two 1h candles for crossover
        prev_close9 = None
        prev_close21 = None
        if len(closes) >= 23:
            prev_close9 = self._calc_ema_value(closes[:-1], period=9)
            prev_close21 = self._calc_ema_value(closes[:-1], period=21)

        # ADX > 25 on 1h
        adx = self._calc_adx_from_ohlc(highs, lows, closes, period=14)
        result["adx"] = adx

        is_bullish = ema9 > ema21
        was_bullish = (
            (
                prev_close9 is not None
                and prev_close21 is not None
                and prev_close9 > prev_close21
            )
            if prev_close9 is not None
            else False
        )
        crossover_long = is_bullish and not was_bullish  # just crossed up

        if not is_bullish:
            result["reason"] = f"bearish EMA (9={ema9:.2f} < 21={ema21:.2f})"
            return result

        if adx is None or adx < 25:
            result["reason"] = f"ADX={adx:.1f} < 25" if adx else "ADX N/A"
            return result

        result["entry"] = True
        result["direction"] = "long"
        result["reason"] = (
            f"EMA 9/21 bullish (9={ema9:.2f} > 21={ema21:.2f}), ADX={adx:.1f} > 25"
        )
        return result

    def _calc_trend_chandelier(
        self, asset_key: str, position: dict, candles_1h: list
    ) -> Optional[float]:
        """Chandelier Exit for trend positions with asset-specific multipliers.

        BTC/ETH: 2.5x ATR(14) on 1h candles
        SOL: 4.0x ATR(14) on 1h candles
        """
        if not candles_1h or len(candles_1h) < 15:
            return None

        closes = [c["close"] for c in candles_1h]
        highs = [c["high"] for c in candles_1h]
        lows = [c["low"] for c in candles_1h]
        current_price = closes[-1]

        # ATR on 1h candles
        atr_pct = self._calc_atr(
            [
                {"high": highs[i], "low": lows[i], "close": closes[i]}
                for i in range(len(closes))
            ],
            period=14,
        )
        if atr_pct is None or atr_pct <= 0:
            return None

        atr_price = atr_pct * current_price
        multiplier = 2.5 if "SOL" not in asset_key else 4.0  # BTC/ETH=2.5, SOL=4.0
        high_since_entry = position.get(
            "chandelier_high", position.get("entry_price", current_price)
        )
        chandelier = high_since_entry - (atr_price * multiplier)
        return chandelier

    def _check_zscore_flash(self, candles: list, asset_key: str) -> tuple:
        """Check 5-minute return z-score for flash crash detection.

        Computes rolling z-score of 5-minute returns. If latest return exceeds
        -3.0σ (majors) or -2.0σ (alts), flags capitulation. The safety gate
        then blocks entries for 30 cycles (cooling chamber).

        Returns:
            (blocked: bool, reason: str)
        """
        if len(candles) < 30:
            return False, ""
        closes = [c["close"] for c in candles]

        # Compute 5-minute returns (every 5th 1m close)
        five_min_returns = []
        for i in range(5, len(closes)):
            if closes[i - 5] > 0:
                five_min_returns.append((closes[i] - closes[i - 5]) / closes[i - 5])

        if len(five_min_returns) < 10:
            return False, ""

        latest = five_min_returns[-1]
        mean = sum(five_min_returns) / len(five_min_returns)
        variance = sum((r - mean) ** 2 for r in five_min_returns) / (
            len(five_min_returns) - 1
        )
        std = variance**0.5

        if std <= 0:
            return False, ""

        zscore = (latest - mean) / std
        threshold = -3.0 if ("BTC" in asset_key or "ETH" in asset_key) else -2.0

        if zscore < threshold:
            return (
                True,
                f"zscore_cooling: {zscore:.2f}σ below {threshold:.0f}σ threshold",
            )

        return False, ""

    def _update_btc_vol_history(self):
        """Track BTC 1m returns for realized vol surge detection.

        Computes 1m BTC return from consecutive price fetches and stores in
        rolling history. Sets btc_vol_surge_blocked if recent 3-cycle vol
        exceeds baseline 6-cycle vol by >30% — captures intra-hour vol shocks.
        """
        btc_price = self.btc_context.get("btc_price")
        last_price = getattr(self, "_last_btc_price", None)
        if btc_price is not None and last_price is not None and last_price > 0:
            ret = abs(btc_price - last_price) / last_price
            self.btc_vol_history.append(ret)
            if len(self.btc_vol_history) > 48:
                self.btc_vol_history = self.btc_vol_history[-48:]
        self._last_btc_price = btc_price

        # Check surge: recent 3 cycles vs baseline 6+ cycles
        self.btc_vol_surge_blocked = False
        self.btc_vol_surge_reason = ""
        if len(self.btc_vol_history) >= 9:
            recent = self.btc_vol_history[-3:]
            baseline = self.btc_vol_history[:-3]
            avg_recent = sum(recent) / len(recent)
            avg_baseline = sum(baseline) / len(baseline)
            if avg_baseline > 0 and (avg_recent / avg_baseline) > 1.30:
                self.btc_vol_surge_blocked = True
                self.btc_vol_surge_reason = (
                    f"btc_vol_surge: {avg_recent / avg_baseline:.2f}x avg"
                )

    def _update_oi_snapshots(self):
        """Track OI snapshots hourly for velocity gate.

        Once per 60 cycles (~hourly), stores current OI for each tracked asset.
        If any asset's OI expanded > 15% vs snapshot ~48 hours ago, the
        kill switch blocks entries (institutional trend-building detection).
        """
        self.oi_cycle_counter += 1
        if self.oi_cycle_counter % 60 != 0:
            return

        hl_assets = self.hl_context.get("assets", {})
        if not hl_assets:
            return

        for symbol, data in hl_assets.items():
            oi_usd = data.get("open_interest_usd", 0)
            if oi_usd is not None and oi_usd > 0:
                if symbol not in self.oi_snapshots:
                    self.oi_snapshots[symbol] = []
                self.oi_snapshots[symbol].append(oi_usd)
                # Keep last 50 snapshots (~50 hours)
                if len(self.oi_snapshots[symbol]) > 50:
                    self.oi_snapshots[symbol] = self.oi_snapshots[symbol][-50:]

    def _update_adx_danger_zones(self, asset_key: str, candles: list, strategy: dict):
        """Check 15m/1h ADX for trend extreme conditions.

        Resamples 1m candles to 15m and 1h timeframes and computes ADX(14).
        Sets adx_danger_blocked when:
          - 15m ADX > 35 (trend extreme)
          - OR 1h ADX > 30 AND price < EMA200 (trend + bearish)
        """
        self.adx_danger_blocked[asset_key] = False
        if len(candles) < 225:
            return  # not enough data for 15m ADX(14)

        def _resample(closes, step):
            """Resample 1m closes to target step, returning list of closes."""
            return [closes[i] for i in range(step - 1, len(closes), step)]

        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]

        tf = strategy.get("trend_filter", {})
        adx_period = tf.get("adx_period", 14)

        # 15m ADX
        clos15 = _resample(closes, 15)
        high15 = _resample(highs, 15)
        low15 = _resample(lows, 15)
        if len(clos15) >= adx_period * 2 + 1:
            try:
                adx15 = self._calc_adx_from_ohlc(
                    high15, low15, clos15, period=adx_period
                )
                if adx15 is not None and adx15 > 35:
                    self.adx_danger_blocked[asset_key] = True
                    print(
                        f"  {asset_key}: 🚨 ADX danger zone — 15m ADX={adx15:.1f} > 35"
                    )
            except Exception:
                pass

        # 1h ADX + EMA200
        if len(closes) >= 900 and not self.adx_danger_blocked.get(asset_key, False):
            clos60 = _resample(closes, 60)
            high60 = _resample(highs, 60)
            low60 = _resample(lows, 60)
            if len(clos60) >= adx_period * 2 + 2:
                try:
                    adx60 = self._calc_adx_from_ohlc(
                        high60, low60, clos60, period=adx_period
                    )
                    ema200 = self._calc_ema_value(clos60, period=200)
                    if adx60 is not None and ema200 is not None and adx60 > 30:
                        if closes[-1] < ema200:
                            self.adx_danger_blocked[asset_key] = True
                            print(
                                f"  {asset_key}: 🚨 ADX danger zone — 1h ADX={adx60:.1f} > 30, price < EMA200"
                            )
                except Exception:
                    pass

    def _calc_adx_from_ohlc(
        self, highs: list, lows: list, closes: list, period: int = 14
    ) -> Optional[float]:
        """Calculate ADX from pre-resampled OHLC data."""
        if len(closes) < period * 2 + 1:
            return None
        tr_list, plus_dm_list, minus_dm_list = [], [], []
        for i in range(1, len(closes)):
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - closes[i - 1])
            lc = abs(lows[i] - closes[i - 1])
            tr_list.append(max(hl, hc, lc))
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]
            plus_dm = up_move if up_move > down_move and up_move > 0 else 0
            minus_dm = down_move if down_move > up_move and down_move > 0 else 0
            plus_dm_list.append(plus_dm)
            minus_dm_list.append(minus_dm)
        if len(tr_list) < period + 1:
            return None
        atr_vals, plus_di, minus_di = [], [], []
        for i in range(period, len(tr_list)):
            atr = sum(tr_list[i - period : i]) / period
            if atr == 0:
                continue
            plus_di_val = sum(plus_dm_list[i - period : i]) / period / atr * 100
            minus_di_val = sum(minus_dm_list[i - period : i]) / period / atr * 100
            atr_vals.append(atr)
            plus_di.append(plus_di_val)
            minus_di.append(minus_di_val)
        if len(atr_vals) < period:
            return None
        dx_list = []
        for i in range(len(plus_di)):
            di_sum = plus_di[i] + minus_di[i]
            di_diff = abs(plus_di[i] - minus_di[i])
            dx_list.append(di_diff / di_sum * 100 if di_sum > 0 else 0)
        if len(dx_list) < period:
            return None
        adx = sum(dx_list[-period:]) / period
        return adx

    def _classify_regime(self, price_data: dict) -> str:
        """Simple 20-period rolling return classifier for market regime."""
        candles = price_data.get("candles", [])
        if len(candles) < 20:
            return "unknown"
        closes = [c["close"] for c in candles[-20:]]
        ret = (closes[-1] - closes[0]) / closes[0]
        if ret > 0.05:
            return "strong_upward"
        elif ret > 0.01:
            return "upward"
        elif ret < -0.05:
            return "strong_downward"
        elif ret < -0.01:
            return "downward"
        else:
            return "range_bound"

    STRATEGY_SCHEMA_VERSION = 22  # Current expected strategy config version

    REQUIRED_STRATEGY_FIELDS = {
        "entry": {"threshold", "direction"},
        "stop_loss_pct": set(),
        "position_size_r": set(),
        "cooldown_cycles": set(),
        "btc_gate": {"min_btc_1h_rsi"},
        "fng_gate": {"min_value"},
        "hurst": {"enabled"},
        "kill_switches": {"enabled", "max_open_positions"},
        "position_sizing": {"enabled", "base_r"},
    }

    def _validate_strategy(self, strategy: dict, asset_key: str) -> list:
        """Validate strategy config against required schema. Returns list of warnings."""
        warnings = []

        # Version check
        ver = strategy.get("version")
        if ver is None:
            warnings.append(f"{asset_key}: no version field — add version: {self.STRATEGY_SCHEMA_VERSION}")
        else:
            try:
                if int(ver) < self.STRATEGY_SCHEMA_VERSION:
                    warnings.append(
                        f"{asset_key}: version v{ver} < current v{self.STRATEGY_SCHEMA_VERSION} "
                        f"— consider migrating"
                    )
            except (ValueError, TypeError):
                warnings.append(f"{asset_key}: non-numeric version '{ver}'")

        # Required field check
        for field, subfields in self.REQUIRED_STRATEGY_FIELDS.items():
            if field not in strategy:
                warnings.append(f"{asset_key}: missing required field '{field}'")
                continue
            if subfields:
                val = strategy[field]
                if not isinstance(val, dict):
                    warnings.append(f"{asset_key}: '{field}' should be a dict")
                    continue
                for sf in subfields:
                    if sf not in val:
                        warnings.append(
                            f"{asset_key}: missing required subfield '{field}.{sf}'"
                        )

        return warnings

    def _load_strategy(self, asset_key: str) -> Optional[Dict]:
        """Load strategy from state file. Returns None if missing (fail-closed)."""
        strat_path = self.state_dir / asset_key / "strategy.yaml"
        if not strat_path.exists():
            if self.mode == "live":
                print(
                    f"🔴 FATAL: {asset_key}: strategy.yaml not found — refusing to trade live"
                )
                sys.exit(1)
            print(
                f"  ⚠️  {asset_key}: strategy.yaml not found — skipping asset (fail-closed)"
            )
            return None
        with open(strat_path) as f:
            strategy = yaml.safe_load(f)
        # Run validation
        if strategy:
            strategy["_asset"] = asset_key
            warnings = self._validate_strategy(strategy, asset_key)
            for w in warnings:
                print(f"  ⚠️  {w}")
        return strategy

    def _load_mc_dd_threshold(self):
        """Load Monte Carlo 95th percentile max DD from backtest state."""
        mc_file = self.state_dir / "mc_dd_95.json"
        if mc_file.exists():
            try:
                import json

                data = json.loads(mc_file.read_text())
                self.mc_dd_threshold = data.get("dd_95_pct")
            except Exception:
                self.mc_dd_threshold = None

    def _compute_trust_state(self, asset_key: str, strategy: dict) -> float:
        """Unified trust-state: single 0-1 multiplier from all risk signals."""
        ks = strategy.get("kill_switches", {})
        max_pos = ks.get("max_open_positions", 3)
        oc = sum(1 for p in self.positions.values() if p is not None)

        trust = compute_trust_state(
            asset_key=asset_key,
            consecutive_losses=self.consecutive_losses,
            stale_price_cycles=self.stale_price_cycles,
            daily_pnl_pct=self.daily_pnl_pct,
            open_positions_count=oc,
            max_open_positions=max_pos,
        )
        self.trust_multiplier = trust
        self.trust_label = trust_status(trust)
        return trust

    # ── Optimizer check (run on cycle tick, logs if ready) ──

    def _check_optimizer_ready(self, asset_key: str):
        """Check optimizer triggers: trade threshold, performance drop, or scheduled.

        Runs check_and_optimize() with the new trigger-aware optimizer.
        Stores results per-asset in optimizer_logs.
        """
        from hermes_trading.optimizer import check_and_optimize as run_opt

        trades_file = self.state_dir / asset_key / "trades.jsonl"
        if not trades_file.exists():
            return

        import json

        trades = [
            json.loads(l) for l in trades_file.read_text().strip().split("\n") if l
        ]
        if len(trades) < 5:
            return  # not enough data for meaningful analysis

        # Load previous optimization log for this asset
        opt_file = self.state_dir / asset_key / "optimizer_log.jsonl"
        opt_log = []
        if opt_file.exists():
            opt_log = [
                json.loads(l) for l in opt_file.read_text().strip().split("\n") if l
            ]

        cfg = self._load_strategy(asset_key)
        result = run_opt(asset_key, trades, cfg, opt_log)

        # Store per-asset
        if asset_key not in self.optimizer_logs:
            self.optimizer_logs[asset_key] = []
        self.optimizer_logs[asset_key].append(result)

        # Update global status
        self.optimizer_status = result

        # Log result to file
        opt_file.parent.mkdir(parents=True, exist_ok=True)
        with open(opt_file, "a") as f:
            f.write(json.dumps(result) + "\n")

        # Print summary
        trigger = result.get("trigger", {})
        if trigger.get("triggered"):
            wf = result.get("walk_forward", {})
            bm = result.get("benchmarks", {})
            recs = wf.get("recommendations", [])
            print(
                f"🧠 {asset_key}: Optimizer {result['status']} "
                f"(trigger: {trigger.get('reasons', ['unknown'])[0]} | "
                f"trades: {result['trades_available']} | "
                f"fitness: train={wf.get('train_fitness', {}).get('fitness', '?')}/"
                f"val={wf.get('validate_fitness', {}).get('fitness', '?')})"
            )
            for r in recs:
                print(f"   → REC: {r}")
            if bm.get("status") == "analyzed":
                sharpe_ok = "✅" if bm.get("sharpe_met") else "❌"
                wr_ok = "✅" if bm.get("win_rate_met") else "❌"
                dd_ok = "✅" if bm.get("max_dd_met") else "❌"
                print(
                    f"   Benchmarks: Sharpe {bm.get('sharpe', '?')} {sharpe_ok} "
                    f"| WR {bm.get('win_rate', '?'):.0%} {wr_ok} "
                    f"| DD {bm.get('max_drawdown', '?')}% {dd_ok}"
                )
        else:
            print(
                f"  {asset_key}: Optimizer dormant ({result.get('message', 'waiting for triggers')})"
            )

    def _replay_historical_trades(self):
        """Scan all trade log files and compound PnL into paper_balance.
        Ensures balance reflects historical trades after crash/restart cycles."""
        base_dirs = list(self.state_dir.iterdir())
        total_replayed = 0
        compound = 1.0
        for asset_dir in sorted(base_dirs):
            if not asset_dir.is_dir():
                continue
            tf = asset_dir / "trades.jsonl"
            if not tf.exists():
                continue
            for line in tf.read_text().strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    t = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Use net_pnl_pct if available, fall back to pnl_pct
                pnl = t.get("net_pnl_pct")
                if pnl is None:
                    pnl = t.get("pnl_pct", 0.0) or 0.0
                compound *= 1.0 + pnl / 100.0
                total_replayed += 1
        self.paper_balance = round(self.initial_balance * compound, 2)
        if total_replayed > 0:
            print(
                f"📊 Replayed {total_replayed} historical trades → "
                f"balance: ${self.initial_balance:.0f} → ${self.paper_balance:.2f} "
                f"({(self.paper_balance / self.initial_balance - 1) * 100:+.2f}%)"
            )

    def _write_heartbeat(self):
        """Write heartbeat JSON to state."""
        now = datetime.now(timezone.utc).isoformat()
        positions_summary = {}
        for key, pos in self.positions.items():
            if pos:
                positions_summary[key] = {
                    "entry_price": pos["entry_price"],
                    "entry_time": pos["entry_time"],
                    "signal": pos["signal"],
                }
            else:
                positions_summary[key] = None

        heartbeat = {
            "timestamp": now,
            "mode": self.mode,
            "paper_balance": round(self.paper_balance, 2),
            "paper_start_date": self.paper_start_date,
            "total_pnl_pct": round(
                (self.paper_balance - self.initial_balance)
                / self.initial_balance
                * 100,
                2,
            ),
            "positions": positions_summary,
            "trend_positions": {
                k: {
                    "entry_price": v["entry_price"],
                    "entry_time": v["entry_time"],
                    "signal": v["signal"],
                    "strategy": v.get("strategy", "unknown"),
                }
                for k, v in self.trend_positions.items()
                if v is not None
            },
            "btc_context": {
                "btc_price": self.btc_context.get("btc_price"),
                "btc_1h_rsi": self.btc_context.get("btc_1h_rsi"),
                "btc_4h_rsi": self.btc_context.get("btc_4h_rsi"),
            },
            "fear_greed": {
                "value": self._fng_value(),
                "classification": self._fng_class(),
            },
            "trade_count_since_reflection": dict(self.trade_count_since_reflection),
            "cycles_since_last_trade": dict(self.cycles_since_last_trade),
            "trust_state": {
                "multiplier": round(self.trust_multiplier, 3),
                "label": self.trust_label,
            },
            "monte_carlo": {
                "dd_95_pct": self.mc_dd_threshold,
            },
            "max_drawdown": {
                "highest_dd_pct": round(
                    self.portfolio_tracker.status().get("highest_dd_pct", 0), 2
                ),
                "current_dd_pct": round(
                    self.portfolio_tracker.status().get("drawdown_pct", 0), 2
                ),
            },
            "optimizer": {
                "status": self.optimizer_status.get("status", "unknown"),
                "trades_needed": max(
                    0,
                    200
                    - max(
                        (self.optimizer_status.get("trades_available", 0) for _ in [1]),
                        default=0,
                    ),
                ),
            },
        }
        hb_file = self.state_dir / "heartbeat.json"
        hm_state.atomic_write_json(hb_file, heartbeat)

    def _write_runtime(self):
        """Write consolidated runtime state to state/runtime.json atomically."""
        runtime = hm_state.build_from_loop(self)
        hm_state.write_runtime(self.state_dir, runtime)
