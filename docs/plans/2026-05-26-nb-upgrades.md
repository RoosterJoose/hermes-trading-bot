# NB-Inspired Trading Bot Upgrades

> **Goal:** Implement all recommendations from NotebookLM's institutional analysis.

## Changes Overview

### P0: Half-Signal Requires Volume Confluence
- **File:** `loop.py` — `_compute_confidence_score()`
- Currently: half-signal fires at score ≥ 0.55
- Change: half-signal also requires volume > 4x avg OR lower-low cascade absence
- Fixes: rsi_oversold_half bleed (-1.82% net, 36.4% WR)

### P1: ADX Hard Gate for MR Entries
- **File:** `loop.py` — main decision chain in `_process_asset()` or `_compute_confidence_score()`
- Currently: ADX is a soft scoring component (13% weight)
- Change: When 1h ADX > 30, block ALL MR entries (not just score penalty)
- Fixes: losses clustered during directional ADX>30 regimes

### P1: Cross-Asset Correlation Deceleration Gate
- **File:** `loop.py` — `_process_asset()` + `risk.py`
- Currently: per-position correlation cap (max 2 β≥0.7)
- Change: When average pair-wise correlation across ALL positions > 0.85, block NEW entries
- Fixes: systemic correlated sell-offs hitting multiple stops simultaneously

### P2: Stop-Loss Audit
- **File:** `reflect.py` summarize output
- Currently: no stop-loss suitability check
- Change: Add analysis of avg stop-loss in ATR terms, flag if < 1.5x ATR

### P2: Enhanced Reflection
- **File:** `reflect.py` — summarize() function
- Add: half-signal autocorrelation check, regime-conditioned splits with ADX/Hurst boundaries, duration-PnL correlation section

---

## Implementation Tasks

### Task 1: Half-Signal Volume Confluence Gate
**Objective:** Require volume spike or LL cascade absence for half-signal decisions

**Modify:** `loop.py` → `_compute_confidence_score()` final decision logic

After weighted score is calculated, but before decision is returned:
- If decision == "half", check:
  - Is volume_available AND volume_ratio >= 4.0? → keep half
  - Is lower_low_score == 1.0 AND cascade_check was positive? → keep half  
  - Else: downgrade to "none"
- Log the downgrade reason in the return dict

This directly addresses NB's: "require strict confluence—such as a Volume Spike (≥4.0x average) or a Lower-Low Cascade—to upgrade it to a valid entry"

### Task 2: ADX Hard Gate
**Objective:** Block MR entries when 1h ADX > 30

**Modify:** `loop.py` — either in `_compute_confidence_score()` as a market_penalty booster, or as a pre-gate in `_process_asset()`

Best approach: Add to market_penalty in `_compute_confidence_score()`:
- If adx_score indicates ADX > 30 (adx_score < 0.0 or very low), add extra market_penalty that pushes below 0.55 threshold
- More cleanly: check in the decision logic after scoring: if adx is very high (ADX > 30), force decision to "none" regardless of score

### Task 3: Cross-Asset Correlation Deceleration Gate
**Objective:** Block new entries when market-wide correlation is dangerously high

**Modify:** `loop.py` → `_process_asset()` entry decision chain + `risk.py`

Add to risk.py:
- Function `market_correlation_allows_entry(positions, correlations) -> bool`
- Compute average Pearson correlation across all open positions
- If average > 0.85, return False with reason

Add to loop.py:
- Call this gate before the confidence score check
- Log to setups_log.jsonl with reason "market_correlation_block"

### Task 4: Enhanced Reflection
**Modify:** `reflect.py` → `summarize()` function

Add these analysis sections to the report:

a) Half-signal trade analysis:
- Filter trades by signal == "rsi_oversold_half"
- Report count, WR, net PnL, avg R of half-signal trades
- If WR < 40% AND net negative, flag: "Half-signal bleeding — consider disabling or requiring volume confluence"

b) Regime-conditioned WR with ADX/Hurst boundaries:
- Categorize trades by ADX regime (from setups_log): <20, 20-25, 25-30, >30
- Report WR and avg PnL per ADX band
- If >30 band shows negative PnL: flag ADX gate recommendation

c) Duration-PnL correlation with action recommendation:
- Already partially present, enhance to match NB's exact framing:
  - "If shorter holds = profit and longer holds = losses: standard MR profile, tighten time exit"
  - "If longer holds = profit: runner capture working, don't tighten time stop"

### Task 5: Stop-Loss ATR Audit in Reflection
**Modify:** `reflect.py` → `summarize()`

- Read current stop_loss_pct from strategy
- Compare to ATR from recent bars (1h or 1m)
- Report: stop is X.Xx ATR
- If < 1.5x ATR: flag "Stop may be too tight for MR — consider widening to 1.5-3.0x ATR per NB recommendation"

---

## Verification

After implementation:
1. Read test the reflection output for BTC_USDT
2. Verify half-signal downgrade logic doesn't break normal scoring
3. Manual code review of gate logic
4. Syntax check all modified files
