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

## Documentation

- [Turtle Rules](docs/turtle-rules.md)
- [System Architecture](docs/architecture.md)
- [Toss API Contract](docs/toss-api-contract.md)
- [macOS Operations](docs/macos-operations.md)
- [Implementation Plan](docs/implementation-plan.md)

## Official References

- Toss Open API LLM guide: <https://developers.tossinvest.com/llms.txt>
- Toss OpenAPI JSON: <https://openapi.tossinvest.com/openapi-docs/latest/openapi.json>
- Original Turtle Rules PDF: <https://www.tradingwithrayner.com/wp-content/uploads/2014/11/OriginalTurtleRules.pdf>
