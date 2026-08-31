# macOS Operations

The production target is a macOS machine running continuously.

> **Deployment hold (2026-08-29):** the existing Mac checkout is diverged and
> dirty, and an operating SQLite database failed integrity checking. Do not
> update, install into, or run trading from that checkout. Build a reviewed
> clean integration commit first, transfer that exact SHA into a dedicated
> immutable release directory and virtual environment, and keep live disarmed.
> The isolated Discord worker may receive a synthetic shadow E2E only after the
> same clean-release and Discord ACL gates pass.

> **Synthetic acceptance (2026-08-30):** the isolated exact-SHA approval release
> passed Gateway connection, button/modal interaction, strict private receipt,
> restart, and duplicate-suppression checks without any broker credential, DB
> consumer, or order capability. The temporary LaunchAgent and synthetic runtime
> were removed afterwards; the clean immutable release remains for review. This
> result does not lift the live deployment hold.

> **Current code status (2026-08-30):** `NON_LIVE_CORE_IMPLEMENTED / LIVE_NO_GO`.
> The five checked-in plist files are templates, not proof that exactly five jobs
> are installed. Four component heartbeat producers and the fifth-job watchdog
> evaluator are wired in the templates, but their Mac installation/permission
> acceptance and watchdog Discord delivery are not verified. The
> hash-locked wheelhouse/release staging, macOS syntax/Keychain/UID/no-egress
> acceptance, exhaustive replay/SLO escalation, and an external deadman remain
> release blockers. Do not install or arm a live writer from this checkout.

## Runtime Assumptions

- Amphetamine or equivalent prevents system sleep.
- The machine remains connected to power.
- Network is stable enough for broker polling.
- The process is managed by `launchd`.
- Secrets are not committed to git.

Amphetamine prevents sleep. A per-user LaunchAgent starts only after that user
logs in and the Keychain is available; it can restart a crashed process but does
not run through logout. Verify login recovery after every reboot.

## Python Environment

Recommended:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

The editable install above is for development and shadow verification only.
The future live release is built side-by-side under an exact 40-character SHA,
uses a fully transitive dependency lock plus an offline wheelhouse whose files
and manifest are hash-verified, and performs no `pip install` at service startup.
Source, virtualenv, and every ancestor are root-owned and not writable by runtime
users. Release directories are traversable (`0755`), wrappers are executable
(`0555` or `0755`), and source is `0444/0555`; do not make a root-owned wrapper
`0700`. Config, SQLite/WAL, logs, mailboxes, and Keychain items remain outside
the release. Each plist points directly to that exact SHA path, not a mutable
checkout or `/current` symlink. Editable installs, user site-packages, unhashed
downloads, and range-only dependency declarations fail the release gate.

### Intraday shadow planner source gate

Do not run the intraday planner with the system Python or a virtual environment
created for another checkout. From the intended repository root, verify the
editable import before every deployment/update:

```bash
expected="$(pwd -P)/src/turtle_bot/__init__.py"
actual="$(.venv/bin/python -c 'from pathlib import Path; import turtle_bot; print(Path(turtle_bot.__file__).resolve())')"
test "$actual" = "$expected"
news_expected="$(pwd -P)/src/turtle_news/__init__.py"
news_actual="$(.venv/bin/python -c 'from pathlib import Path; import turtle_news; print(Path(turtle_news.__file__).resolve())')"
test "$news_actual" = "$news_expected"
.venv/bin/python -m pytest
```

Create a separate fail-closed configuration without overwriting an existing
local file:

```bash
test -e config/intraday.local.yaml || cp config/intraday.example.yaml config/intraday.local.yaml
```

Fill the account sequence and every blank intraday risk/market guardrail while
leaving `runtime.symbols` empty; the selector locks exactly one symbol. Then
validate and run one read-only heartbeat:

```bash
.venv/bin/python -m turtle_bot --config config/intraday.local.yaml \
  --state-db state/intraday.sqlite3 --log-dir logs --ops-check
.venv/bin/python -m turtle_bot --config config/intraday.local.yaml \
  --state-db state/intraday.sqlite3 --log-dir logs --shadow-service --once
```

Remove `--once` only after the one-shot check succeeds. The process waits for
the configured premarket plan window and stores at most one immutable plan per
account and US session date. Plan alerts are kept in a SQLite outbox; a missing
or failing Discord webhook leaves them pending for the next service iteration.
It never submits an intraday order in this phase.
Use Tailscale SSH for administration; the Tailscale address is not the public
egress IP that Toss may require in its API allowlist.

### Discord direct approval worker (shadow-only)

This release records operator intent but does not arm trading or submit an
order. The only data path is:

```text
immutable shadow plan -> redacted 0600 envelope -> Discord button + modal
                                               -> one new 0600 receipt
paper summary -> redacted 0600 paper-status.json -> guild-only /현황
```

There is deliberately no edge from the receipt back to the trading DB in this
release. The worker does not import `turtle_bot`, open the trading SQLite file,
load Toss credentials, run a subprocess, or call a broker endpoint.

Install the isolated optional dependency in the reviewed checkout:

```bash
.venv/bin/python -m pip install -e ".[approval]"
chmod 700 ops/run-discord-approval.command
```

That `0700` command applies only to the current user-owned development checkout.
The reviewed root-owned immutable release installs the wrapper as `0555` or
`0755` so its runtime UID can execute but not replace it.

Set `strategy.intraday.approval_envelope_path` to an absolute path outside the
checkout. Use a separate mode-0700 runtime directory and a previously absent
`approval-inbox` directory for receipts. Do not point either path at the
trading DB, logs, configuration, or the news worker state. Also set a short,
non-secret `toss.account_alias`; envelope export fails closed if no account
label can be determined.

Create the private runtime and inbox before loading the plist. The template
writes stdout/stderr directly under this already-existing runtime directory so
`launchd` never depends on an absent intermediate log directory:

```bash
runtime_dir="$HOME/.local/share/toss-trading-bot/approval-runtime"
install -d -m 700 "$runtime_dir" "$runtime_dir/approval-inbox"
```

The local LaunchAgent must provide exactly these non-secret settings:

```text
DISCORD_ALLOWED_GUILD_ID
DISCORD_ALLOWED_CHANNEL_ID
DISCORD_ALLOWED_USER_ID
DISCORD_APPROVAL_ENVELOPE_PATH
DISCORD_APPROVAL_INBOX_DIR
```

All three Discord IDs are single exact decimal snowflakes, not comma-separated
lists. Keep the real values only in the local installed plist; the repository
template contains placeholders. The target channel is the only channel where
the bot may have `VIEW_CHANNEL | SEND_MESSAGES` (`3072`) through a channel
overwrite. Install the bot with server-level permissions `0`; do not grant
administrator, history, message-management, webhook-management, or privileged-intent
permissions. The worker registers only the guild-scoped `/현황` command and checks the
exact user, guild, and channel before reading status or responding. The worker uses an outbound Gateway
connection with intents `0`, so it needs no public inbound port or interaction
HTTP endpoint.

Do not infer exact-channel isolation from OAuth `permissions=0` or from one
target-channel overwrite. Discord category, role, and member inheritance must
be evaluated across the whole guild after every permission change. The release
gate is: target `VIEW_CHANNEL=true` and `SEND_MESSAGES=true`; every other
channel/category has effective View and Send counts `0`; and the bot has no
`ADMINISTRATOR`, `MANAGE_CHANNELS`, or `MANAGE_ROLES`. The 2026-08-29 audit
passed this gate after explicit non-target denies. Re-run it before E2E and
record only redacted counts, never private IDs or the token.

Store the bot token as an application-password item in the login Keychain:

```text
service: TossTradingBot.DiscordApprovalToken
account: discord-approval-bot
```

Never put the token in a command argument, plist, `.env`, config file, log, or
shell history. Metadata can be checked without printing the value:

```bash
security find-generic-password \
  -s TossTradingBot.DiscordApprovalToken \
  -a discord-approval-bot >/dev/null
```

The supplied wrapper refuses arguments and SSH/headless starts, then launches a
single `zsh -f` under an `env -i` allowlist. Only that clean child reads the token
and exports it to the final isolated Python process; the token is never an argv
value, and Python removes the environment entry immediately after validated
configuration capture. The wrapper also uses Python isolated mode and verifies that
`turtle_approval` imports from the same checkout. It pins TLS verification to the
readable Apple-managed `/etc/ssl/cert.pem`; a missing CA bundle is a startup error,
not a reason to disable certificate checks. Keychain access must first be
tested in the logged-in Aqua desktop session; an unlocked login keychain alone
does not prove an SSH Background session can decrypt the item.

Before reading Keychain, the outer wrapper must set `ulimit -c 0`; the clean
child inherits it and Python immediately verifies/sets `RLIMIT_CORE=0`. The
current wrapper must not pass release acceptance until this is implemented and
tested in both wrapper and child. A token-bearing crash must not create a core
file.

This package split is not an OS sandbox. Another process running as the same
macOS UID can access user-owned mode-0600 files and may be able to invoke the
same login Keychain item. Consequently this shadow receipt is an operator audit
record, not a live-trading capability. Before adding any inbox consumer, require
an authenticated provenance boundary unavailable to arbitrary same-UID code
(for example a separate OS identity or an appropriate code-signing/hardware-key
design) and repeat immutable-plan, broker-state, expiry, and risk validation in
the consumer. A same-UID Keychain HMAC alone is insufficient.

Create the installed plist without overwriting an existing job, replace every
placeholder locally, then validate it:

```bash
installed="$HOME/Library/LaunchAgents/com.sands15.toss-discord-approval.plist"
if [[ -e "$installed" ]]; then
  printf '%s\n' 'approval LaunchAgent already exists; inspect it first' >&2
else
  cp ops/launchd/com.sands15.toss-discord-approval.plist.example "$installed"
fi
chmod 600 "$installed"
plutil -lint "$installed"
```

Bootstrap it from the logged-in Mac Terminal, not through SSH:

```bash
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.sands15.toss-discord-approval.plist"
```

Before leaving it unattended, verify all of the following with a fresh shadow
envelope: one plan message appears only in the allowed channel; another
user/channel/server cannot approve it; the allowed user must enter the shown
plan-hash suffix in the modal; expiry or any plan/hash/nonce change is rejected;
one receipt is created with mode 0600; duplicate clicks and a worker restart do
not create a second receipt. The worker must reject and log a symlink, empty,
over-permissive, malformed, or binding-mismatched receipt instead of treating it
as completed. Delete the test receipt afterwards only by its
exact verified path. A receipt is not evidence of a placed order.

Discord requires the initial interaction response within three seconds and
keeps an interaction token valid for 15 minutes, so the button opens its modal
immediately and the modal submission is deferred before filesystem validation.
See the official [interaction callback](https://docs.discord.com/developers/interactions/receiving-and-responding#interaction-callback),
[component](https://docs.discord.com/developers/components/reference),
[modal](https://docs.discord.com/developers/components/using-modal-components),
[Gateway intents](https://docs.discord.com/developers/events/gateway#gateway-intents),
and [permission flags](https://docs.discord.com/developers/topics/permissions#bitwise-permission-flags)
documentation.

### Future live identity and process boundary

Do not promote the current same-UID approval receipt to a live capability. The
live design uses two non-admin macOS users:

| Identity | May read | May write/call | Must not receive |
| --- | --- | --- | --- |
| `toss-trader` | immutable plan, broker data, approved receipt | trading SQLite, Toss order APIs, private heartbeat | Discord bot token, news webhook |
| `toss-approver` | redacted envelope | one-shot receipt, Discord Gateway | Toss credentials, account ID, trading DB |
| `toss-watchdog` | group-readable redacted heartbeat, `launchctl` state | watchdog-only Discord Keychain and one-channel stale/restart alert | Toss/approval/news secrets, trading DB, order code |
| news worker | selected-symbol context, news/LLM | its own dedupe DB and one-channel alert | Toss credentials, receipt inbox, trading DB |

Root creates a non-writable local-filesystem anchor and two cross-UID mailboxes
once. The trader owns an envelope outbox
that the approver can only read; the approver owns a receipt inbox that the
trader can only read. Directories are `0750`, files are `0640`, with exact
owner/read-only peer group and no world permissions. The current same-UID
private-file helper rejects those group bits and therefore must not be reused;
v2 uses fd-relative `openat`/`O_NOFOLLOW`, exact UID/GID/mode and ancestor checks,
local APFS `statfs` validation with ownership enabled, and file+directory fsync.
Any other filesystem is fail-closed pending a separate threat review. The live consumer revalidates the
immutable plan hash, every displayed economic value, nonce, approval generation,
allowed Discord user/guild/channel, expiry, current broker state, and risk gates
before a one-time SQLite CAS that uniquely stores both receipt hash and Discord
interaction ID. A reboot before entry invalidates the old receipt
and requires a new approval generation. A reboot after a fill resumes only
protection and exit without waiting for approval.

No-clobber publish links a completed temp inode to the final name and then
unlinks the temp. A crash between those calls leaves `nlink=2`; the approver
alone may recover it after verifying that temp/final are the same expected inode
and fsyncing the directory. The trader never cleans the peer directory. The
consumer accepts only `nlink=1`, treats `nlink=2` as pending for at most 30
seconds/three checks, then alerts and consumes nothing. Release tests inject the
`after_link_before_temp_unlink` crash and malicious hardlink/symlink/wrong-owner
cases.

The trader and approver LaunchAgents require both login sessions and their respective
login Keychains after FileVault unlock. Do not enable automatic login or move
secrets into plist/environment files to make reboot recovery appear automatic.
If two-user login recovery cannot be operated, keep the runtime shadow-only.

Every shadow/approval/news/watchdog wrapper rejects arguments, runs `zsh -f` with
an `env -i` allowlist, sets `umask 077` and `ulimit -c 0`, verifies `python -I`
imports from the exact release, reads only its own Keychain items, and removes
secret environment entries immediately after capture. The checked-in wrappers
implement this boundary; Mac release acceptance still requires `zsh -n`,
dummy-Keychain, core-limit, and exact-release tests on macOS.

The future live release has one account writer process. It combines exactly the
locked symbol's trade/orderbook subscriptions with the account-wide personal
order topic in one WebSocket declaration and performs REST reconciliation before
and after every connect. Stop the separate shadow stream before starting it.
Tailscale SSH is maintenance transport only; neither order management nor the
watchdog depends on an SSH session remaining connected.

The local watchdog cannot report a total Mac power or Internet outage. A live
pilot also needs a deadman on a second always-on node that expects a redacted
heartbeat and alerts the same single Discord channel when it is late. It has no
Toss credentials and cannot submit/cancel orders. If no second node is
available, the deployment must explicitly remain shadow-only rather than claim
outage alerting that the Mac cannot provide while offline.

### Non-live implementation boundary

The current requested milestone explicitly excludes every real-account order
test. Its Mac release may contain a deterministic intraday lifecycle module for
fake-broker tests, but it must not install or dispatch that module with Toss
order credentials. The current verified code milestone is
`NON_LIVE_CORE_IMPLEMENTED / LIVE_NO_GO`; it is not the completed NL1–NL5 release.

The installed process allowlist is exactly:

1. intraday shadow planner;
2. selected-symbol market stream shadow;
3. Discord approval shadow recorder; the offline consumer module is not dispatched;
4. selected-symbol news one-shot worker;
5. trading-unprivileged shadow watchdog under `toss-watchdog`.

Do not install the generic bot plist, a live intraday writer, a receipt consumer,
dashboard, multi-user gateway, Docker dashboard, or a `tailscale serve` listener.
Tailscale SSH remains maintenance transport only.

The checked-in non-live service manifest is intentionally just these five
templates and wrappers:

| label | template | zero-argument wrapper | cadence |
| --- | --- | --- | --- |
| `com.sands15.toss-intraday-shadow` | `com.sands15.toss-intraday-shadow.plist.example` | `run-intraday-shadow.command` | continuous; redacted heartbeat + DB `quick_check` |
| `com.sands15.toss-market-stream-shadow` | `com.sands15.toss-market-stream-shadow.plist.example` | `run-toss-stream.command` | `WatchPaths=news-context.json`; ACK/baseline heartbeat while a plan is active |
| `com.sands15.toss-discord-approval` | `com.sands15.toss-discord-approval.plist.example` | `run-discord-approval.command` | continuous recorder + heartbeat |
| `com.sands15.toss-news-shadow` | `com.sands15.toss-news-shadow.plist.example` | `run-news-shadow.command` | `StartInterval=900`; one-shot result heartbeat |
| `com.sands15.toss-shadow-watchdog` | `com.sands15.toss-shadow-watchdog.plist.example` | `run-shadow-watchdog.command` | `StartInterval=15`; expectation+context-aware change-only JSON, Discord sender not wired |

Copy templates to `~/Library/LaunchAgents` without overwriting an existing file,
replace every placeholder locally, and keep the real installed plist mode 0600.
Each `ProgramArguments` array remains length one. `WorkingDirectory` and the
wrapper path point directly to the same root-owned release directory whose final
component is the exact 40- or 64-hex commit SHA; no `/current` symlink is allowed.
Planner and stream read only the existing `toss-trading-bot` Keychain client
items for their non-secret slug. News reads service/account pairs
`TossTradingBot.FinnhubApiKey`/`news-finnhub` and
`TossTradingBot.DiscordNewsWebhook`/`discord-news-webhook`; when its configured
loopback LLM needs a key, add `TOSS_NEWS_LLM_API_KEY_ENV` only to the installed
news plist and store the value as `TossTradingBot.NewsLlmApiKey`/`news-llm`.
No secret value is present in a checked-in or installed plist.

There are three independent no-trade barriers.

1. The `--shadow-service` entrypoint must reject, before DB, Keychain, or network
   access, unless the strategy is intraday, runtime mode is shadow, both Toss and
   intraday live flags are false, emergency stop is true, the generic live symbol
   list is empty, and `toss.base_url` is the exact official origin. The service
   reloads config every iteration, so it must repeat the check on the newly read
   config object before deriving any path/account or constructing any client.
   Failure terminates the process; it does not skip one iteration. The current
   CLI and operation entrypoint perform this guard before DB, Keychain, or
   network construction and repeat it after config reload.
2. Planner and stream clients must wrap the common Toss transport with a
   read-only tripwire. Production accepts only canonical
   `https://openapi.tossinvest.com` (port absent or 443), no user-info/fragment,
   the compile-time shadow GET path/query allowlist fixed in
   [intraday design section 13.1](intraday-bracket-design.md#131-산출물과-금지선), and exact OAuth
   `POST /oauth2/token` with empty query and exact form key set. Encoded
   slash/dot, backslash, duplicate authority, other origin/port/method/path/query
   fail before delegate. `urllib` uses an actual no-redirect opener and rejects
   301/302/303/307/308 without forwarding OAuth form, Authorization, or account
   headers. Fake origins exist only in dependency-injected tests.
3. The release manifest and launchd allowlist contain no order writer or approval
   consumer, even if their code exists for offline tests.

The release allowlist is the five `*.plist.example` files under `ops/launchd`.
The legacy generic `com.sands15.toss-turtle-bot.plist` is not a non-live release
template and must not be installed. The dedicated planner, stream, approval,
news, and watchdog wrappers use `zsh -f`, `env -i`, `umask 077`, `ulimit -c 0`,
`python -I`, an exact-SHA-named release-root/import check, and immediate Python
environment removal after secret handoff. Every Discord sender must revalidate
the remote channel on each
POST instead of caching the first success. That includes every article within one
news run. Immediately before each POST it resolves channel/webhook metadata and
guild, requires the exact single target, and then sends with mentions disabled.
Bots/webhooks have no management rights; an administrator changing Discord state
between metadata GET and POST is a residual remote trust boundary. A detected
mismatch stops all later sends.

NL1–NL5 automated runners start with Toss/Discord/account secrets removed in
the parent environment and loopback-only OS/container/firewall egress. Child
processes inherit the same deny policy. Autouse socket/urllib/WebSocket guards
and call counters are additional evidence, not substitutes for process-level
network isolation; fake broker hosts use `.invalid` only.

Offline/synthetic Mac verification uses dummy Keychain items and mailboxes,
fixture heartbeats, a copied SQLite DB, fake REST/WebSocket transports, and no
order credential. Real market shadow verification may subscribe only to the
locked symbol's public trade/orderbook topics. It may not receive an account
sequence or personal order topic. Reboot, sleep, disk, restart-loop, mailbox UID,
and watchdog tests remain in scope because they do not place an order.

The future watchdog Discord sender's only secret is an alert-only bot token in its
own Keychain item; the current watchdog process performs no network I/O and emits
change-only redacted JSON to stdout. Planner, stream, approval, and news wrappers
now atomically publish the exact heartbeat schema. Planner publishes its own DB
`quick_check=ok|fail`; an active stream publishes verified ACK/baseline freshness; approval
and news use `not_applicable`. The watchdog validates those four files and launchd
state but never opens trading SQLite. The writer currently creates each local
heartbeat as mode 0600. A separate-watchdog-UID/group-readable deployment remains
an unverified Mac gate and must not be claimed from the templates alone.

The watchdog plist must set `TOSS_WATCHDOG_CONTEXT_PATH` to the exact same
owner-private `news-context.json` watched by the stream and
`TOSS_WATCHDOG_EXPECTATION_PATH` to its sibling `stream-expectation.json`.
The planner atomically writes that redacted expectation from its locked state DB
before exporting context. An active expectation plus missing, deleted, idle, or
invalid context produces `STREAM_CONTEXT_INVALID`, so a context export failure
cannot masquerade as healthy idle. An active context with an absent or expired
expectation, or a malformed expectation, produces `STREAM_EXPECTATION_INVALID`.
Only a pre-plan absence or a pair of well-formed expired files means stream-idle:
a loaded WatchPaths job may then be stopped without a fresh stream heartbeat.
When both are active for the current session, process, heartbeat, ACK, and REST
baseline are required. Both files must be regular, non-symlink, runtime-owned,
and owner-only. Schema, time, session, reason, or permission failures remain
fail-closed.

The evidence package contains only exact release and dependency hashes, test and
fixture counts, plist/wrapper lint results, the hard-off assertion, read-only
transport audit, and redacted heartbeat status. Never include private paths,
account or channel identifiers, broker payloads, Keychain values, logs, or
screenshots.

### One-month intraday forward simulation overlay

The planned forward observation window is inclusive US market session dates
`2026-08-31` through `2026-09-30`. The default opening balance is a configurable,
simulation-only `USD 10,000`; it is never synchronized from the Toss account.
For each expected weekday, the planner records an immutable plan, an idempotent
`MARKET_CLOSED` row after checking the Toss calendar, or `NO_CANDIDATE` after the
last useful planner iteration finds no symbol that passes the strategy thresholds.
An earlier empty selection remains retryable so a later candidate can still be
planned. Incomplete, stale, or future daily/premarket candles and invalid quote or
orderbook data remain fail-closed and never create coverage. If Mac deployment
finishes after the first date, do not backfill ticks, plans, holidays, or fills.
Those uncovered weekdays remain listed as missing and the post-period summary is
`INCOMPLETE` rather than a successful month.

`MARKET_CLOSED` requires both `preMarket` and `regularMarket` keys to be present
and both values to be explicit null. A missing key is
`intraday_calendar_malformed`; only one null is
`intraday_required_session_unavailable`. Neither case may create a holiday
coverage row.

This overlay does not add a sixth LaunchAgent. The intraday planner owns virtual
cash sizing, the immutable daily plan, and daily/month-end reporting. The existing
selected-symbol stream appends validated Toss public trade/orderbook events and
runs the causal fill evaluator against a separate USD ledger. The other three
allowlisted jobs remain approval recorder, news one-shot, and unprivileged
watchdog. Paper execution does not wait for or consume a Discord approval receipt;
the approval job remains an independent shadow security exercise.

After each planner iteration, the planner derives `paper-status.json` beside the
configured `approval-envelope.json` and atomically writes only the redacted month
summary, latest-day summary, and planner readiness. The approval process reads only
that owner-private file for `/현황`; it receives no trading database path and imports
no trading package. The reader rejects a stale file (over 130 seconds), wrong release
SHA or boot hash, symlinks, non-`0600` mode, schema drift, and any value claiming live
submission. Calls outside the exact allowlisted user, guild, and channel are silent;
the allowed response is ephemeral with mentions disabled.

Use two separate private manifests with the same basename: a planner manifest
under a planner-only directory and an account-free stream manifest under a
stream-only directory. Only the planner copy contains the account sequence,
because Toss requires its account header for the commission-schedule GET. Its
simulation transport blocks holdings, buying power, account/order history,
personal WebSocket, create, replace, cancel, and conditional-order endpoints,
and rejects an account header on public market GETs. The stream copy leaves both
account alias and sequence empty and uses only the locked symbol's public
`trade:us` and `orderbook:us` topics plus its public REST baseline. It still reads
OAuth application credentials from Keychain because Toss authenticates its public
market interface; those are not account holdings or order authority.

Before any Keychain read, the planner wrapper uses `lstat` to require its config
to be a non-symlink regular file owned by the runtime UID with no group or other
permission bits; install it as mode 0600. The private installed plist also pins
`TOSS_SHADOW_ACCOUNT_FINGERPRINT`, a 64-lowercase-hex SHA-256 binding of the
planner account sequence. The planner recomputes and compares that binding on
every config reload, so removing or changing account authority terminates the
service. Never place the sequence or its fingerprint in the stream manifest,
stream environment, release manifest, logs, or public evidence.

Both manifests must resolve to the same absolute intraday plan DB, paper DB, and
context path, and must produce the same experiment SHA-256. That hash includes
the run, inclusive dates, virtual capital, slippage, economic/risk/selection/time
inputs, relevant runtime fields, and the resolved absolute `news_context_path`;
it intentionally excludes account identity and other filesystem paths. Both
wrappers separately pin expected run ID, dates, paper
DB, and experiment hash. The planner repeats the lock check on config reload, and
the paper DB rejects a different immutable run config. A changed manifest must
fail closed instead of silently starting a new strategy inside the same month.

Only normalized USD trade/orderbook frames that pass the parser and sink freshness
checks are journaled; malformed frames themselves are not persisted. The separate
SQLite database requires WAL and `synchronous=FULL`. The stream preserves queue
order and commits on the 128th frame or on a periodic receive-loop tick after the
0.25-second threshold; the default idle receive poll is one second. Disconnect
and normal close also flush. An abrupt death can therefore lose at most 127 queued
frames. After context validation but before OAuth or socket work, the stream sends
`start`; the sink durably opens a `paper_stream_instances` marker, refreshes its
`last_seen_at` no more than once per second, and closes it only after an orderly
final flush. At session finalization the planner checks whether any instance spans
the required entry-expiry or force-exit boundary. It defers while the newest open
marker started before that boundary and remains fresh within one quote TTL. Once
stale, it closes every orphan marker as `stream_liveness_expired`. No instance
spanning the boundary records `stream_coverage_incomplete`; durable boundary
coverage followed by loss of the still-open process records
`stream_process_interrupted`. Only then may finalization continue. A replacement
stream closes its predecessor as `superseded_by_stream_restart`.

After a verified REST baseline, each acknowledged selected-symbol trade and
orderbook topic must produce a fresh event in the current generation within one
quote TTL. `trade_topic_silent` or `orderbook_topic_silent` disconnects and
reconnects the socket and durably invalidates a sensitive-window day. A sensitive
disconnect, late first frame, or frame-level validation error also records a data
gap. Lost frame contents cannot be reconstructed: this is a real selected-symbol
received-event sample, never a gap-free exchange tape.

Entry, target, and stop triggers come only from an actually received trade frame,
not a last-trade value carried forward in an orderbook or REST baseline. The trade
timestamp must also be after the relevant entry-start or virtual-entry boundary.
Each trigger then requires a subsequent accepted orderbook with a different book
hash. Visible top-of-book depth must cover the whole integer
quantity; limit rules and configured adverse slippage apply. Fees are only the
plan's per-leg broker commission plus half the configured fixed round-trip cost;
there is no separate regulatory/minimum-fee engine or slippage-drag metric. One
BUY per session is allowed with no re-entry. A gap with an open virtual position
forces a full-depth, no-limit exit at the next accepted bid and excludes the day
from clean metrics. Force-exit uses a fresh stored book; failure to close by the
regular close leaves an entered virtual position `UNRESOLVED`, makes final equity
and return unknown, and blocks later plans. `UNRESOLVED` does not mean no entry or
missing calendar coverage; it specifically means the virtual position could not
be given a causal exit fill. The engine never invents a fill.

The planner enqueues a redacted daily Discord payload with status, quantity,
entry/exit price, time, and reason, gross/net P&L, total modeled fees, cash
before/after, accepted-event/journal-frame/gap counts, first/last event time, fee
sources, and clean-metric inclusion. After the inclusive end date, the status-keyed
run payload includes status, initial/current cash and final equity, realized and
clean P&L/return, trade/win/loss counts, win rate, average win/loss, expectancy,
profit factor, total fees, MDD, exit reasons,
no-entry/no-candidate/invalid/unresolved/waiting counts,
expected/covered/missing/market-closed/no-candidate coverage, journal counters,
and the fee/journal policy. The rendered one-line Discord message shows the most important
subset. A non-`COMPLETE` run report is warning-level.

All expected weekdays must be covered by a plan, `MARKET_CLOSED`, or
`NO_CANDIDATE` row. `NO_CANDIDATE` proves that the final usable selection pass ran;
it is not a fabricated plan or trade. After the end date, missing coverage or zero
actual plans still yields `INCOMPLETE`. `WAITING`,
`OPEN`, `UNRESOLVED`, `INVALID`, and `BLOCKED` take precedence when they describe a
more concrete condition. MAE/MFE, exposure, symbol distribution, uptime/reconnect
percentiles, and separate slippage drag are not implemented. Discord retries use
the main outbox and do not repeat a fill or ledger entry.

`/현황` schema version 2 exposes the no-candidate count and uses the latest covered
day, including `NO_CANDIDATE` or `MARKET_CLOSED`, rather than the latest plan only.
The final no-candidate decision currently writes a durable runtime event and this
owner-private status artifact; it does not enqueue a separate Discord alert.

Local implementation and regression verification are complete. Mac deployment is
still pending: do not start the dated run until plist/wrapper lint, the exact-five
process manifest, exact-SHA installation, real public Toss WS journal smoke, and
redacted daily/final Discord smoke pass on that Mac. Current status is
`LOCAL_IMPLEMENTATION_VERIFIED / MAC_DEPLOYMENT_PENDING / LIVE_NO_GO`.

### Selected-symbol Toss market stream (shadow-only)

The stream is a third process, separate from the 60-second planner heartbeat and
the 15-minute news worker. Its LaunchAgent has `WatchPaths` on the planner-owned
`news-context.json` and deliberately has no `RunAtLoad`, `StartInterval`, or
`KeepAlive`: it stays idle before a plan and launchd invokes it when the planner
creates or refreshes context. Once invoked it opens one Toss WebSocket and
subscribes only to `trade:us` and `orderbook:us` for the locked symbol. It receives no account sequence, does not subscribe to
`personal:order`, and never instantiates or calls an order adapter. With the
simulation arguments it opens the intraday state DB to load the immutable plan
and writes only the separate paper journal/ledger DB. Importing a `turtle_bot`
submodule still executes the package's
current eager `__init__`, so the verified authority boundary is zero order calls,
not OS-level code isolation. Its published `ready_for_live_entry` is always
`false`.

Install the reviewed optional dependency in a clean exact-SHA release:

```zsh
.venv/bin/python -m pip install -e '.[stream]'
chmod 700 ops/run-toss-stream.command
```

This is a user-owned development-checkout permission only. The immutable
root-owned release installs the wrapper as `0555` or `0755`, pins the hashed
wheelhouse, and performs no editable install.

Use the same resolved absolute private context path in both manifests as
`strategy.intraday.news_context_path`; simulation validation and the experiment
hash reject a different resolved path. The planner remains the only context
writer and writes sibling `stream-expectation.json` from the locked plan DB before
each pre-close context export; the stream is the only `market-stream.json` writer. All files live
outside the checkout under current-user private directories:

```zsh
runtime_dir="$HOME/.local/share/toss-trading-bot/intraday-shadow"
install -d -m 700 "$runtime_dir"
```

For the forward simulation, create two files named exactly
`intraday-simulation.yaml`: one under a planner directory and one under a stream
directory. Point `TOSS_SHADOW_CONFIG_PATH` to the planner copy and
`TOSS_STREAM_SIMULATION_CONFIG_PATH` to the stream copy. Leave the stream copy's
account alias/sequence blank. Both jobs use the same intraday plan DB named
`intraday.sqlite3`, the same separate paper DB ending in
`intraday-paper.sqlite3`, and the same context path. Compute and pin one identical
experiment hash only after validating both parsed manifests. Keep the planner
copy mode 0600 and set its private account fingerprint in the installed planner
plist; the stream copy must not contain either value.

The argument-free wrapper reuses credentials previously stored by the local
gateway in the login Keychain. For a non-secret gateway user slug `USER_SLUG`,
the metadata is:

```text
service: toss-trading-bot
account: USER_SLUG:toss_client_id
account: USER_SLUG:toss_client_secret
```

Confirm that both items exist without printing their values:

```zsh
security find-generic-password \
  -s toss-trading-bot -a 'USER_SLUG:toss_client_id' >/dev/null
security find-generic-password \
  -s toss-trading-bot -a 'USER_SLUG:toss_client_secret' >/dev/null
```

Do not use `security ... -w` interactively and do not copy either value into a
plist, command argument, `.env`, config, or log. The wrapper starts an `env -i`
child, reads both values only inside that child, and the Python process removes
the two environment entries immediately after capture. No account sequence,
Discord setting, approval receipt, or live flag enters that process. The wrapper
does pass the immutable simulation config and intraday plan-DB paths; the paper
DB path is read from that private config.

Create a local LaunchAgent from the template without overwriting an existing
job. Replace `release_root` and `slug` with reviewed non-secret values:

```zsh
release_root=/ABSOLUTE/PATH/TO/CLEAN/EXACT-SHA/RELEASE
slug=USER_SLUG
runtime_dir="$HOME/.local/share/toss-trading-bot/intraday-shadow"
installed="$HOME/Library/LaunchAgents/com.sands15.toss-market-stream-shadow.plist"

if [[ -e "$installed" ]]; then
  printf '%s\n' 'shadow stream LaunchAgent already exists; inspect it first' >&2
  return 1
fi
cp "$release_root/ops/launchd/com.sands15.toss-market-stream-shadow.plist.example" \
  "$installed"
plutil -replace ProgramArguments.0 -string \
  "$release_root/ops/run-toss-stream.command" "$installed"
plutil -replace WorkingDirectory -string "$release_root" "$installed"
plutil -replace EnvironmentVariables.TOSS_STREAM_CONTEXT_PATH -string \
  "$runtime_dir/news-context.json" "$installed"
plutil -replace WatchPaths.0 -string \
  "$runtime_dir/news-context.json" "$installed"
plutil -replace EnvironmentVariables.TOSS_STREAM_SNAPSHOT_PATH -string \
  "$runtime_dir/market-stream.json" "$installed"
plutil -replace EnvironmentVariables.TOSS_STREAM_SIMULATION_CONFIG_PATH -string \
  "$runtime_dir/stream/intraday-simulation.yaml" "$installed"
plutil -replace EnvironmentVariables.TOSS_STREAM_PLAN_DB -string \
  "$runtime_dir/intraday.sqlite3" "$installed"
plutil -replace EnvironmentVariables.TOSS_STREAM_SIMULATION_DB -string \
  "$runtime_dir/intraday-paper.sqlite3" "$installed"
plutil -replace EnvironmentVariables.TOSS_STREAM_SIMULATION_ID -string \
  '2026-09-forward-test' "$installed"
plutil -replace EnvironmentVariables.TOSS_STREAM_SIMULATION_START_DATE -string \
  '2026-08-31' "$installed"
plutil -replace EnvironmentVariables.TOSS_STREAM_SIMULATION_END_DATE -string \
  '2026-09-30' "$installed"
plutil -replace EnvironmentVariables.TOSS_STREAM_EXPERIMENT_HASH -string \
  'REPLACE_WITH_VERIFIED_EXPERIMENT_HASH' "$installed"
plutil -replace EnvironmentVariables.TOSS_STREAM_HEARTBEAT_PATH -string \
  "$runtime_dir/heartbeats/stream/heartbeat.json" "$installed"
plutil -replace EnvironmentVariables.TOSS_STREAM_KEYCHAIN_SLUG -string \
  "$slug" "$installed"
plutil -replace StandardOutPath -string \
  "$runtime_dir/market-stream.stdout.log" "$installed"
plutil -replace StandardErrorPath -string \
  "$runtime_dir/market-stream.stderr.log" "$installed"
chmod 600 "$installed"
plutil -lint "$installed"
zsh -n "$release_root/ops/run-toss-stream.command"
```

Bootstrap the job from the logged-in Mac Aqua Terminal before creating the first
context, so any Keychain ACL prompt is visible when `WatchPaths` starts it. An
unlocked login keychain or an SSH shell alone does not prove an Aqua LaunchAgent
can read the items:

```zsh
launchctl bootstrap "gui/$(id -u)" "$installed"
launchctl print "gui/$(id -u)/com.sands15.toss-market-stream-shadow"
```

After the Aqua session is established, Tailscale SSH is appropriate for
monitoring. A manual kickstart is a diagnostic action, not the normal scheduler:

```zsh
launchctl print "gui/$(id -u)/com.sands15.toss-market-stream-shadow"
launchctl kickstart -k \
  "gui/$(id -u)/com.sands15.toss-market-stream-shadow"
```

The job exits cleanly when the selected context expires. At or after regular
close, the planner may refresh the expectation marker but does not rewrite
`news-context.json`, avoiding a post-close `WatchPaths` relaunch loop. The stream
then remains idle until a later session's context write; there is no unconditional
60-second launch and no pre-plan OAuth/WebSocket activity. Inside one active
process, WebSocket failures use capped exponential backoff. If the process exits,
the next planner context refresh launches it again; an operator may use the shown
kickstart only for supervised diagnosis. If the planner stops refreshing context,
the stream fails closed instead of continuing with an old symbol.

With a fresh intraday plan, validate the redacted snapshot without displaying
raw credentials or frame data:

```zsh
snapshot="$runtime_dir/market-stream.json"
test "$(stat -f '%Lp' "$runtime_dir")" = 700
test "$(stat -f '%Lp' "$snapshot")" = 600
"$release_root/.venv/bin/python" -I -c '
import datetime, json, pathlib, sys
p = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert p["mode"] == "shadow"
assert p["live_order_submission"] is False
assert p["ready_for_live_entry"] is False
symbol = p["symbol"]
expected = {f"trade:us:{symbol}", f"orderbook:us:{symbol}"}
assert set(p["subscribed_topics"]) in (set(), expected)
valid_until = datetime.datetime.fromisoformat(p["valid_until"])
if p["shadow_usable"]:
    assert datetime.datetime.now(datetime.timezone.utc) < valid_until
print({k: p[k] for k in ("symbol", "connected", "subscription_acknowledged",
                          "shadow_usable", "valid_until", "rest_resynced_at",
                          "error_codes")})
' "$snapshot"
```

Before unattended shadow soak, also confirm the installed plist has no client
ID, secret, token, or account sequence; process argv has only the wrapper or
Python module plus context/snapshot paths; and stdout/stderr contain none of
`Authorization`, `Bearer`, `access_token`, or `client_secret`. The Tailscale IP
is only the SSH route. If Toss enforces an API allowlist, register the Mac's
public egress IP, not its Tailscale address.

Passing unit tests is not an external deployment result. A real Toss handshake,
REST baseline, reconnect injection, and 5–10 US market-session shadow soak must
still pass on the clean Mac release before any live entry state machine is
designed or enabled.

### Selected-symbol news worker

The news path is a separate one-shot process, not a broker WebSocket. The
intraday service refreshes exactly one redacted symbol context; every 15 minutes
the worker polls Finnhub Company News, optionally calls a loopback-only LLM, and
sends at most four pending items per invocation through a news-only
credential/webhook. The cap is rate control, not a drop policy: every validated
new provider item is first persisted, unsent items remain pending, and later
runs drain oldest-first while the same plan/session and 24-hour age window remain
valid. “Every new article” means every unique article actually returned by the
configured provider and passing the locked-symbol checks; it cannot guarantee
coverage or real-time delivery of all Internet news. That
webhook must resolve to the same single allowed Discord channel used by approval
and trade notifications; only processes and credentials are separate.

Prepare distinct local files without putting secrets in them:

```bash
test -e config/news-digest.local.json || cp config/news-digest.example.json config/news-digest.local.json
chmod 600 config/news-digest.local.json
```

Set both configs to the same absolute `news-context.json` outside the checkout,
and set the news DB to a different outside-checkout path. One context path has
exactly one trading writer. For a second account, create a second runtime
directory, config, DB, and worker; do not share the context file.

The clean news environment contains `FINNHUB_API_KEY`,
`DISCORD_NEWS_WEBHOOK_URL`, and `DISCORD_ALLOWED_CHANNEL_ID`, plus the optional
local LLM key only. It must not
inherit `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`, or
`DISCORD_TRADE_ALERT_WEBHOOK_URL`; the worker refuses to start if any is present.
Store the values in Keychain and have a news-only wrapper retrieve them at exec
time. Never place them in a plist, JSON, command argument, log, or shell history.
Before fetching articles and again immediately before **each article POST**, the
worker resolves webhook/channel/guild metadata and requires the exact configured
single target; mismatch or lookup failure performs no Discord POST and stops the
remaining batch. A successful first lookup is never cached across sends.

Run a manual one-shot before scheduling it:

```bash
.venv/bin/python -m turtle_news --config config/news-digest.local.json
```

The command requires a fresh same-day context, so first run the intraday shadow
heartbeat during its permitted plan window. To test feed and Discord without a
model, set `llm.enabled` to `false`; invalid or unavailable LLM output omits the
excerpt and sends only sanitized headline/source/time/link. It never affects trading.

Install the news command as its own LaunchAgent with a distinct label,
`StartInterval=900`, no `KeepAlive`, umask `077`, the reviewed checkout as
working directory, the exact `.venv/bin/python`, and separate stdout/stderr
files. Its Keychain wrapper must construct an allowlisted clean environment.
Before bootstrap, run `plutil -lint` on the generated plist and run the exact
wrapper manually. After installation, verify one scheduled run, user login after
reboot, stale-context refusal, LLM shutdown fallback, Discord failure retry, and
that the trading heartbeat/DB are unchanged.

Discord delivery is at-least-once: URL dedupe and SQLite leases suppress normal
duplicates, but a response lost after Discord accepted a message can cause one
retry duplicate. Finnhub polling is not guaranteed real-time or complete and is
not a trading risk control.

## Dashboard Over Tailscale

> **Live intraday exclusion:** the current dashboard, `health.py` action POST
> routes, `ops/run-dashboard-macos.command`, multi-user gateway, and dashboard
> containers are not part of the live intraday release. The current action
> surface is not an authenticated, CSRF-protected, read-only control plane and
> can reach live-smoke/safe-pilot/stop operations. Loopback binding or Tailscale
> transport does not change that capability. During a live pilot, use Tailscale
> SSH for read-only CLI inspection only and keep these processes unloaded.

The instructions below are retained for the legacy paper/dashboard workflow.
They must not be used to start, arm, stop, or inspect the live intraday writer.

### Single Local Operator

For a single Mac desktop/session launcher, run:

```bash
chmod +x ops/run-dashboard-macos.command
DASHBOARD_HOST=127.0.0.1 ops/run-dashboard-macos.command
```

Force the dashboard to `127.0.0.1` and reach it only through Tailscale SSH local
forwarding. From the management device, forward local port `8765` to the Mac's
`127.0.0.1:8765`, then open `http://127.0.0.1:8765/`. Do not bind the dashboard
directly to a LAN or Tailscale address.

To use a different port:

```bash
DASHBOARD_PORT=8766 ops/run-dashboard-macos.command
```

If the SSH tunnel does not open, confirm Tailscale is logged in on the Mac with:

```bash
tailscale status
tailscale ip -4
```

To force local-only access:

```bash
DASHBOARD_HOST=127.0.0.1 ops/run-dashboard-macos.command
```

### Per-User Docker Containers

If more than one person will use the dashboard, prefer the multi-user gateway.
It identifies users by Tailscale Serve identity headers. A first-time Tailscale
user enters only Toss app ID, Toss app secret, and account sequence. After that,
the user's Tailscale login, for example `alice@example.com`, is routed to their
own Docker container from any of their Tailscale devices.

The one-file launcher is:

```bash
chmod +x Start-Toss-Gateway.command
open Start-Toss-Gateway.command
```

This prepares `.venv`, installs the local package, checks Docker Desktop and
Tailscale, starts `tailscale serve`, and then runs the multi-user gateway.

For unattended operation, install the gateway as a per-user `launchd` service:

```bash
chmod +x Install-Toss-Gateway-Service.command
open Install-Toss-Gateway-Service.command
```

This creates `~/Library/LaunchAgents/com.sands15.toss-gateway.plist`, starts the
gateway on login, restarts it after crashes, and keeps Tailscale Serve pointed
at the gateway port.

The following updater belongs to the legacy multi-user gateway workflow. Do not
use it for the trading, intraday, approval, or news release while the deployment
hold above applies, and never point it at a dirty or diverged checkout:

```bash
chmod +x Update-Toss-Gateway.command
open Update-Toss-Gateway.command
```

For the legacy gateway only, the updater fetches `origin/main`, updates the local package, starts a standby
gateway on port `8766`, checks `/health`, temporarily points Tailscale Serve to
the standby gateway, restarts the managed launchd gateway, then switches Serve
back to port `8765`. Existing user Docker containers are left running.

The lower-level gateway launcher is:

```bash
chmod +x ops/run-multi-user-gateway.command
open ops/run-multi-user-gateway.command
```

Open the Tailscale Serve HTTPS URL:

```bash
tailscale serve status
```

The gateway process itself binds to `127.0.0.1` because Tailscale identity
headers must come from the Serve proxy. Do not expose the gateway directly on a
LAN or Tailscale IP, because direct callers could spoof identity headers.

By default, any first-time Tailscale user who can reach the Serve URL may open
the setup form. For a smaller private beta, restrict first-time setup to known
Tailscale IPs or CIDR ranges:

```bash
REGISTRATION_ALLOWLIST="PRIVATE_TAILSCALE_IP,PRIVATE_TAILSCALE_CIDR" open ops/run-multi-user-gateway.command
```

Setup submissions are rate-limited per client IP. The defaults are 5 setup
submissions per 900 seconds. Override them only when onboarding many known
devices at once:

```bash
SETUP_RATE_LIMIT=10 SETUP_RATE_WINDOW_SECONDS=900 open ops/run-multi-user-gateway.command
```

User containers default to conservative Docker limits: 512 MB memory, 1 CPU,
and Docker logs capped at 10 MB x 3 files. Override these for a larger Mac:

```bash
CONTAINER_MEMORY=1g CONTAINER_CPUS=2.0 CONTAINER_LOG_MAX_SIZE=20m open ops/run-multi-user-gateway.command
```

The official Toss Open API live path is configured with the app ID, app secret,
OAuth token, and `X-Tossinvest-Account` account header. Do not add a local
preflight that requires Toss developer-center IP registration for personal
accounts. If Toss returns a concrete network rejection for a specific Mac,
investigate VPN, proxy, or cloud routing as an incident, not as a normal setup
step.

The gateway stores its routing registry at:

```text
/PRIVATE/RUNTIME/multi-user/registry.json
```

It writes audit events for setup attempts and container lifecycle commands to:

```text
/PRIVATE/RUNTIME/multi-user/audit.jsonl
```

The routing key is the Tailscale user login from `Tailscale-User-Login`, not the
device IP. The registry keeps `last_client_ip` for auditing and optional gateway
setup allowlists. This is only a local onboarding control for first-time
dashboard registration, not a Toss Open API requirement.

Registry admin helpers:

```bash
python ops/multi_user_gateway.py --list-users
python ops/multi_user_gateway.py --unmap-ip PRIVATE_TAILSCALE_IP
python ops/multi_user_gateway.py --delete-user alice
```

These commands change only the registry. User files and Docker containers remain
until you remove or stop them explicitly.

Container lifecycle helpers:

```bash
python ops/multi_user_gateway.py --stop-user alice
python ops/multi_user_gateway.py --start-user alice
python ops/multi_user_gateway.py --restart-user alice
python ops/multi_user_gateway.py --remove-user-container alice
```

These commands use the registry to find `toss-dashboard-<user>` and update the
stored user status. `--remove-user-container` removes the Docker container only;
it does not delete `.local/users/<user>/` files or Toss credentials.

Secret and cleanup helpers:

```bash
python ops/multi_user_gateway.py --delete-user-secrets alice --confirm DELETE_USER_SECRETS
python ops/multi_user_gateway.py --list-orphans
python ops/multi_user_gateway.py --cleanup-orphans --confirm CLEANUP_ORPHANS
python ops/multi_user_gateway.py --offboard-user alice --confirm OFFBOARD_USER
python ops/multi_user_gateway.py --admin-status
```

`--delete-user-secrets` removes the user's stored Toss app ID and app secret
from the configured backend. `--list-orphans` reports gateway containers or user
folders that are no longer present in the registry. `--cleanup-orphans` removes
orphan containers and moves stale user folders into `/PRIVATE/RUNTIME/trash/`.
`--offboard-user` combines user container removal, secret deletion, registry
removal, and local file trashing for one user. `--admin-status` prints a JSON
summary with user counts, Docker container state, orphan resources, and recent
audit events.

Each user gets separate local files:

```text
.local/users/<user>/config/local.yaml
.local/users/<user>/state/turtle.sqlite3
.local/users/<user>/logs/
.local/users/<user>/.env   # placeholder when the gateway uses Keychain
```

The multi-user gateway uses `SECRET_BACKEND=auto` by default. On macOS that
stores Toss API values in Keychain and leaves `.env` as a non-secret placeholder.
On Windows or when `SECRET_BACKEND=file` is selected, `.env` is plaintext local
development storage. Secret storage details are in `docs/secret-storage-plan.md`.

The macOS Keychain backend must run from the logged-in desktop session. Do not
restart the gateway with SSH `nohup` when `SECRET_BACKEND=auto` or `keychain` is
selected; `security find-generic-password -w` can fail with `User interaction is
not allowed`, and user containers will not receive Toss credentials. Use:

```bash
open ops/run-multi-user-gateway.command
```

For Windows tests or disposable local development only, use `SECRET_BACKEND=file`.

The updater rebuilds the `toss-trading-bot:local` dashboard image and replaces
existing `toss-dashboard-*` containers. This is intentional: the gateway can be
on the latest Git commit while a user's Docker container still serves older
dashboard HTML until the container is recreated.

User containers are bound to `127.0.0.1:<internal-port>` on the Mac. Only the
gateway is exposed on the Tailscale address.

For manual user container management without the gateway:

```bash
chmod +x ops/run-user-dashboard-container.command
ops/run-user-dashboard-container.command alice
```

Use a different port for each user:

```bash
DASHBOARD_PORT=8766 ops/run-user-dashboard-container.command bob
DASHBOARD_PORT=8767 ops/run-user-dashboard-container.command charlie
```

Store that user's Toss credentials in:

```text
.local/users/<user>/.env
```

The manual launcher is still a local development path and uses the plaintext
file backend. For other users, prefer the multi-user gateway with Keychain.

Manage a user container:

```bash
docker logs -f toss-dashboard-alice
docker stop toss-dashboard-alice
docker start toss-dashboard-alice
```

This is still a local Tailnet deployment pattern, not a public SaaS model. A
public multi-tenant service still needs authentication, account ownership,
encrypted secret storage, admin controls, and audit logs before real users are
invited.

Hardening status and remaining work are tracked in
`docs/multi-user-hardening.md`.

Windows development should use the same package:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

## Configuration

Use a local config file and environment variables.

Never commit:

- `TOSS_CLIENT_ID`
- `TOSS_CLIENT_SECRET`
- account identifiers if the user considers them private
- live trading enable flags

Suggested local files:

```text
.env
config/local.yaml
state/turtle.sqlite3
logs/
```

Minimum paper-mode config once Toss read-only credentials are available:

```yaml
toss:
  live_enabled: false
  account_seq: "7"
  client_id_env: TOSS_CLIENT_ID
  client_secret_env: TOSS_CLIENT_SECRET

runtime:
  mode: paper
  market: KR
  timezone: Asia/Seoul
  use_market_calendar: true
  watchlist_enabled: true
  watchlist_top_n: 20
  watchlist_name: premarket
  symbols:
    - "005930"
  state_db: state/turtle.sqlite3
  log_dir: logs

ai:
  enabled: false
  provider: openai_compatible
  model: bRadu/gemma-4-E2B-it-textonly
  base_url: http://localhost:8000/v1
  api_key_env: TURTLE_AI_API_KEY
```

`runtime.symbols` is the manual starting point. When
`runtime.universe_enabled=true`, `UniverseBuilder` selects an eligible universe
from `runtime.universe_candidate_symbols` through deterministic read-only
filters: market, instrument type, warning status, liquidity, price, and enough
completed candle history for Turtle rules.

## launchd Service

Template path in repo:

```text
ops/launchd/com.sands15.toss-turtle-bot.plist
```

Install location:

```text
~/Library/LaunchAgents/com.sands15.toss-turtle-bot.plist
```

Expected behavior:

- Starts at login.
- Restarts on crash.
- Writes stdout/stderr to repo or user log directory.
- Runs paper mode by default.
- Live mode requires explicit config.

The checked-in template contains placeholder paths. Generate a local plist with
absolute paths before installing it:

```bash
python -m turtle_bot \
  --config config/local.yaml \
  --state-db state/turtle.sqlite3 \
  --log-dir logs \
  --ensure-runtime-dirs

python -m turtle_bot \
  --config config/local.yaml \
  --repo-dir "$PWD" \
  --python-executable "$PWD/.venv/bin/python" \
  --state-db "$PWD/state/turtle.sqlite3" \
  --log-dir "$PWD/logs" \
  --write-launchd-plist "$HOME/Library/LaunchAgents/com.sands15.toss-turtle-bot.plist"
```

Before bootstrapping, run the paper-mode operations check:

```bash
python -m turtle_bot \
  --config config/local.yaml \
  --state-db state/turtle.sqlite3 \
  --log-dir logs \
  --ops-check
```

The paper service can also be smoke-tested without entering the infinite
launchd loop:

```bash
python -m turtle_bot \
  --config config/local.yaml \
  --state-db state/turtle.sqlite3 \
  --log-dir logs \
  --paper-service \
  --once
```

Postmarket daily report export:

```bash
python -m turtle_bot \
  --state-db state/turtle.sqlite3 \
  --daily-report reports/daily-$(date +%F).json \
  --report-date "$(date +%F)" \
  --report-timezone Asia/Seoul
```

The report is read-only over SQLite state. It summarizes runtime events,
blockers, watchlist rows, paper positions, and latest broker snapshots. AI may
summarize this report for the operator, but the report itself remains the
auditable source of facts.

AI daily report summary through an OpenAI-compatible API:

```bash
python -m turtle_bot \
  --config config/local.yaml \
  --state-db state/turtle.sqlite3 \
  --daily-report reports/daily-$(date +%F).json \
  --report-date "$(date +%F)" \
  --report-timezone Asia/Seoul \
  --daily-report-ai-summary
```

The configured server must expose `/v1/chat/completions`. The default model
string is `bRadu/gemma-4-E2B-it-textonly`, but the bot treats the model as an
API implementation detail. On Apple Silicon, the preferred future local path is
an MLX int4 model served behind an OpenAI-compatible API. NVIDIA/vLLM,
Transformers, or llama.cpp servers are acceptable test or alternate backends as
long as they preserve the same API contract. AI summary output is
operator-facing prose only; it must not feed back into universe selection,
watchlist ranking, Turtle signals, sizing, or guards.

Example commands:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sands15.toss-turtle-bot.plist
launchctl kickstart -k gui/$(id -u)/com.sands15.toss-turtle-bot
launchctl print gui/$(id -u)/com.sands15.toss-turtle-bot
launchctl bootout gui/$(id -u)/com.sands15.toss-turtle-bot
```

## SQLite Backup and Rollback Gate

Never use a zip containing only a live `.sqlite3` file as a consistent backup;
WAL state may be omitted. With the writer entry-disabled and broker state
captured read-only, use SQLite's backup API to a previously absent exact path:

```bash
sqlite3 "$PRIVATE_DB" ".timeout 5000" ".backup '$PRIVATE_BACKUP'"
sqlite3 "$PRIVATE_BACKUP" 'PRAGMA quick_check;'
chmod 600 "$PRIVATE_BACKUP"
shasum -a 256 "$PRIVATE_BACKUP" > "$PRIVATE_BACKUP.sha256"
```

Do not print the private paths or broker snapshots into public logs. Re-read
holdings, all OPEN general/conditional orders, and session CLOSED pages before
and after the backup. A rollback is allowed only when the strategy is flat and
every owned general order has an exact terminal status, no conditional group is
active, and no leg-created SELL remains. Preserve the exact previous release with its
pre-migration backup; do not run a reverse migration. If a position or OCO is
active, keep the current guardian process managing it and postpone rollback.

## Power Settings

Amphetamine should be configured to prevent system sleep indefinitely while the
bot is expected to run.

For fixed machines such as Mac mini, consider also checking:

```bash
pmset -g
```

Do not rely on display sleep status. Display sleep is fine; system sleep is not.

## Startup Safety Sequence

On every process start:

1. Load config.
2. Open DB and acquire process lock.
3. Resolve mode.
4. Authenticate if mode needs Toss.
5. Resolve account sequence.
6. Fetch holdings.
7. Fetch open orders.
8. Reconcile local positions.
9. Block live orders if mismatch exists.
10. Fetch latest completed candles.
11. Calculate indicator snapshots.
12. Build or load the premarket watchlist.
13. Start health/status surface if enabled.
14. Enter runtime loop.

The bot must never submit orders immediately after process start before
reconciliation.

For the future intraday live writer, startup is stricter:

1. Open the exact release and private DB, run `quick_check`, and acquire the
   account writer fence.
2. Enter durable `RECONCILING`; do not reuse an in-memory state or an earlier
   pre-entry receipt after a restart.
3. Read all holdings, all OPEN general/conditional orders, every CLOSED page
   covering the session, and every locally known order detail.
4. Connect one full-replace WebSocket declaration for the immutable symbol's
   trade/orderbook topics plus the personal account-order topic; verify exact
   ACK, then repeat the REST snapshot.
5. Reconstruct cumulative strategy BUY minus strategy SELL fills. Never adopt
   an unknown/manual holding as bot ownership.
6. If a position exists, resume OCO verification or risk-reducing exit even when
   the entry kill switch is active. If no submit exists, rotate approval
   generation and require a fresh Discord approval before `READY_TO_ENTER`.
7. Start the independent trading-unprivileged heartbeat watchdog. Only then may a
   fresh crossing be evaluated.

The operational stop is a durable **new-entry disable**, not `kill -9` of an
account with a position. It blocks only `ENTRY`; entry cancel, OCO protection,
protective exit, force exit, and emergency exit must continue under the writer
fence. Unload or roll back the writer only after REST proves the account is flat
for the strategy, every owned general order has an exact terminal status, no
conditional group is active, and no leg-created SELL remains.

The current paper-mode service is intentionally conservative. Without
`runtime.symbols`, Toss env credentials, and `toss.account_seq`, it records a
blocked health payload and no trade decision is evaluated. Once those read-only
inputs exist, it checks the Toss read-only market calendar first. If the session
is closed or unknown, it records a blocker and stops that iteration. If the
session is PREOPEN, it builds and persists the premarket watchlist but still
blocks paper order-intent evaluation. If the session is open, it builds the
watchlist, reconciles holdings/open orders through read-only endpoints, fetches
prices, and then runs the paper Turtle loop. It does not submit, cancel, or
modify broker orders.

## Runtime Windows

Times must be based on market calendar APIs, not hard-coded clocks. Static
times may be used only as fallback in paper/backtest mode. The current paper
service uses the Toss market-calendar endpoint as a gate before evaluating
paper intents.

Loop profiles:

- Premarket: fetch candles, prepare channels, verify account.
- Premarket watchlist: rank symbols near 20-day and 55-day breakout levels,
  persist the session watchlist, and notify.
- Automatic universe selection: rule-based screening before watchlist
  generation. AI may summarize the screening result but must not select symbols.
- Market open: price/orderbook polling through cache, order guard, state sync.
- Postmarket: final order reconciliation, report, candle cache refresh.
- Closed: slow health loop only.

## Daily Operating Rhythm

Suggested KST rhythm for KR market operation:

- 07:00: token/account readiness check.
- 07:30: completed candle refresh and Turtle watchlist generation.
- 08:00: system-active notification with current blockers.
- 09:00: market-open reconciliation and paper/live loop activation.
- 15:30: market-close handling and order reconciliation.
- 16:00: final daily report.

These are operational checkpoints, not trading rules. The scheduler should use
Toss market-calendar APIs for actual market sessions.

## Failure Policies

### Network Failure

- Do not submit orders.
- Keep process alive.
- Retry with capped backoff.
- Alert if failure persists beyond threshold.

### API Rate Limit

- Obey `Retry-After`.
- Reduce polling frequency.
- Do not compensate by bursting later.
- Preserve quota for account reconciliation and order-state checks before
  broad watchlist or universe refreshes.

### Process Crash

`launchd` restarts the process. On restart, startup safety sequence runs before
any order can be considered.

### Unknown Broker State

If order state is unknown:

- Mark symbol blocked.
- Query open orders and order detail.
- Do not create a replacement order.
- Require clean reconciliation before unblocking.

## Windows Compatibility

Windows is a development and test platform, not the primary daemon runtime.

Must work on Windows:

- Unit tests.
- Backtests.
- Read-only client tests with mocked HTTP.
- Paper loop with mocked or real read-only API if credentials exist.

May be macOS-only:

- `launchd` files.
- Keychain integration.
- Amphetamine assumptions.

Use `pathlib.Path`, not hard-coded `/` or `\`.

## Health Surface

The local health/status service must be read-only until authentication and
operator confirmation are designed.

The current repository does not satisfy that requirement because the same
surface includes unauthenticated action POST routes. Treat the lists below as a
future contract, not a statement that the existing server is safe for a live
release. The live pilot does not start this server at all.

Allowed:

- Current mode.
- Market session state.
- Current blockers.
- Latest watchlist.
- Cached data freshness.
- Open positions and unresolved orders.

Disallowed for now:

- Enabling live mode.
- Starting or stopping trading.
- Closing all positions.
- Editing credentials or account settings.
