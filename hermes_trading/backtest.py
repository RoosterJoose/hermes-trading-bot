"""
backtest.py — Phase 5: Backtesting Harness

Replays 1m bar data from SQLite through the same strategy logic as loop.py.
Produces performance metrics: Sharpe, max DD, win rate, profit factor.

Usage:
    uv run python -m hermes_trading.backtest SOL_USDT
    uv run python -m hermes_trading.backtest --all
    uv run python -m hermes_trading.backtest SOL_USDT --report

Same entry/exit logic as the live trading loop (reimplemented for
deterministic replay — no async, no adapters, no noise).
"""

import argparse
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import yaml

BASE_DIR = Path(__file__).parent.parent
BAR_DB_PATH = BASE_DIR / "data" / "bars.db"
STATE_DIR = BASE_DIR / "state"


# ── Pure calc functions (identical to loop.py) ──


def _calc_rsi(closes: list[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(-period, 0)]
    gains = sum(d for d in deltas if d > 0)
    losses = sum(-d for d in deltas if d < 0)
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _calc_ema_value(closes: list[float], period: int = 20) -> Optional[float]:
    if len(closes) < period:
        return None
    multiplier = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def _calc_atr(candles: list[dict], period: int = 14) -> Optional[float]:
    """Calculate ATR as percentage of price."""
    if len(candles) < period + 1:
        return None
    tr_values = []
    for i in range(-period, 0):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_values.append(tr)
    atr = sum(tr_values) / period
    current_price = candles[-1]["close"]
    return atr / current_price if current_price > 0 else None


def _calc_adx(candles: list[dict], period: int = 14) -> Optional[float]:
    """Calculate ADX value."""
    if len(candles) < period + 2:
        return None
    plus_dm = []
    minus_dm = []
    tr_values = []
    for i in range(-period - 1, 0):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_high = candles[i - 1]["high"]
        prev_low = candles[i - 1]["low"]
        prev_close = candles[i - 1]["close"]
        up_move = high - prev_high
        down_move = prev_low - low
        if up_move > down_move and up_move > 0:
            plus_dm.append(up_move)
        else:
            plus_dm.append(0.0)
        if down_move > up_move and down_move > 0:
            minus_dm.append(down_move)
        else:
            minus_dm.append(0.0)
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_values.append(tr)
    if not tr_values or sum(tr_values) == 0:
        return None
    atr_period = sum(tr_values) / period
    if atr_period <= 0:
        return None
    avg_plus_dm = sum(plus_dm) / period
    avg_minus_dm = sum(minus_dm) / period
    di_plus = 100 * avg_plus_dm / atr_period if atr_period > 0 else 0
    di_minus = 100 * avg_minus_dm / atr_period if atr_period > 0 else 0
    dx = (
        abs(di_plus - di_minus) / (di_plus + di_minus) * 100
        if (di_plus + di_minus) > 0
        else 0
    )
    return dx


def _calc_chandelier_exit(
    candles: list[dict], high_water: float, mult: float
) -> Optional[float]:
    """Calculate Chandelier exit level."""
    atr_pct = _calc_atr(candles, 14)
    if atr_pct is None:
        return None
    if len(candles) == 0:
        return None
    current_price = candles[-1]["close"]
    return high_water - atr_pct * mult * current_price


# ── Strategy evaluation (matches _cycle logic) ──


def evaluate_entry(
    candles: list[dict],
    strategy: dict,
    btc_1h_rsi: Optional[float] = None,
    btc_4h_rsi: Optional[float] = None,
) -> tuple[bool, str]:
    """Evaluate whether to enter a position on this bar.

    Returns (should_enter: bool, reason: str).
    """
    closes = [c["close"] for c in candles]
    if len(candles) < 20:
        return False, "insufficient_candles"

    rsi = _calc_rsi(closes)
    if rsi is None:
        return False, "no_rsi"

    entry = strategy.get("entry", {})
    threshold = entry.get("threshold", 30)

    # BTC gate
    btc_gate = strategy.get("btc_gate", {})
    btc_4h_min = btc_gate.get("min_btc_4h_rsi", 25)
    btc_1h_min = btc_gate.get("min_btc_1h_rsi", 20)
    if btc_4h_rsi is not None and btc_4h_rsi < btc_4h_min:
        return False, "btc_4h_gate"
    if btc_1h_rsi is not None and btc_1h_rsi < btc_1h_min:
        return False, "btc_1h_gate"

    # FnG gate (skip in backtest — no historical FnG data)
    fng_gate = strategy.get("fng_gate", {})
    if fng_gate.get("min_value", 10) > 0:
        pass  # Cannot verify historically — allow

    # RSI signal
    if rsi >= threshold:
        return False, f"rsi_{rsi:.1f}_ge_{threshold}"

    # ADX trend filter
    trend = strategy.get("trend_filter", {})
    if trend.get("enabled", True):
        adx = _calc_adx(candles, trend.get("adx_period", 14))
        if adx is not None and adx >= trend.get("adx_threshold_strong", 30):
            return False, f"adx_{adx:.0f}_strong_trend"

    # Entry evaluator checks (simplified — no volume panic in backtest)
    evaluator = strategy.get("evaluator", {})
    if evaluator.get("enabled", True):
        # Lower low cascade
        cascade_check = evaluator.get("lower_low_cascade", 3)
        lows = [c["low"] for c in candles]
        if cascade_check > 0 and len(lows) >= cascade_check + 1:
            recent_lows = lows[-(cascade_check + 1) :]
            if all(recent_lows[i] > recent_lows[i + 1] for i in range(cascade_check)):
                return False, "lower_low_cascade"
        # Candle position
        pos_check = evaluator.get("min_candle_position", 0.30)
        if len(candles) >= 1:
            ch = candles[-1]["high"]
            cl = candles[-1]["low"]
            cr = ch - cl
            if cr > 0:
                pos = (candles[-1]["close"] - cl) / cr
                if pos < pos_check:
                    return False, "low_candle_position"

    return True, "entry_signal"


def manage_exit(
    entry_price: float,
    current_price: float,
    candles: list[dict],
    strategy: dict,
    chandelier_high: float,
    scaled_out: bool,
) -> tuple[bool, str, bool]:
    """Check exit conditions for an open position.

    Returns (should_exit: bool, reason: str, is_partial: bool).
    """
    pnl_pct = ((current_price - entry_price) / entry_price) * 100

    # Stop loss
    stop_loss_pct = strategy.get("stop_loss_pct", 3.0)
    atr_pct = _calc_atr(candles, 14)
    if atr_pct is not None and atr_pct > 0:
        sl_mult = strategy.get("atr_sl_mult_alt", 3.0)
        atr_sl = atr_pct * sl_mult * 100
        sl_floor = strategy.get("atr_sl_floor_pct", 1.0)
        sl_ceiling = strategy.get("atr_sl_ceiling_pct", 10.0)
        stop_loss_pct = min(max(atr_sl, sl_floor), sl_ceiling)

    if pnl_pct < -stop_loss_pct:
        return True, "stop_loss", False

    # Scale-out TP1 (50% at EMA reversion)
    if not scaled_out and len(candles) >= 22:
        closes = [c["close"] for c in candles]
        ema20 = _calc_ema_value(closes, 20)
        if (
            ema20 is not None
            and candles[-2]["close"] < ema20
            and current_price >= ema20
        ):
            return True, "scale_out_tp1", True

    # Chandelier trailing (only after scale-out)
    if scaled_out:
        ch_mult = strategy.get("chandelier_mult_alts", 4.0)
        chandelier = _calc_chandelier_exit(candles, chandelier_high, ch_mult)
        if chandelier is not None and current_price < chandelier:
            return True, "chandelier_exit", False

    return False, "", False


# ── Metrics ──


def compute_metrics(trades: list[dict], initial_equity: float = 10000.0) -> dict:
    """Compute performance metrics from a list of closed trades."""
    if not trades:
        return {
            "total_trades": 0,
            "win_rate": 0,
            "profit_factor": 0,
            "total_pnl_pct": 0,
            "avg_r_multiple": 0,
            "max_drawdown_pct": 0,
            "sharpe_ratio": 0,
            "final_equity": initial_equity,
            "time_range": "N/A",
        }

    wins = [t for t in trades if t.get("pnl_pct", 0) > 0]
    losses = [t for t in trades if t.get("pnl_pct", 0) <= 0]
    total_pnl = sum(t["pnl_pct"] for t in trades)
    win_rate = len(wins) / len(trades) * 100 if trades else 0

    gross_profit = sum(t["pnl_pct"] for t in wins)
    gross_loss = abs(sum(t["pnl_pct"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Simulate equity curve for max DD and Sharpe
    equity = initial_equity
    equity_curve = [equity]
    for t in trades:
        equity *= 1 + t["pnl_pct"] / 100
        equity_curve.append(equity)

    peak = initial_equity
    max_dd = 0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # Daily returns for Sharpe (approximate — each trade as one period)
    returns = [t["pnl_pct"] / 100 for t in trades]
    if len(returns) >= 2:
        sharpe = (
            (np.mean(returns) / np.std(returns, ddof=1)) * math.sqrt(365)
            if np.std(returns, ddof=1) > 0
            else 0
        )
    else:
        sharpe = 0

    avg_r = total_pnl / len(trades) if trades else 0

    # Time range
    times = [t["entry_time"] for t in trades] + [t["exit_time"] for t in trades]
    time_range = f"{datetime.fromtimestamp(min(times)).strftime('%m/%d %H:%M')} → {datetime.fromtimestamp(max(times)).strftime('%m/%d %H:%M')}"

    return {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "total_pnl_pct": round(total_pnl, 2),
        "avg_r_multiple": round(avg_r, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe_ratio": round(sharpe, 2),
        "final_equity": round(equity, 2),
        "best_trade_pct": round(max(t["pnl_pct"] for t in trades), 2) if trades else 0,
        "worst_trade_pct": round(min(t["pnl_pct"] for t in trades), 2) if trades else 0,
        "time_range": time_range,
    }


def format_metrics(m: dict) -> str:
    """Format metrics as a readable report."""
    lines = [
        "═" * 50,
        "  BACKTEST RESULTS",
        "═" * 50,
        f"  Total trades:     {m['total_trades']} ({m['wins']}W / {m['losses']}L)",
        f"  Win rate:         {m['win_rate']}%",
        f"  Profit factor:    {m['profit_factor']}",
        f"  Total PnL:        {m['total_pnl_pct']:+.2f}%",
        f"  Avg R-multiple:   {m['avg_r_multiple']:+.2f}R",
        f"  Max drawdown:     {m['max_drawdown_pct']:.2f}%",
        f"  Sharpe ratio:     {m['sharpe_ratio']:.2f}",
        f"  Best trade:       {m['best_trade_pct']:+.2f}%",
        f"  Worst trade:      {m['worst_trade_pct']:+.2f}%",
        f"  Final equity:     ${m['final_equity']:.2f}",
        "═" * 50,
    ]
    return "\n".join(lines)


# ── Main runner ──


def run_backtest(asset_key: str, timeframe: str = "1m") -> tuple[list[dict], dict]:
    """Run backtest for a single asset using bar data from SQLite.

    Args:
        asset_key: e.g. 'SOL_USDT' or 'XRP_USDT'
        timeframe: '1m' or '1h'

    Returns:
        (trades: list[dict], metrics: dict)
    """
    # Load strategy
    strategy_path = STATE_DIR / asset_key / "strategy.yaml"
    if not strategy_path.exists():
        print(f"  Strategy not found: {strategy_path}")
        return [], compute_metrics([])

    with open(strategy_path) as f:
        strategy = yaml.safe_load(f)

    # Load bars from SQLite (1m or 1h)
    if not BAR_DB_PATH.exists():
        print(f"  Bar DB not found: {BAR_DB_PATH}")
        return [], compute_metrics([])

    table = "bars_1h" if timeframe == "1h" else "bars"
    conn = sqlite3.connect(str(BAR_DB_PATH))
    # Check table exists
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    if table not in tables:
        print(f"  {table} table not found in DB (tables: {', '.join(tables)})")
        conn.close()
        return [], compute_metrics([])
    rows = conn.execute(
        f"SELECT timestamp, open, high, low, close, volume FROM {table} "
        "WHERE asset = ? ORDER BY timestamp ASC",
        (asset_key,),
    ).fetchall()
    conn.close()

    if len(rows) < 30:
        print(f"  {asset_key}: only {len(rows)} bars (need 30+ for backtest)")
        return [], compute_metrics([])

    # Convert to candle dicts
    candles = [
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

    start_time = datetime.fromtimestamp(candles[0]["timestamp"])
    end_time = datetime.fromtimestamp(candles[-1]["timestamp"])
    print(
        f"  {asset_key}: {len(candles)} bars ({start_time:%m/%d %H:%M} → {end_time:%m/%d %H:%M})"
    )

    # Run simulation
    position: Optional[dict] = None
    trades: list[dict] = []
    cooldown_cycles = strategy.get("cooldown_cycles", 30)
    cycles_since_trade = 999

    # Iterate candles in order
    for i, candle in enumerate(candles):
        current_price = candle["close"]
        # Build lookback window (all candles up to current)
        lookback = candles[: i + 1]

        # Skip first 30 bars (need minimum data for indicators)
        if len(lookback) < 30:
            continue

        cycles_since_trade += 1

        if position is None:
            # Check entry
            if cycles_since_trade >= cooldown_cycles:
                should_enter, reason = evaluate_entry(lookback, strategy)
                if should_enter:
                    position = {
                        "entry_price": current_price,
                        "entry_time": candle["timestamp"],
                        "entry_idx": i,
                        "chandelier_high": current_price,
                        "scaled_out": False,
                        "stop_loss_pct": strategy.get("stop_loss_pct", 3.0),
                    }
        else:
            # Update chandelier high
            if current_price > position["chandelier_high"]:
                position["chandelier_high"] = current_price

            # Check exit
            should_exit, exit_reason, is_partial = manage_exit(
                position["entry_price"],
                current_price,
                lookback,
                strategy,
                position["chandelier_high"],
                position["scaled_out"],
            )

            if is_partial and not position["scaled_out"]:
                # Scale out 50% — mark as partial, don't close fully
                position["scaled_out"] = True
                # Record partial close
                pnl = (
                    (current_price - position["entry_price"]) / position["entry_price"]
                ) * 100
                trades.append(
                    {
                        "entry_time": position["entry_time"],
                        "exit_time": candle["timestamp"],
                        "entry_price": position["entry_price"],
                        "exit_price": current_price,
                        "pnl_pct": round(pnl, 2),
                        "reason": exit_reason,
                        "partial": True,
                    }
                )
            elif should_exit:
                pnl = (
                    (current_price - position["entry_price"]) / position["entry_price"]
                ) * 100
                trades.append(
                    {
                        "entry_time": position["entry_time"],
                        "exit_time": candle["timestamp"],
                        "entry_price": position["entry_price"],
                        "exit_price": current_price,
                        "pnl_pct": round(pnl, 2),
                        "reason": exit_reason,
                        "partial": False,
                    }
                )
                position = None
                cycles_since_trade = 0

    # Close any remaining position at last price
    if position is not None:
        pnl = (
            (candles[-1]["close"] - position["entry_price"]) / position["entry_price"]
        ) * 100
        trades.append(
            {
                "entry_time": position["entry_time"],
                "exit_time": candles[-1]["timestamp"],
                "entry_price": position["entry_price"],
                "exit_price": candles[-1]["close"],
                "pnl_pct": round(pnl, 2),
                "reason": "end_of_data",
                "partial": False,
            }
        )

    metrics = compute_metrics(trades)
    return trades, metrics


def main():
    parser = argparse.ArgumentParser(
        description="Backtest trading strategy on bar data"
    )
    parser.add_argument(
        "assets", nargs="*", default=[], help="Asset keys (e.g. SOL_USDT)"
    )
    parser.add_argument(
        "--all", action="store_true", help="Backtest all assets in state dir"
    )
    parser.add_argument(
        "--report", action="store_true", help="Print detailed trade report"
    )
    parser.add_argument(
        "--timeframe",
        default="1m",
        choices=["1m", "1h"],
        help="Bar timeframe (default: 1m)",
    )
    args = parser.parse_args()

    if args.all:
        assets = [
            p.name
            for p in STATE_DIR.iterdir()
            if p.is_dir() and (p / "strategy.yaml").exists()
        ]
    elif args.assets:
        assets = args.assets
    else:
        print("Usage: uv run python -m hermes_trading.backtest SOL_USDT [XRP_USDT ...]")
        print("       uv run python -m hermes_trading.backtest --all")
        sys.exit(1)

    for asset_key in assets:
        trades, metrics = run_backtest(asset_key, timeframe=args.timeframe)
        print(format_metrics(metrics))

        if args.report and trades:
            print("\n  Trade log:")
            print(f"  {'#':4s} {'Entry':12s} {'Exit':12s} {'PnL%':8s} {'Reason':20s}")
            for i, t in enumerate(trades[-20:]):  # last 20
                et = datetime.fromtimestamp(t["entry_time"]).strftime("%H:%M")
                xt = datetime.fromtimestamp(t["exit_time"]).strftime("%H:%M")
                pnl = f"{t['pnl_pct']:+.2f}%"
                part = " (50%)" if t.get("partial") else ""
                print(
                    f"  {i + 1:4d} {et:12s} {xt:12s} {pnl:8s} {t['reason'] + part:20s}"
                )

        print()

    # Aggregate metrics across assets
    if len(assets) > 1:
        all_metrics = []
        for asset_key in assets:
            _, m = run_backtest(asset_key, timeframe=args.timeframe)
            all_metrics.append((asset_key, m))

        print("═" * 50)
        print(f"  CROSS-ASSET SUMMARY  ({args.timeframe})")
        print("═" * 50)
        total_trades = sum(m["total_trades"] for _, m in all_metrics)
        avg_wr = sum(m["win_rate"] for _, m in all_metrics) / len(all_metrics)
        total_pnl = sum(m["total_pnl_pct"] for _, m in all_metrics)
        avg_sharpe = sum(m["sharpe_ratio"] for _, m in all_metrics) / len(all_metrics)
        print(f"  Assets: {', '.join(a for a, _ in all_metrics)}")
        print(f"  Total trades: {total_trades} | Avg WR: {avg_wr:.1f}%")
        print(f"  Combined PnL: {total_pnl:+.2f}% | Avg Sharpe: {avg_sharpe:.2f}")
        print(f"  Time range: {all_metrics[0][1].get('time_range', 'N/A')}")
        print("═" * 50)


if __name__ == "__main__":
    main()
