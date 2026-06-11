# Toss Trading Bot

Turtle Trading bot for Toss Securities Open API.

The project is designed for 24/7 macOS operation, while keeping the strategy,
backtest, API client, and tests runnable on Windows.

## Non-Negotiable Principle

Turtle Trading rules come first. API convenience must not change the trading
rules silently. If an implementation cannot preserve a rule, it must mark the
behavior as an explicit deviation and block live trading until approved.

## Current Direction

- Primary runtime: macOS daemon managed by `launchd`
- Sleep prevention: Amphetamine or equivalent macOS power policy
- Development and tests: macOS and Windows
- Strategy MVP: long-only stock trading, intraday breakout detection from
  previous-day channels, no discretionary AI order decisions
- Broker: Toss Securities Open API behind adapter interfaces
- First safe modes: backtest, read-only live sync, paper trading

## Current Implementation

- Turtle strategy core with Decimal-safe candles, Donchian channels, Turtle N,
  System 1/System 2 entries, exits, stops, skip state, and 0.5N pyramiding.
- API-free operational primitives: rate-limit queue, market data cache,
  watchlist builder, notifier, read-only health payload/server, runtime shell,
  and SQLite state store.
- Daily-bar single-symbol backtest engine with CSV loading, simulated fills,
  fees/slippage/tax hooks, trade output, equity curve, and audit log.

## Documentation

- [Turtle Rules](docs/turtle-rules.md)
- [System Architecture](docs/architecture.md)
- [Toss API Contract](docs/toss-api-contract.md)
- [macOS Operations](docs/macos-operations.md)
- [Implementation Plan](docs/implementation-plan.md)
- [Reference Project Notes](docs/reference-project-notes.md)

## Official References

- Toss Open API LLM guide: <https://developers.tossinvest.com/llms.txt>
- Toss OpenAPI JSON: <https://openapi.tossinvest.com/openapi-docs/latest/openapi.json>
- Original Turtle Rules PDF: <https://www.tradingwithrayner.com/wp-content/uploads/2014/11/OriginalTurtleRules.pdf>
