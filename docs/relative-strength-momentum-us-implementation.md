# US Relative Strength Momentum Implementation

This strategy is a practical alternative to original Turtle rules for a
market-wide US equity scanner. It ranks the whole available universe, enters
only strong relative winners, and blocks new entries when the broad market is
below trend.

## Default Rules

- Universe: normalized daily US OHLCV CSV files.
- Market filter: `SPY` close must be above its 200-day moving average.
- Candidate filter:
  - close price at least `5`
  - 20-day average traded value at least `50,000,000`
  - symbol close above its 200-day moving average
- Ranking score: 126-trading-day return, excluding the most recent 21 trading
  days.
- Entries: buy up to 2 top-ranked candidates per day until 5 positions are
  open.
- Position size: target 10% of current equity per new position.
- Exit: close the position when symbol close falls below its 75-day moving
  average.

## Backtest Assumptions

- Uses daily close fills for both entries and exits.
- Models Toss US stock trading costs by default:
  - buy commission: `0.1%`
  - sell commission: `0.1%`
  - SEC fee: `0.00206%` of sell notional, minimum `$0.01`
- Does not currently model slippage, borrow cost, spread, market impact, FX
  spread, or annual overseas capital-gains tax.
- Uses current S&P 500 membership proxy data unless a true point-in-time
  universe is supplied by a later data pipeline.
- Designed for research and candidate selection, not direct live order routing.

## Saved Candidate Strategy - 2026-06-14

This is the currently selected non-Turtle candidate for recent US market
conditions:

- Relative-strength momentum universe: current S&P 500 proxy plus `SPY` market
  filter.
- Broad-market gate: only accept new positions when `SPY` is above its 200-day
  moving average.
- Candidate gate: candidate close must be above its 200-day moving average,
  close at least `$5`, and 20-day average traded value at least `$50,000,000`.
- Score: 126-trading-day return excluding the most recent 21 trading days.
- Entry: accept up to 2 top-ranked candidates per day until 5 positions are
  open.
- Sizing: target 10% of current equity per new position.
- Exit: close the position when the candidate closes below its 75-day moving
  average.
- Research capital: `$100,000`.
- Cost model: Toss US stock costs above, equivalent to about `0.2023%`
  round-trip break-even before slippage/tax.

## Current CLI

```powershell
python -m turtle_bot.cli --momentum-backtest `
  --momentum-data-dir data/normalized/us_yahoo `
  --initial-equity 100000 `
  --backtest-report reports/backtest/us-sp500-current-proxy-momentum-20150101-20260612.json
```

The JSON report includes the ordinary backtest summary plus a `momentum` section
with the exact configuration, recommendation days, accepted days, and daily
ranked candidates.

## Paper Runtime

The same strategy can be selected for paper runtime with:

```yaml
strategy:
  kind: momentum
  momentum:
    market_symbol: SPY
    lookback_days: 126
    skip_days: 21
    trend_ma_days: 200
    exit_ma_days: 75
    max_positions: 5
    accept_top_n: 2
    target_position_pct: 0.10
    min_price: 5
    min_average_daily_value: 50000000
    average_daily_value_days: 20
    use_market_filter: true
```

Runtime behavior remains paper-only: it records order intents and simulated
paper fills, but does not submit live broker orders. The runtime ranks all
configured `runtime.symbols`, checks the `SPY` market filter, exits existing
momentum paper positions below the 75-day moving average, blocks same-iteration
re-entry after an exit, and accepts the top-ranked candidates until the
configured position limits are reached.

## Shadow Validation Runtime

The next operational step after paper mode is `runtime.mode: shadow`:

```yaml
runtime:
  mode: shadow
  market: US
  symbols:
    - SPY
    - QQQ
    - SMH

strategy:
  kind: momentum
```

Shadow mode still uses only Toss read-only endpoints. It fetches real market
data, account holdings, and open orders, records `shadow_order_intent` events,
and applies virtual fills to the local paper-position tables so forward
performance can be measured. It does not submit, modify, or cancel broker
orders.

Unlike paper mode, shadow mode treats broker/local reconciliation issues as
warnings. This allows validation on a real account that may already hold
positions outside this bot while still preserving the full broker snapshot in
the runtime event log.

Run one validation heartbeat with:

```powershell
python -m turtle_bot `
  --config config/local.yaml `
  --state-db state/turtle.sqlite3 `
  --log-dir logs `
  --shadow-service `
  --once
```
