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
- Daily-bar backtest engine with CSV loading, single-symbol and portfolio
  loops, Turtle risk-based unit sizing, simulated fills, fees/slippage/tax
  hooks, trade output, equity curve, JSON report export, and audit log.
- Toss OpenAPI read-only client for token issuance, market data, market info,
  accounts, holdings, order lookup, buying power, sellable quantity, and
  commissions. Order create/modify/cancel is intentionally absent.
- Position reconciliation that compares local Turtle position state with
  broker holdings/open orders and reports blockers before paper/live decisions.
- Paper runtime loop that runs reconcile first, evaluates Turtle signals, and
  records would-be order intents through runtime events and notifications.
- Paper-only guard, fill/state simulator, repeated-iteration scheduler, and
  JSON report export.
- macOS paper-service shell with launchd plist rendering, runtime directory
  setup, operations checks, and a read-only blocked health payload until market
  data wiring is present.
- Toss read-only market-data provider for paper mode. When env credentials,
  `toss.account_seq`, and `runtime.symbols` are configured, the paper service
  fetches candles/prices through read-only endpoints, reconciles account state,
  and then runs the paper Turtle loop.
- Market-calendar gate for the paper service. The service checks the read-only
  Toss calendar before evaluating paper intents and blocks when the session is
  closed or unknown.
- Premarket watchlist generation inside the paper service. OPEN/PREOPEN
  sessions build and persist a Turtle breakout-distance watchlist, but the
  watchlist remains an operational polling/status artifact, not a trade signal.
- Automatic universe selection through rule-based liquidity, market,
  warning-status, and Turtle-data-readiness filters. AI is reserved for news
  summaries, reports, and situation explanations, not order decisions.
- Postmarket daily report export with runtime event summary, blockers,
  watchlist, paper positions, and latest read-only broker snapshots.
- AI daily report summary adapter for OpenAI-compatible chat-completions APIs,
  configured for `bRadu/gemma-4-E2B-it-textonly` by default. AI output remains
  explanation-only and cannot affect Turtle decisions.

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
