"""Replay-mode backtester — feeds historical bars through the same strategy + risk code.

Usage:
    python -m hermes_trading.backtest ETH_USDT
    python -m hermes_trading.backtest ETH_USDT --fee 0.00045

Design:
    - Reuses TradingLoop._cycle() unchanged — same indicators, risk, entry/exit.
    - Replaces live adapters with replay adapters that read from bars.db.
    - Trades are logged to an in-memory results store and summarized at the end.
    - No exchange I/O, no real-time waits, no side effects.
"""

import argparse
import asyncio
import io
import json
import shutil
import sqlite3
import sys
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Replay Adapters ─────────────────────────────────────────────


class ReplayPriceAdapter:
    """Feeds historical bars from bars.db in the same shape as PriceAdapter.fetch().

    Internal index advances on each fetch() call so each bar is seen exactly once
    as the "current" bar, with prior bars as history.
    """

    SCHEMA_VERSION = "replay-v1"

    def __init__(self, asset_key: str, bars: list[dict]):
        self.asset_key = asset_key
        self.bars = bars
        self.idx = 0  # current bar index — advances on each fetch
        self.consecutive_failures = 0  # matches live PriceAdapter interface

    async def fetch(
        self, asset_key: str, timeframe: str = "1m", limit: int = 100
    ) -> dict:
        """Return dict matching PriceAdapter.fetch() output shape.

        Returns { candles, current_price, source }.
        Advances the internal index by one bar.
        """
        if self.idx >= len(self.bars):
            return {
                "schema_version": self.SCHEMA_VERSION,
                "asset": asset_key,
                "current_price": 0.0,
                "candles": [],
                "source": "replay",
                "error": "no more bars",
            }

        current_idx = self.idx
        self.idx += 1

        # Build candle history up to current bar
        window = self.bars[: current_idx + 1]
        # Convert from DB row format to candle dict format
        candles = [
            {
                "timestamp": int(b["timestamp"]) * 1000,  # ms for compatibility
                "open": float(b["open"]),
                "high": float(b["high"]),
                "low": float(b["low"]),
                "close": float(b["close"]),
                "volume": float(b["volume"]),
            }
            for b in window
        ]

        current_bar = self.bars[current_idx]

        return {
            "schema_version": self.SCHEMA_VERSION,
            "asset": asset_key,
            "current_price": float(current_bar["close"]),
            "candles": candles,
            "source": "replay",
            "replay_bar_idx": current_idx,
        }

    @property
    def current_bar_idx(self) -> int:
        return self.idx - 1

    @property
    def progress(self) -> float:
        return self.idx / len(self.bars) if self.bars else 0.0

    async def _close_exchanges(self):
        pass  # no-op


class StubAdapter:
    """Returns empty/neutral data matching the expected adapter schema.

    Schema is validated by hermes_trading.schema.validate_adapter_output(),
    so stubs must return the right field structure even if empty.
    """

    SCHEMA_VERSION = "stub-v1"

    def __init__(self, adapter_type: str = "onchain"):
        """
        Args:
            adapter_type: one of 'onchain', 'news', 'macro', 'price'
        """
        self.adapter_type = adapter_type

    async def fetch(self, asset_key: str = "", *args, **kwargs) -> dict:
        if self.adapter_type == "onchain":
            return {
                "schema_version": self.SCHEMA_VERSION,
                "asset": asset_key,
                "available": False,
                "metrics": {},
            }
        elif self.adapter_type == "news":
            return {
                "schema_version": self.SCHEMA_VERSION,
                "asset": asset_key,
                "available": False,
                "articles": [],
            }
        elif self.adapter_type == "macro":
            return {
                "schema_version": self.SCHEMA_VERSION,
                "asset": asset_key,
                "available": False,
                "indicators": {},
            }
        else:
            return {
                "schema_version": self.SCHEMA_VERSION,
                "asset": asset_key or "unknown",
                "current_price": 0.0,
                "candles": [],
            }

    async def _close_exchanges(self):
        pass


# ── Bar Loader ──────────────────────────────────────────────────


def load_bars(asset_key: str, db_path: Path, limit: Optional[int] = None) -> list[dict]:
    """Load 1m bars from bars.db for one asset, in chronological order."""
    if not db_path.exists():
        print(f"❌ bars.db not found at {db_path}")
        return []

    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row  # dict-like rows
    query = "SELECT * FROM bars WHERE asset = ? ORDER BY timestamp ASC"
    params = [asset_key]
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    rows = db.execute(query, params).fetchall()
    db.close()

    if not rows:
        print(f"❌ No bars for {asset_key} in {db_path}")
        return []

    bars = [dict(r) for r in rows]
    print(f"📂 Loaded {len(bars)} bars for {asset_key}")
    return bars


# ── Backtest Result Collector ───────────────────────────────────


class BacktestResult:
    """Collects trades and per-bar state during a replay run."""

    def __init__(self):
        self.trades: list[dict] = []
        self.equity_curve: list[dict] = []
        self.start_balance: float = 0.0
        self.end_balance: float = 0.0
        self.n_trades: int = 0
        self.n_wins: int = 0
        self.n_losses: int = 0
        self.max_drawdown_pct: float = 0.0
        self.total_fees: float = 0.0

    def add_trade(self, trade: dict):
        self.trades.append(trade)
        self.n_trades += 1
        if trade.get("net_pnl_dollars", 0) > 0:
            self.n_wins += 1
        elif trade.get("net_pnl_dollars", 0) < 0:
            self.n_losses += 1
        self.total_fees += trade.get("fee_dollars", 0)

    def add_equity_point(self, timestamp: int, balance: float):
        self.equity_curve.append({"ts": timestamp, "balance": round(balance, 2)})

    def finalize(self):
        self.end_balance = self.equity_curve[-1]["balance"] if self.equity_curve else 0
        self.start_balance = self.equity_curve[0]["balance"] if self.equity_curve else 0

        # Compute max drawdown from equity curve
        peak = self.start_balance
        for pt in self.equity_curve:
            if pt["balance"] > peak:
                peak = pt["balance"]
            dd = (peak - pt["balance"]) / peak * 100 if peak > 0 else 0
            if dd > self.max_drawdown_pct:
                self.max_drawdown_pct = dd

    def summary(self) -> dict:
        self.finalize()
        total_pnl = self.end_balance - self.start_balance
        total_pnl_pct = (total_pnl / self.start_balance * 100) if self.start_balance > 0 else 0.0
        win_rate = (self.n_wins / self.n_trades * 100) if self.n_trades > 0 else 0.0

        # Simple Sharpe (daily risk-free = 0, using per-trade returns)
        returns = [t.get("net_pnl_dollars", 0) / max(t.get("entry_value", 1), 1) for t in self.trades]
        avg_ret = sum(returns) / len(returns) if returns else 0
        std_ret = (sum((r - avg_ret) ** 2 for r in returns) / len(returns)) ** 0.5 if returns else 1
        sharpe = (avg_ret / std_ret) * (252 * 1440) ** 0.5 if std_ret > 0 else 0.0  # annualized for 1m bars

        return {
            "start_balance": round(self.start_balance, 2),
            "end_balance": round(self.end_balance, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "n_trades": self.n_trades,
            "win_rate_pct": round(win_rate, 1),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "sharpe_annualized": round(sharpe, 2),
            "total_fees": round(self.total_fees, 4),
            "n_wins": self.n_wins,
            "n_losses": self.n_losses,
        }

    def print_summary(self):
        s = self.summary()
        print("\n" + "=" * 50)
        print("BACKTEST RESULTS")
        print("=" * 50)
        print(f"  Start balance:    ${s['start_balance']:>8.2f}")
        print(f"  End balance:      ${s['end_balance']:>8.2f}")
        print(f"  Total PnL:        ${s['total_pnl']:>+8.2f} ({s['total_pnl_pct']:+.2f}%)")
        print(f"  Trades:           {s['n_trades']}")
        print(f"  Win rate:         {s['win_rate_pct']:.1f}% ({s['n_wins']}W / {s['n_losses']}L)")
        print(f"  Max drawdown:     {s['max_drawdown_pct']:.2f}%")
        print(f"  Sharpe (ann.):    {s['sharpe_annualized']:.2f}")
        print(f"  Total fees:       ${s['total_fees']:.4f}")
        print("=" * 50)


# ── Main Replay Driver ──────────────────────────────────────────


async def run_backtest(
    asset_key: str = "ETH_USDT",
    start_balance: float = 1000.0,
    bars_db: str = "data/bars.db",
    fee_rate: float = 0.00025,  # Hyperliquid taker
) -> BacktestResult:
    """Run replay-mode backtest for one asset.

    Loads historical bars, feeds them through the same TradingLoop._cycle()
    code path used in paper/live, collects trades, and returns a summary.
    """
    from hermes_trading.loop import TradingLoop

    # Resolve paths
    base_dir = Path.cwd()
    db_path = Path(bars_db)
    if not db_path.is_absolute():
        db_path = base_dir / db_path

    # Load bars
    bars = load_bars(asset_key, db_path)
    if not bars:
        print("No data to backtest.")
        return BacktestResult()

    # Build a minimal goal/config for the asset
    goal = {
        "key": asset_key,
        "name": asset_key.replace("_", "/"),
        "asset_type": "crypto",
        "timeframe": "1m",
    }

    # Instantiate TradingLoop in backtest mode
    assets = [goal]
    state_dir = base_dir / "backtest_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    
    loop = TradingLoop(
        assets=assets,
        state_dir=state_dir,
        base_dir=base_dir,
        mode="paper",
        initial_balance=start_balance,
        replay_mode=True,
    )

    # Override paper balance tracking for clean start
    loop.paper_balance = start_balance
    loop.initial_balance = start_balance

    # Create replay and stub adapters
    replay_price = ReplayPriceAdapter(asset_key, bars)
    stub_onchain = StubAdapter("onchain")
    stub_news = StubAdapter("news")
    stub_macro = StubAdapter("macro")

    # Load strategy config — use existing state strategy or fallback
    strategy = None
    real_state_dir = base_dir / "state"
    strat_path = real_state_dir / asset_key / "strategy.yaml"
    if strat_path.exists():
        import yaml
        with open(strat_path) as f:
            strategy = yaml.safe_load(f)
        if strategy:
            strategy["_asset"] = asset_key
            # Manually validate version
            ver = strategy.get("version", 0)
            from hermes_trading.loop import TradingLoop
            expected = getattr(TradingLoop, "STRATEGY_SCHEMA_VERSION", 22)
            if ver and int(ver) < expected:
                print(f"  ⚠️  {asset_key}: strategy v{ver} < current v{expected}")
            elif not ver:
                print(f"  ⚠️  {asset_key}: no version field")
            print(f"📖 Loaded strategy from {strat_path}")

    if not strategy:
        # Build a basic MR strategy from defaults
        strategy = {
            "rsi": {"oversold_threshold": 27, "period": 14},
            "position_sizing": {"enabled": True, "r_base": 0.01, "atr_period": 14},
            "kill_switches": {"enabled": True, "max_open_positions": 2, "max_daily_loss_pct": 2.5},
            "confidence": {"enabled": True, "threshold_full": 0.60, "threshold_half": 0.55},
            "trend_filter": {"enabled": True, "adx_period": 14},
            "hurst": {"enabled": True, "min_bars": 500, "mr_threshold": 0.45, "trend_threshold": 0.55},
            "macro": {"enabled": False},
        }
        print("⚠️  Using fallback strategy config (no strategy.yaml for this asset)")

    # Save strategy to temp state dir so _close_position can reload it
    strat_dir = loop.state_dir / asset_key
    strat_dir.mkdir(parents=True, exist_ok=True)
    import yaml as yaml_save
    with open(strat_dir / "strategy.yaml", "w") as f:
        yaml_save.dump(strategy, f)

    # ── Variant overrides ──
    # After strategy is loaded, apply any CLI overrides
    try:
        import __main__
        if hasattr(__main__, 'FORCE_MR') and __main__.FORCE_MR:
            strategy.setdefault("hurst", {})["block_on_trending"] = False
            strategy.setdefault("rsi", {})["oversold_threshold"] = 27
            print("🔧 Force MR override: block_on_trending=False, rsi.oversold_threshold=27")
            # Re-save with overrides
            with open(strat_dir / "strategy.yaml", "w") as f:
                yaml_save.dump(strategy, f)
    except (ImportError, AttributeError):
        pass

    # Results collector
    results = BacktestResult()

    print(f"\n🚀 Running backtest for {asset_key} on {len(bars)} bars...")
    print(f"   Start balance: ${start_balance:.2f}")
    print(f"   Fee rate: {fee_rate*100:.3f}%")
    print(f"   Strategy bars: 1m")

    # Log starting equity
    results.add_equity_point(bars[0]["timestamp"], start_balance)

    # Main replay loop — feed every bar through the exact same _cycle() code
    while replay_price.idx < len(bars):
        idx = replay_price.current_bar_idx

        # Run one cycle — exact same code path as paper/live, but silent
        # (cycle prints per-bar diagnostics via print() — suppress those)
        with redirect_stdout(io.StringIO()):
            await loop._cycle(
                asset_key,
                goal,
                strategy,
                replay_price,  # instead of price_adapter
                stub_onchain,  # instead of onchain_adapter
                stub_news,  # instead of news_adapter
                stub_macro,  # instead of macro_adapter
            )

        # Track equity after each cycle
        balance = loop.paper_balance
        results.add_equity_point(bars[idx]["timestamp"], balance)

        # Progress indicator every 200 bars
        if idx > 0 and idx % 200 == 0:
            print(f"   {idx}/{len(bars)} bars (ETA: ~{idx} bars) — equity: ${balance:.2f}")

    # Force-close any remaining open MR positions at the last bar price
    for pos_key in list(loop.positions.keys()):
        pos = loop.positions[pos_key]
        if pos is None:
            continue
        last_bar = bars[-1]
        final_price = float(last_bar["close"])
        pnl_pct = (final_price - pos["entry_price"]) / pos["entry_price"] * 100
        loop._close_position(
            pos_key,
            final_price,
            pnl_pct,
            "backtest_end",
            {"candles": [], "source": "replay"},
        )
        # Update equity after close
        results.add_equity_point(last_bar["timestamp"], loop.paper_balance)

    # Force-close any remaining trend positions
    for pos_key in list(loop.trend_positions.keys()):
        pos = loop.trend_positions[pos_key]
        if pos is None:
            continue
        last_bar = bars[-1]
        final_price = float(last_bar["close"])
        pnl_pct = (final_price - pos["entry_price"]) / pos["entry_price"] * 100
        position = {
            "asset": pos_key,
            "entry_price": pos["entry_price"],
            "entry_time": pos.get("entry_time", ""),
        }
        loop.positions[pos_key] = position  # temporarily move to MR slot for close
        loop._close_position(
            pos_key,
            final_price,
            pnl_pct,
            "backtest_end_trend",
            {"candles": [], "source": "replay"},
        )
        loop.positions[pos_key] = None
        loop.trend_positions[pos_key] = None

        results.add_equity_point(last_bar["timestamp"], loop.paper_balance)

    # Collect trades from the trade logger
    trades_file = loop.state_dir / asset_key / "trades.jsonl"
    if trades_file.exists():
        with open(trades_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        trade = json.loads(line)
                        results.add_trade(trade)
                    except json.JSONDecodeError:
                        pass

    results.finalize()
    results.print_summary()

    # Cleanup temp state_dir
    import shutil
    if loop.state_dir.exists() and "backtest" in str(loop.state_dir):
        shutil.rmtree(loop.state_dir, ignore_errors=True)

    return results


# ── CLI Entry Point ─────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Hermes replay-mode backtester")
    parser.add_argument("asset", nargs="?", default="ETH_USDT", help="Asset key (e.g. ETH_USDT)")
    parser.add_argument("--balance", type=float, default=1000.0, help="Starting paper balance")
    parser.add_argument("--db", default="data/bars.db", help="Path to bars.db")
    parser.add_argument("--fee", type=float, default=0.00025, help="Taker fee rate")
    args = parser.parse_args()

    asyncio.run(run_backtest(
        asset_key=args.asset,
        start_balance=args.balance,
        bars_db=args.db,
        fee_rate=args.fee,
    ))


if __name__ == "__main__":
    main()
