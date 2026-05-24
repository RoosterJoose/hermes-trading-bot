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
import sys
import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from hermes_trading.adaptive import compute_dynamic_rsi_threshold, compute_hurst_exponent, compute_cusum_regime

from hermes_trading.universe import fetch_hl_meta_and_ctx, parse_universe, screen_and_rank, format_universe_report

from hermes_trading.risk import PortfolioTracker, correlation_allows_entry, compute_correlations, compute_var, var_position_cap
from hermes_trading.trust_state import compute_trust_state, status as trust_status
from hermes_trading.optimizer import trades_ready, check_and_optimize

from hermes_trading.schema import SchemaError, validate_adapter_output


class TradingLoop:
    def __init__(self, assets: List[Dict], state_dir: Path, base_dir: Path, mode: str = "paper", initial_balance: float = 10000.0):
        self.assets = assets
        self.state_dir = state_dir
        self.base_dir = base_dir
        self.mode = mode
        self.cycle_interval = 60  # seconds — check every minute
        self.max_consecutive_failures = 5
        self.user_risk_accepted = os.environ.get("HERMES_TRADING_I_ACCEPT_RISK", "").lower() == "true"

        # Open positions per asset
        self.positions: Dict[str, Optional[Dict]] = {a["key"]: None for a in assets}

        # Cooldown: cycles since last trade close (prevents rapid re-entry)
        self.cycles_since_last_trade: Dict[str, int] = {a["key"]: 999 for a in assets}

        # Trade count tracker (for reflection trigger)
        self.trade_count_since_reflection: Dict[str, int] = {a["key"]: 0 for a in assets}

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

        # Phase 6: Advanced Risk Management
        self.correlations: dict = {}
        self.correlation_window = 90
        self.portfolio_tracker = PortfolioTracker(initial_equity=10000.0, max_drawdown_pct=15.0)
        self.var_cache: dict = {}
        self.risk_log: list = []

        # Paper balance tracking (dollar PnL)
        self.initial_balance = initial_balance
        self.paper_balance = initial_balance

        # Trust-state scaling (unified risk score)
        self.trust_multiplier: float = 1.0
        self.trust_label: str = "high"

        # Monte Carlo 95% DD threshold (loaded from backtest data)
        self.mc_dd_threshold: Optional[float] = None
        self._load_mc_dd_threshold()

        # Optimizer readiness
        self.optimizer_status: dict = {"status": "dormant"}
        self.optimizer_logs: dict = {}  # asset_key → list of optimization results

    async def run(self):
        """Main loop — runs until cancelled."""
        sys.path.insert(0, str(self.base_dir))
        from hermes_trading.adapters import PriceAdapter, OnChainAdapter, NewsAdapter, MacroAdapter

        price = PriceAdapter(mode=self.mode)
        onchain = OnChainAdapter(mode=self.mode)
        news = NewsAdapter(mode=self.mode)
        macro = MacroAdapter(mode=self.mode)

        print(f"🔄 Trading loop started — checking every {self.cycle_interval}s")
        print(f"   Mode: {self.mode.upper()} | Assets: {', '.join(a['key'] for a in self.assets)}")
        print(f"   Strategy: RSI entry (< threshold) + BTC market gate + FnG filter")
        first_strat = self._load_strategy(self.assets[0]["key"])
        default_cooldown = first_strat.get("cooldown_cycles", 30) if first_strat else 30
        print(f"   Cooldown: {default_cooldown} cycles (configurable per-asset)")

        if self.mode == "live" and not self.user_risk_accepted:
            print("⚠️  LIVE MODE: Set HERMES_TRADING_I_ACCEPT_RISK=true to enable live trading")
            print("   Worker will observe-only until flag is set")

        try:
            while True:
                # ── Fetch BTC market context once per cycle ──
                self.btc_context = await self._fetch_btc_context(price)

                # ── Fetch Fear & Greed once per cycle ──
                fng_data = await macro.fetch(self.assets[0]["key"])
                if fng_data.get("available"):
                    self.last_fear_greed = fng_data.get("indicators", {}).get("fear_greed", {})

                # Phase 4: Fetch Hyperliquid market context (funding, OI, universe)
                await self._fetch_hl_context()

                # Log market snapshot
                self._log_market_snapshot()

                # ── Cycle through each asset ──
                for asset_cfg in self.assets:
                    key = asset_cfg["key"]
                    strategy = self._load_strategy(key)
                    if not strategy:
                        continue

                    await self._cycle(key, asset_cfg, strategy, price, onchain, news, macro)

                self._write_heartbeat()
                await asyncio.sleep(self.cycle_interval)

        finally:
            await price._close_exchanges()

    async def _fetch_btc_context(self, price_adapter) -> dict:
        """Fetch BTC 1h candles and compute RSI on 1h and 4h timeframes."""
        ctx = {"btc_1h_rsi": None, "btc_4h_rsi": None, "btc_price": None, "available": False}

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
                    closes_4h = closes_1h[-(14 * 4):][::4]  # every 4th close for last 56 closes
                    ctx["btc_4h_rsi"] = self._calc_rsi(closes_4h, period=14)
                else:
                    # Fallback: use RSI on 1h data with longer period as proxy
                    ctx["btc_4h_rsi"] = self._calc_rsi(closes_1h, period=56)

                ctx["available"] = True

            btc_status = f"1h={ctx['btc_1h_rsi']:.0f}" if ctx['btc_1h_rsi'] else "1h=---"
            btc_status += f" 4h={ctx['btc_4h_rsi']:.0f}" if ctx['btc_4h_rsi'] else " 4h=---"
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
                    print(f"  📊 {symbol}: fund={annualized:+.3f}% APY | OI=${oi_billions:.2f}B")

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
        btc_4h_min_rsi = btc_gate.get("min_btc_4h_rsi", 25)

        btc_4h_rsi = self.btc_context.get("btc_4h_rsi")
        btc_1h_rsi = self.btc_context.get("btc_1h_rsi")

        if btc_4h_rsi is not None and btc_4h_rsi < btc_4h_min_rsi:
            return (False, f"BTC 4h RSI {btc_4h_rsi:.0f} < {btc_4h_min_rsi} (too bearish)")

        if btc_1h_rsi is not None and btc_1h_rsi < btc_gate.get("min_btc_1h_rsi", 20):
            return (False, f"BTC 1h RSI {btc_1h_rsi:.0f} < {btc_gate['min_btc_1h_rsi']} (deeply oversold)")

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
            recent_lows = lows[-(cascade_check + 1):]
            is_cascading = all(recent_lows[i] > recent_lows[i + 1] for i in range(cascade_check))
            features["cascade_found"] = is_cascading
            if is_cascading:
                return (False, f"lower_low_cascade: {cascade_check} consec lower lows", features)

        # 2. Volume panic — current volume vs average
        vol_check = evaluator.get("volume_spike_mult", 1.5)
        features["vol_check"] = vol_check
        if vol_check > 0 and len(volumes) >= 11 and volumes[-1] > 0:
            avg_vol = sum(volumes[-11:-1]) / 10
            features["volume_ratio"] = volumes[-1] / avg_vol if avg_vol > 0 else 0.0
            if avg_vol > 0 and volumes[-1] > avg_vol * vol_check:
                return (False, f"volume_spike: {volumes[-1]/avg_vol:.1f}x avg", features)

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
                    return (False, f"candle_position: {position_in_candle:.0%} < {pos_check:.0%}", features)

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
                    drop_from_ema = (ema20 - current_price) / atr_price if atr_price > 0 else 0
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
                            return (False, f"falling_knife: price {drop_from_ema:.1f} ATR below EMA20, vol {vol_ratio:.1f}x", features)

        return (True, "evaluator_passed", features)

    def _log_rejected_entry(self, asset_key: str, price: float, reason: str,
                            rsi: Optional[float], threshold: float,
                            eval_features: Optional[dict] = None):
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

    def _log_evaluator_pass(self, asset_key: str, price: float,
                            rsi: Optional[float], threshold: float,
                            eval_features: dict):
        """Log an evaluator pass (accepted entry) for analysis."""
        # Check benchmark targets for this asset
        from hermes_trading.optimizer import check_benchmarks
        trades_file = self.state_dir / asset_key / "trades.jsonl"
        bm_check = {"status": "insufficient_data"}
        if trades_file.exists():
            import json
            tlines = [l for l in trades_file.read_text().strip().split('\n') if l]
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
            } if bm_check.get("status") == "analyzed" else {},
        }
        log_file = self.state_dir / asset_key / "evaluator_log.jsonl"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _log_evaluator_flag(self, asset_key: str, price: float, reason: str,
                             rsi: Optional[float], threshold: float,
                             eval_features: dict, score_override: bool = False):
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

    async def _cycle(self, asset_key: str, goal: dict, strategy: dict,
                     price_adapter, onchain_adapter, news_adapter, macro_adapter):
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
            print(f"🔴 {asset_key}: Circuit breaker — {price_adapter.consecutive_failures} consecutive failures")
            return

        current_price = price_data.get("current_price", 0.0)
        if current_price <= 0:
            return

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
        cooldown_remaining = cooldown_cycles - self.cycles_since_last_trade.get(asset_key, 999)
        rsi_status = f"RSI={rsi:.1f}" if rsi is not None else "RSI=---"

        # Phase 3: Dynamic RSI Percentile (overrides fixed threshold when enough 1m bar data)
        dynamic = self._compute_dynamic_rsi(asset_key, strategy)
        effective_threshold = dynamic["threshold"] if dynamic.get("active") else threshold
        if dynamic.get("active"):
            print(f"  {asset_key}: 📊 dynamic RSI threshold={effective_threshold} ({dynamic['percentile']}th pctile, {dynamic['bars_used']} 1m bars)")
        elif dynamic.get("reason") and dynamic["reason"] not in ("not enabled",):
            pass  # silently accumulating data — log at info level only when threshold changes

        # Phase 3b: Hurst Exponent regime classification (1m bar R/S analysis)
        hurst = self._compute_hurst(asset_key, strategy)
        if hurst.get("active"):
            regime_label = hurst["regime"].replace("_", " ")
            print(f"  {asset_key}: 📈 Hurst H={hurst['hurst']:.4f} — {regime_label}")
        elif hurst.get("reason") and hurst["reason"] not in ("not enabled",):
            pass  # silently accumulating data

        # Directional Hurst — determine mode from H(t)
        hurst_mode = "mean_reversion"
        hurst_signal = None
        h = hurst.get("hurst")
        if h is not None and hurst.get("active"):
            if h > 0.55:
                hurst_mode = "trend"
                hurst_signal = rsi is not None and rsi > 60
                print(f"  {asset_key}: 🔀 Trend mode (H={h:.3f}) — momentum entries (RSI>60)")
            elif h < 0.45:
                hurst_mode = "mean_reversion"
                hurst_signal = rsi is not None and rsi < effective_threshold
                print(f"  {asset_key}: 🔄 MR mode (H={h:.3f}) — oversold entries (RSI<{effective_threshold})")
            else:
                hurst_mode = "random_walk"
                hurst_signal = False
                print(f"  {asset_key}: ⏸️ Random walk (H={h:.3f}) — no entries")

        # Phase 3c: CUSUM regime detection (1m log return cumulative sum)
        cusum = self._compute_cusum(asset_key, strategy)
        if cusum.get("active"):
            regime_label = cusum["regime"].replace("_", " ")
            print(f"  {asset_key}: 🔄 CUSUM {regime_label} ({cusum['up_breaks']}↑/{cusum['down_breaks']}↓ breaks)")
        elif cusum.get("reason") and cusum["reason"] not in ("not enabled",):
            pass  # silently accumulating data

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
            pnl_pct = ((current_price - existing["entry_price"]) / existing["entry_price"]) * 100
            print(f"  {asset_key}: {rsi_status} | POSITION @ {existing['entry_price']:.2f} | PnL: {pnl_pct:+.2f}%")
        elif rsi is not None and rsi < effective_threshold:
            if market_blocked:
                print(f"  {asset_key}: {rsi_status} < {effective_threshold} (⬇ oversold) → MARKET GATE BLOCKED: {'; '.join(market_gate_reasons)}")
            elif hurst.get("block_entry", False):
                regime_label = hurst["regime"].replace("_", " ")
                print(f"  {asset_key}: {rsi_status} < {effective_threshold} (⬇ oversold) → HURST REGIME BLOCKED: {regime_label} (H={hurst['hurst']:.4f})")
            elif cusum.get("block_entry", False):
                regime_label = cusum["regime"].replace("_", " ")
                print(f"  {asset_key}: {rsi_status} < {effective_threshold} (⬇ oversold) → CUSUM REGIME BLOCKED: {regime_label} ({cusum['up_breaks']}↑/{cusum['down_breaks']}↓)")
            elif cooldown_remaining <= 0:
                print(f"  {asset_key}: {rsi_status} < {effective_threshold} (⬇ oversold) → RSI MET, awaiting confidence score")
            else:
                print(f"  {asset_key}: {rsi_status} < {effective_threshold} (⬇ oversold) → signal ready, cooldown {cooldown_remaining}")
        else:
            threshold_display = f"{effective_threshold}" + (f" (fixed {threshold})" if dynamic.get("active") else "")
            print(f"  {asset_key}: {rsi_status} ≥ {threshold_display} (waiting for oversold)")

        # ── 6b. Track stale prices ──
        self._check_stale_prices(price_data, asset_key)

        # ── 6c. Trust-state + Monte Carlo DD status ──
        trust_label = self.trust_label
        if trust_label in ("low", "critical"):
            print(f"  {asset_key}: 🛡️ Trust-state={trust_label} (×{self.trust_multiplier:.2f})")
        if self.mc_dd_threshold is not None:
            dd = self.portfolio_tracker.status().get("drawdown_pct", 0)
            print(f"  {asset_key}: 📉 MC DD threshold={self.mc_dd_threshold:.1f}% | current DD={dd:.1f}%")

        # ── 7. Entry signal check (confidence score based) ──
        if not has_position:
            enough_cooldown = self.cycles_since_last_trade.get(asset_key, 999) >= cooldown_cycles

            # Trust-state (updated every cycle)
            trust = self._compute_trust_state(asset_key, strategy)
            mc_dd_allowed = True
            if self.mc_dd_threshold is not None:
                dd_val = self.portfolio_tracker.status().get("drawdown_pct", 0)
                mc_dd_allowed = dd_val < self.mc_dd_threshold

            # ── Safety gates (hard blocks — checked first) ──
            safety_skip_reason = None

            if not enough_cooldown:
                safety_skip_reason = f"cooldown {self.cycles_since_last_trade.get(asset_key, 999)}/{cooldown_cycles}"
            elif not mc_dd_allowed:
                dd_val = self.portfolio_tracker.status().get("drawdown_pct", 0)
                safety_skip_reason = f"mc_dd: {dd_val:.1f}% >= {self.mc_dd_threshold:.1f}%"
            else:
                # Kill switches (consecutive losses, max positions, daily loss, stale prices)
                ks_allowed, ks_reason = self._kill_switch_allows_entry(asset_key, strategy)
                if not ks_allowed:
                    safety_skip_reason = f"kill_switch: {ks_reason}"
                else:
                    # Portfolio DD gate
                    portfolio_allowed, portfolio_reason = self.portfolio_tracker.allow_entry()
                    if not portfolio_allowed:
                        safety_skip_reason = f"portfolio_dd: {portfolio_reason}"
                    else:
                        # Correlation gate
                        if len(self.positions) > 1:
                            open_keys = [k for k, v in self.positions.items() if v is not None]
                            if len(open_keys) >= 2 and asset_key not in open_keys:
                                corr_allowed, corr_reason = correlation_allows_entry(
                                    asset_key, self.correlations, self.positions,
                                    max_same_side_alt=2, correlation_threshold=0.70,
                                )
                                if not corr_allowed:
                                    safety_skip_reason = f"correlation: {corr_reason}"

            if safety_skip_reason:
                self._log_skipped_setup(asset_key, current_price, safety_skip_reason, rsi, effective_threshold)
                if rsi is not None:
                    rsi_display = f"{rsi:.1f} < {effective_threshold}" if rsi < effective_threshold else f"{rsi:.1f} ≥ {effective_threshold}"
                    print(f"  {asset_key}: {rsi_display} → SAFETY BLOCKED: {safety_skip_reason}")
                else:
                    print(f"  {asset_key}: RSI=N/A → SAFETY BLOCKED: {safety_skip_reason}")
            else:
                # ── All safety gates passed → compute confidence score ──
                confidence = self._compute_confidence_score(
                    asset_key, rsi, effective_threshold, candles, strategy,
                    hurst, hurst_mode, btc_allowed, fng_allowed,
                )
                cs = confidence["score"]
                decision = confidence["decision"]
                comp = confidence["components"]

                if decision == "full" or decision == "half":
                    # Log evaluator features for analysis (informational only — not a gate)
                    eval_allowed, eval_reason, eval_features = self._evaluate_entry(asset_key, candles, strategy)
                    eval_noted = not eval_allowed
                    if eval_noted:
                        self._log_evaluator_flag(asset_key, current_price, eval_reason, rsi, effective_threshold, eval_features, score_override=True)
                    else:
                        self._log_evaluator_pass(asset_key, current_price, rsi, effective_threshold, eval_features)

                    # Position sizing with confidence-based scaling
                    position_size_r = self._calc_position_size(asset_key, candles, strategy)
                    if decision == "half":
                        position_size_r *= 0.5

                    ctx = self._current_market_context()
                    signal_label = "rsi_oversold" if decision == "full" else "rsi_oversold_half"
                    confidence_score = confidence["score"] if decision in ("full", "half") else None
                    self._open_position(asset_key, current_price, position_size_r, signal_label, price_data, ctx, confidence_score)

                    print(f"  {asset_key}: RSI={rsi:.1f} < {effective_threshold} → ✅ ENTRY ({decision}, score={cs:.3f}, rsi={comp['rsi']:.2f} vol={comp['volume']:.2f} regime={comp['regime']:.2f} adx={comp['adx']:.2f})")
                else:
                    self._log_skipped_setup(asset_key, current_price, f"low_confidence:{cs:.3f}", rsi, effective_threshold, confidence)
                    print(f"  {asset_key}: RSI={rsi:.1f} < {effective_threshold} → ⏸️ LOW CONFIDENCE (score={cs:.3f}, below 0.55, comp: rsi={comp['rsi']:.2f} vol={comp['volume']:.2f} regime={comp['regime']:.2f})")

        # ── 8. Exit check for existing position ──
        existing = self.positions.get(asset_key)
        if existing and current_price > 0:
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
                self._close_position(asset_key, current_price, pnl_pct, "stop_loss", price_data, ctx)
            else:
                # ── 8b. Update chandelier high-water mark ──
                existing["chandelier_high"] = max(existing.get("chandelier_high", entry_price), current_price)

                # ── 8c. Scale-out TP1 (50% at 20 EMA reversion) ──
                if not existing.get("scaled_out", False) and len(candles) >= 22:
                    ema_value = self._calc_ema_value([c["close"] for c in candles], period=20)
                    prev_close = candles[-2]["close"]
                    if ema_value is not None and prev_close < ema_value and current_price >= ema_value:
                        # Price crossed above 20 EMA — scale out 50%
                        ctx = self._current_market_context()
                        self._close_position(asset_key, current_price, pnl_pct, "scale_out_tp1", price_data, ctx, partial=True)
                        print(f"  {asset_key}: ✅ SCALE OUT 50% @ {current_price:.4f} (EMA reversion)")

                # ── 8d. Chandelier trailing stop on remaining position ──
                if existing.get("scaled_out", False):
                    ch_mult = strategy.get("chandelier_mult_alts", 4.0)
                    if "BTC" in asset_key or "ETH" in asset_key:
                        ch_mult = strategy.get("chandelier_mult_major", 2.5)
                    chandelier = self._calc_chandelier_exit(candles, existing["chandelier_high"], ch_mult)
                    if chandelier is not None and current_price < chandelier:
                        ctx = self._current_market_context()
                        remaining_pnl = ((current_price - entry_price) / entry_price) * 100
                        self._close_position(asset_key, current_price, remaining_pnl, "chandelier_exit", price_data, ctx)
                        print(f"  {asset_key}: 🔚 CHANDELIER EXIT @ {current_price:.4f} (trailed from {existing['chandelier_high']:.4f})")

        # ── 8e. Check optimizer readiness (dormant until 200+ trades) ──
        self._check_optimizer_ready(asset_key)

        # ── 9. Increment cooldown counter ──
        self.cycles_since_last_trade[asset_key] = self.cycles_since_last_trade.get(asset_key, 0) + 1

    def _calc_rsi(self, closes: List[float], period: int = 14) -> Optional[float]:
        """Calculate RSI from a list of close prices."""
        if len(closes) < period + 1:
            return None

        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
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
        self, asset_key: str, rsi: Optional[float], effective_threshold: float,
        candles: list, strategy: dict, hurst: dict, hurst_mode: str,
        btc_allowed: bool, fng_allowed: bool,
    ) -> dict:
        """Compute a weighted confidence score for entry decisions.

        Components (each 0-1):
          0.25 * RSI_score        — how well RSI aligns with strategy mode
          0.20 * volume_score     — volume confirmation
          0.15 * lower_low_score  — absence of lower-low cascade
          0.12 * candle_pos_score  — positioning within candle range
          0.13 * regime_score     — Hurst/CUSUM regime alignment
          0.15 * adx_score        — ADX favors mean-reversion (low = good)

        Returns:
          {"score": 0.0-1.0, "components": {...}, "decision": "full"/"half"/"none"}
        """
        # Default weights
        w_rsi = 0.25
        w_vol = 0.20
        w_ll = 0.15
        w_pos = 0.12
        w_reg = 0.13
        w_adx = 0.15

        # ── 1. RSI Score ──
        rsi_score = 0.0
        if rsi is not None:
            if hurst_mode == "trend":
                # Trend mode: high RSI = momentum strength
                rsi_score = min(1.0, max(0.0, (rsi - 55) / 20))
            else:
                # MR mode (default): low RSI = oversold opportunity
                rsi_score = min(1.0, max(0.0, (effective_threshold - rsi) / 10))

        # ── 2. Volume Score ──
        volume_score = 0.5  # default neutral when data unavailable
        volume_available = False
        if len(candles) >= 2:
            volumes = [c.get("volume", 0) or 0 for c in candles]
            recent_volumes = volumes[-11:] if len(volumes) >= 11 else volumes
            # yfinance doesn't provide crypto volume (all zeros) — detect this
            has_real_volume = any(v > 0 for v in recent_volumes[:-1] if v > 0)  # check prior candles
            if has_real_volume and volumes[-1] > 0:
                avg_vol = sum(recent_volumes[:-1]) / max(len(recent_volumes) - 1, 1)
                if avg_vol > 0:
                    vol_ratio = volumes[-1] / avg_vol
                    volume_score = min(1.0, vol_ratio / 2.0)
                    volume_available = True

        # ── 3. Lower-Low Score (absence of cascade = good) ──
        lower_low_score = 1.0
        evaluator = strategy.get("evaluator", {})
        if evaluator.get("enabled", True) and len(candles) >= 4:
            cascade_check = evaluator.get("lower_low_cascade", 3)
            lows = [c["low"] for c in candles]
            if len(lows) >= cascade_check + 1:
                recent_lows = lows[-(cascade_check + 1):]
                is_cascading = all(recent_lows[i] > recent_lows[i + 1] for i in range(cascade_check))
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
                candle_pos_score = max(0.0, 1.0 - pos * 1.5)  # 0.0 pos→1.0, 0.5 pos→0.25, 0.67+→0.0

        # ── 5. Regime Score ──
        regime_score = 0.5  # default neutral
        if hurst_mode == "trend":
            regime_score = 0.8 if hurst.get("active") else 0.5
        elif hurst_mode == "mean_reversion":
            regime_score = 0.8 if hurst.get("active") else 0.6
        elif hurst_mode == "random_walk":
            regime_score = 0.2

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
        }
        raw_score = (
            w_rsi * rsi_score
            + w_vol * volume_score
            + w_ll * lower_low_score
            + w_pos * candle_pos_score
            + w_reg * regime_score
            + w_adx * adx_score
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
        self, asset_key: str, price: float, reason: str,
        rsi: Optional[float], threshold: float,
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

    def _open_position(self, asset_key: str, price: float, size_r: float,
                       signal: str, price_data: dict, market_ctx: dict,
                       confidence_score: Optional[float] = None):
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
            "scaled_out": False,         # TP1 (50% at EMA reversion) taken?
            "chandelier_high": price,    # highest price since entry (for trailing)
        }
        self.positions[asset_key] = position
        print(f"📈 {asset_key}: OPEN at {price:.6f} | signal={signal} | size_r={size_r}")

    def _close_position(self, asset_key: str, price: float, pnl_pct: float,
                        reason: str, price_data: dict, market_ctx: dict = None,
                        partial: bool = False):
        """Close a paper position and log the trade.
        
        If partial=True, closes 50% of position (scale-out TP1) and keeps remaining open.
        Otherwise closes the full position.
        """
        position = self.positions.get(asset_key)
        if not position:
            return

        # Merge entry market context with exit context
        entry_ctx = position.get("market_context", {})
        exit_ctx = market_ctx or self._current_market_context()
        full_ctx = {**entry_ctx, **{"exit_" + k: v for k, v in exit_ctx.items()}}

        close_size = position["position_size_r"] * (0.5 if partial else 1.0)

        trade = {
            "asset": asset_key,
            "entry_price": position["entry_price"],
            "exit_price": price,
            "entry_time": position["entry_time"],
            "exit_time": datetime.now(timezone.utc).isoformat(),
            "pnl_pct": round(pnl_pct, 4),
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
            position["position_size_r"] = close_size  # reduced to remaining 50%
            position["scaled_out"] = True
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
        print(f"📉 {asset_key}: {label} at {price:.6f} | PnL: {pnl_pct:+.2f}% | reason={reason}")

        # ── Track daily PnL (Step 5: Kill Switches) ──
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.last_daily_reset:
            self.daily_pnl_pct = 0.0
            self.last_daily_reset = today
        self.daily_pnl_pct = (self.daily_pnl_pct or 0) + pnl_pct

        # ── Track consecutive losses (full closes only) ──
        if not partial:
            if pnl_pct < 0:
                self.consecutive_losses[asset_key] = self.consecutive_losses.get(asset_key, 0) + 1
                # P3: Auto-pause check
                strategy = self._load_strategy(asset_key)
                ks = strategy.get("kill_switches", {})
                max_cl = ks.get("max_consecutive_losses", 5)
                if self.consecutive_losses[asset_key] >= max_cl and asset_key not in self.paused_assets:
                    self.paused_assets[asset_key] = datetime.now(timezone.utc).isoformat()
                    print(f"🔴 {asset_key}: AUTO-PAUSED after {self.consecutive_losses[asset_key]} consecutive losses")
            else:
                # Winning trade — reset streak and unpause
                was_paused = asset_key in self.paused_assets
                self.consecutive_losses[asset_key] = 0
                self.paused_assets.pop(asset_key, None)
                if was_paused:
                    print(f"🟢 {asset_key}: Unpaused after winning trade")

        # ── Phase 6: Update portfolio tracker ──
        self.portfolio_tracker.update([{"pnl_pct": pnl_pct}])

        # ── Track paper balance in dollars ──
        account_pnl_dollars = pnl_pct / 100 * close_size * self.paper_balance
        self.paper_balance += account_pnl_dollars
        print(f"💰 {asset_key}: Paper balance ${self.paper_balance:.2f} (${account_pnl_dollars:+.2f} on this trade)")

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

    def _calc_ema_slope(self, closes: list, period: int = 20, slope_bars: int = 3) -> Optional[float]:
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
                return (False, f"EMA slope {slope:.6f} > {mx} (moderate trend)", features)

        features["atr"] = self._calc_atr(candles, period=tf.get("atr_period", 14))
        return (True, f"ADX {adx:.1f} trend OK", features)

    # ── Step 5: Kill Switches ─────────────────────────────────────────────

    def _kill_switch_allows_entry(self, asset_key: str, strategy: dict) -> tuple:
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
            return (False, f"KILL: daily PnL {self.daily_pnl_pct:+.2f}% <= -{max_loss}%")

        max_stale = ks.get("stale_price_cycles", 5)
        stale_this = self.stale_price_cycles.get(asset_key, 0)
        if stale_this >= max_stale:
            return (False, f"KILL: stale price {stale_this} cycles")

        # P3: Consecutive loss pause
        max_cl = ks.get("max_consecutive_losses", 5)
        cl = self.consecutive_losses.get(asset_key, 0)
        if cl >= max_cl:
            return (False, f"KILL: {cl} consecutive losses (max {max_cl}) — asset paused")

        return (True, "")

    def _check_stale_prices(self, price_data: dict, asset_key: str):
        """Track consecutive cycles with unchanged price."""
        cur = price_data.get("current_price", 0)
        last = self.last_prices.get(asset_key)
        if last is not None and cur == last:
            self.stale_price_cycles[asset_key] = self.stale_price_cycles.get(asset_key, 0) + 1
        else:
            self.stale_price_cycles[asset_key] = 0
        self.last_prices[asset_key] = cur

    # ── Step 6: Position Sizing ───────────────────────────────────────────

    def _calc_position_size(self, asset_key: str, candles: list, strategy: dict) -> float:
        """Dynamic position size = base_r * vol * streak * heat scalars."""
        ps = strategy.get("position_sizing", {})
        if not ps.get("enabled", True):
            return strategy.get("position_size_r", 0.5)

        base_r = ps.get("base_r", 0.5)

        # Volatility scalar — inverse of ATR%
        atr_pct = self._calc_atr(candles, period=ps.get("atr_period", 14))
        if atr_pct and atr_pct > 0:
            base_atr = ps.get("base_atr_pct", 0.02)
            vs = base_atr / atr_pct
            vs = min(max(vs, ps.get("min_vol_scalar", 0.3)), ps.get("max_vol_scalar", 2.0))
        else:
            vs = 1.0

        # Streak scalar — shrink after consecutive losses
        ls = self.consecutive_losses.get(asset_key, 0)
        if ls > 0:
            sr = ps.get("streak_reduction", 0.5)
            ss = max(sr ** ls, ps.get("max_streak_reduction", 0.25))
        else:
            ss = 1.0

        # Heat scalar — proportional allocation cap (replaces old heat scalar)
        # Ensures total exposure never exceeds max_total_exposure
        oc = sum(1 for p in self.positions.values() if p is not None)
        total_positions_after = oc + 1  # this new position will open
        mte = ps.get("max_total_exposure", 0.8)
        capped_size = mte / total_positions_after if total_positions_after > 0 else mte

        # ── Phase 6: VaR position cap ──
        closes = [c["close"] for c in candles]
        var_pct = compute_var(closes, confidence=0.95)
        if var_pct is not None:
            vr = ps.get("var_risk_fraction", 0.10)
            var_cap = var_position_cap(equity=10000.0, var_pct=var_pct, max_var_exposure=vr)
            self.var_cache[asset_key] = {"var_pct": round(var_pct * 100, 2),
                                         "cap": round(var_cap, 4),
                                         "timestamp": datetime.now(timezone.utc).isoformat()}
        else:
            var_cap = 1.0

        # Apply proportional allocation cap
        raw_size = base_r * vs * ss * var_cap
        # Apply trust-state scaling (unified risk multiplier)
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

    def _calc_chandelier_exit(self, candles: list, high_since_entry: float,
                              multiplier: float = 4.0) -> Optional[float]:
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

    def _load_strategy(self, asset_key: str) -> Optional[Dict]:
        """Load strategy from state file."""
        strat_path = self.state_dir / asset_key / "strategy.yaml"
        if not strat_path.exists():
            default = {
                "version": "01",
                "entry": {"indicator": "rsi", "threshold": 30, "direction": "long"},
                "stop_loss_pct": 2.0,
                "position_size_r": 0.5,
                "cooldown_cycles": 30,
                "btc_gate": {"min_btc_4h_rsi": 25, "min_btc_1h_rsi": 20},
                "fng_gate": {"min_value": 10},
                "evaluator": {
                    "enabled": True,
                    "lower_low_cascade": 3,
                    "volume_spike_mult": 1.5,
                    "min_candle_position": 0.30,
                    "falling_knife_enabled": True,
                    "falling_knife_drop_btc": 2.0,
                    "falling_knife_vol_btc": 2.5,
                    "falling_knife_drop_alt": 3.5,
                    "falling_knife_vol_alt": 4.0,
                },
                "trend_filter": {
                    "enabled": True,
                    "adx_period": 14,
                    "adx_threshold_strong": 30,
                    "adx_threshold_moderate": 20,
                    "ema_period": 20,
                    "ema_slope_period": 3,
                    "max_slope_for_mr": 0.001,
                    "atr_period": 14,
                },
                "kill_switches": {
                    "enabled": True,
                    "max_open_positions": 3,
                    "max_daily_loss_pct": 2.5,
                    "stale_price_cycles": 5,
                    "max_consecutive_losses": 5,
                },
                "position_sizing": {
                    "enabled": True,
                    "base_r": 0.5,
                    "atr_period": 14,
                    "base_atr_pct": 0.02,
                    "min_vol_scalar": 0.3,
                    "max_vol_scalar": 2.0,
                    "streak_reduction": 0.5,
                    "max_streak_reduction": 0.25,
                    "heat_reduction": 0.5,
                },
                "chandelier_mult_major": 2.5,
                "chandelier_mult_alts": 4.0,
                "atr_sl_mult_major": 2.0,
                "atr_sl_mult_alt": 3.0,
                "atr_sl_floor_pct": 1.0,
                "atr_sl_ceiling_pct": 10.0,
            }
            strat_path.parent.mkdir(parents=True, exist_ok=True)
            with open(strat_path, "w") as f:
                yaml.dump(default, f, default_flow_style=False)
            return default
        with open(strat_path) as f:
            return yaml.safe_load(f)

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
        trades = [json.loads(l) for l in trades_file.read_text().strip().split('\n') if l]
        if len(trades) < 5:
            return  # not enough data for meaningful analysis

        # Load previous optimization log for this asset
        opt_file = self.state_dir / asset_key / "optimizer_log.jsonl"
        opt_log = []
        if opt_file.exists():
            opt_log = [json.loads(l) for l in opt_file.read_text().strip().split('\n') if l]

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
            print(f"🧠 {asset_key}: Optimizer {result['status']} "
                  f"(trigger: {trigger.get('reasons',['unknown'])[0]} | "
                  f"trades: {result['trades_available']} | "
                  f"fitness: train={wf.get('train_fitness',{}).get('fitness','?')}/"
                  f"val={wf.get('validate_fitness',{}).get('fitness','?')})")
            for r in recs:
                print(f"   → REC: {r}")
            if bm.get("status") == "analyzed":
                sharpe_ok = "✅" if bm.get("sharpe_met") else "❌"
                wr_ok = "✅" if bm.get("win_rate_met") else "❌"
                dd_ok = "✅" if bm.get("max_dd_met") else "❌"
                print(f"   Benchmarks: Sharpe {bm.get('sharpe','?')} {sharpe_ok} "
                      f"| WR {bm.get('win_rate','?'):.0%} {wr_ok} "
                      f"| DD {bm.get('max_drawdown','?')}% {dd_ok}")
        else:
            print(f"  {asset_key}: Optimizer dormant ({result.get('message', 'waiting for triggers')})")

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
            "total_pnl_pct": round((self.paper_balance - self.initial_balance) / self.initial_balance * 100, 2),
            "positions": positions_summary,
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
            "optimizer": {
                "status": self.optimizer_status.get("status", "unknown"),
                "trades_needed": max(0, 200 - max((self.optimizer_status.get("trades_available", 0) for _ in [1]), default=0)),
            }
        }
        hb_file = self.state_dir / "heartbeat.json"
        with open(hb_file, "w") as f:
            json.dump(heartbeat, f, indent=2)
