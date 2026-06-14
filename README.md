# Toss Trading Bot

Toss Securities Open API trading research and validation bot.

The project started with original Turtle Trading rules and now also includes a
US relative-strength momentum strategy, survivorship-bias controls, Toss
read-only account/market integration, paper trading, and shadow validation.
Shadow mode uses real Toss read-only data and virtual fills, but still has no
live order submission path.

The codebase is designed for 24/7 macOS operation, while keeping the strategy,
backtest, API client, setup scripts, and tests runnable on Windows.

## Non-Negotiable Principle

Turtle Trading rules come first. API convenience must not change the trading
rules silently. If an implementation cannot preserve a rule, it must mark the
behavior as an explicit deviation and block live trading until approved.

## Current Direction

- Primary runtime: macOS daemon managed by `launchd`
- Sleep prevention: Amphetamine or equivalent macOS power policy
- Development and tests: macOS and Windows
- Strategy scope: original Turtle rules plus saved US relative-strength
  momentum strategy
- Broker: Toss Securities Open API behind adapter interfaces
- Safe modes: backtest, read-only sync, paper trading, and shadow validation
- Live broker order submission is intentionally not implemented yet
- AI is explanation-only and cannot select symbols, change sizing, or submit
  orders

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
- Shadow validation mode for the step after paper trading. It uses real
  read-only Toss account/market data, records shadow order intents and virtual
  fills, tolerates unrelated broker holdings as warnings, and still has no live
  order submission path.
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
- Model-agnostic AI explanation boundary with `NullAiClient` and
  `OpenAICompatibleAiClient`. It can summarize news, daily reports, runtime
  events, and situations through `/v1/chat/completions`. AI output remains
  explanation-only and cannot affect Turtle decisions. On macOS, a local MLX
  int4 server can sit behind the same API boundary later.

## Documentation

- [Setup](docs/setup.md)
- [Turtle Rules](docs/turtle-rules.md)
- [System Architecture](docs/architecture.md)
- [Toss API Contract](docs/toss-api-contract.md)
- [macOS Operations](docs/macos-operations.md)
- [Implementation Plan](docs/implementation-plan.md)
- [Reference Project Notes](docs/reference-project-notes.md)

## Quick Setup

Windows:

```powershell
.\ops\setup-local.ps1
```

macOS or Linux:

```bash
bash ops/setup-local.sh
```

Then fill `config/local.yaml`, set `TOSS_CLIENT_ID` and
`TOSS_CLIENT_SECRET`, and run `--ops-check`. See [Setup](docs/setup.md) for the
full first-run flow.

## Latest Test Result

Last verified locally on 2026-06-14:

```text
python -m pytest -q
148 passed
```

This test run covers the strategy core, long/short backtests, point-in-time
universe filtering, scan and momentum backtests, Toss OpenAPI request/response
compatibility, market-calendar parsing, paper runtime, shadow validation,
setup/config parsing, reports, and state storage.

## Official References

- Toss Open API LLM guide: <https://developers.tossinvest.com/llms.txt>
- Toss OpenAPI JSON: <https://openapi.tossinvest.com/openapi-docs/latest/openapi.json>
- Original Turtle Rules PDF: <https://www.tradingwithrayner.com/wp-content/uploads/2014/11/OriginalTurtleRules.pdf>
