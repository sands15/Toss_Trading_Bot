# Turtle Rules

This document defines the trading rules that the implementation must preserve.
When this document conflicts with API convenience or implementation shortcuts,
this document wins.

## Rule Priority

1. Position sizing and risk control.
2. Entry and exit rules.
3. Pyramiding and unit limits.
4. Broker execution details.
5. Reporting and operator convenience.

The bot must not place live orders if it cannot prove which rule produced the
order.

## Turtle Principle Gate

Every implementation step must pass this gate before it can be considered done.
If any item fails, the feature may remain in a branch or paper-only mode, but it
must not be used for live trading.

- The feature preserves the rule order in this document.
- The feature does not replace breakout, exit, sizing, stop, skip, or pyramiding
  rules with API convenience behavior.
- The feature can explain which Turtle rule caused each signal, block, order
  intent, or exit.
- The feature has tests for the Turtle rule it touches.
- The feature treats deviations as explicit configuration or explicit blockers,
  never as silent defaults.
- The feature keeps AI, notifications, UI, watchlists, and broker adapters
  outside final trading decisions.
- The feature blocks live mode when required data is missing, stale, ambiguous,
  or not reconciled with broker state.

Implementation review must answer these questions:

1. Which Turtle rule does this change implement or protect?
2. Which Turtle rule could this change accidentally weaken?
3. What test proves the rule still holds?
4. What condition blocks live trading if the rule cannot be evaluated?

The default answer to uncertain live behavior is `block`, not `approximate`.

## Market Scope

Initial scope:

- Stocks only.
- Long-only.
- One market selected at a time: either KR or US.
- No short selling.
- No margin or leverage.
- Quantity-based orders only.

These are deliberate MVP limits, not changes to the Turtle philosophy. They
must be surfaced in reports as scope restrictions.

## Data Basis

Daily bars define the channel and N values.

- `entry_high_20`: highest high of the previous 20 completed daily bars.
- `entry_low_20`: lowest low of the previous 20 completed daily bars.
- `entry_high_55`: highest high of the previous 55 completed daily bars.
- `entry_low_55`: lowest low of the previous 55 completed daily bars.
- `exit_low_10`: lowest low of the previous 10 completed daily bars.
- `exit_low_20`: lowest low of the previous 20 completed daily bars.

The current trading day's partial bar must never be included in channel
calculation.

## N Calculation

N is the volatility unit.

True Range:

```text
TR[t] = max(
  high[t] - low[t],
  abs(high[t] - close[t-1]),
  abs(low[t] - close[t-1])
)
```

Turtle smoothing:

```text
N[t] = (19 * N[t-1] + TR[t]) / 20
```

Allowed MVP fallback: ATR(20), but the config must name it explicitly as
`n_method = atr20`.

All order sizing and pyramiding must use the N value known at the time of the
initial entry unless a later approved design changes this.

## System 1 Entry

System 1 is the 20-day breakout system.

Long entry trigger:

```text
current_price >= previous_20_day_high + minimum_tick
```

System 1 skip rule:

- If the previous System 1 breakout trade for the same symbol was profitable,
  skip the next System 1 signal.
- If the skipped trend continues to a System 2 breakout, System 2 must still be
  taken.
- The bot must persist enough trade history to apply this rule after restart.

## System 2 Entry

System 2 is the 55-day breakout system.

Long entry trigger:

```text
current_price >= previous_55_day_high + minimum_tick
```

System 2 has no skip rule. It is the failsafe for major trends.

## Gap Rule

If the market opens beyond the breakout level, the entry signal is valid at the
open. In live trading this becomes:

1. Detect open price at or beyond the breakout.
2. Build signal with trigger reason `gap_breakout`.
3. Use the open/available price context for order intent.

The bot must distinguish gap entries from intraday crossing entries in logs.

## Exits

For long-only MVP:

- System 1 channel exit: exit if current price trades at or below the previous
  10-day low.
- System 2 channel exit: exit if current price trades at or below the previous
  20-day low.
- Initial risk stop: exit if current price trades at or below `entry_price - 2N`.

Stop and channel exits must be evaluated before new entries.

## Pyramiding

Pyramiding adds to winners only.

- Add one unit every `0.5N` in the profitable direction.
- Use the actual last unit entry price as the base for the next add level.
- Do not add if the position is losing.
- Do not add if there is any unresolved broker/order/state mismatch.
- Maximum per symbol: 4 units.

Example for long:

```text
unit_1 = initial_breakout_price
unit_2 = unit_1_fill_price + 0.5N
unit_3 = unit_2_fill_price + 0.5N
unit_4 = unit_3_fill_price + 0.5N
```

## Unit Sizing

The original concept is that one unit normalizes volatility risk. For live MVP,
use the conservative loss-to-stop formula:

```text
risk_amount = account_equity * risk_pct_per_unit
risk_per_share = stop_n * N
raw_qty = risk_amount / risk_per_share
unit_qty = floor_to_lot(raw_qty)
```

Defaults:

- `risk_pct_per_unit`: 0.005 to 0.01.
- `stop_n`: 2.0.
- `pyramid_step_n`: 0.5.
- `max_units_per_symbol`: 4.
- `max_total_long_units`: 12.

Any quantity reduced by buying power or broker restrictions must be logged as a
broker constraint, not as a strategy decision.

## Event Priority

For each symbol and loop:

1. Reconcile account, holdings, and open orders.
2. Check stop exits.
3. Check channel exits.
4. Check pyramiding.
5. Check System 2 entries.
6. Check System 1 entries and skip rule.

No new entry is allowed while an exit order is pending for the same symbol.

## Backtest Rules

Daily OHLC does not reveal intraday event order. The backtester must use
conservative assumptions:

- If stop/exit and entry/pyramid can both happen in the same bar, process the
  exit first.
- If stop and pyramid can both happen in the same bar, process the stop first.
- If a gap crosses a trigger, use the open as the simulated fill reference.
- Do not assume fills better than the trigger.
- Include commissions, tax model, slippage, and tick-size rounding before
  trusting performance output.

## Live Trading Blockers

Live order placement must remain disabled if any of these are true:

- Toss API credentials are missing.
- Account sequence is unresolved.
- Local position state differs from broker holdings.
- Open orders cannot be listed.
- Previous System 1 trade outcome is unknown for a symbol being evaluated.
- Market calendar or trade window is unknown.
- Current price or orderbook data is stale.
- Tick-size rounding cannot be determined.
- Buying power or sellable quantity check fails.
- A duplicate client order id exists in local DB with unresolved state.
