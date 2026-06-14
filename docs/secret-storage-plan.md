# Toss Secret Storage

The multi-user gateway no longer stores Toss API credentials directly in the
registry. It uses a small secret-store layer and injects credentials into Docker
containers only at container creation time.

## Implemented Backends

- `keychain`: macOS Keychain through the `security` command.
- `file`: plaintext `.local/users/<slug>/.env`, kept for Windows tests and local
  development.
- `auto`: `keychain` on macOS, `file` elsewhere.

The macOS launcher uses `SECRET_BACKEND=auto` by default, so the production Mac
path stores Toss values in Keychain.

## Keychain Layout

- Service namespace: `toss-trading-bot`
- Account keys:
  - `<slug>:toss_client_id`
  - `<slug>:toss_client_secret`

The registry stores only metadata such as `secret_backend`, not secret values.
`account_seq` remains in `.local/users/<slug>/config/local.yaml` because it is a
configuration identifier, not an API secret.

## Runtime Flow

1. A Tailscale-identified user completes first-time setup in the gateway.
2. Gateway validates input.
3. Gateway stores Toss app ID and app secret in the configured secret backend.
4. Gateway writes the user config without API values.
5. When creating a Docker container, the gateway reads credentials from the
   backend and passes them to Docker through the subprocess environment:
   `--env TOSS_CLIENT_ID --env TOSS_CLIENT_SECRET`.

Secret values are not written into Docker command arguments, the registry,
audit logs, or HTML. With `keychain`, the generated `.env` file is only a
non-secret placeholder.

## Existing User Migration

When the gateway is running with `keychain` and sees an existing
`.local/users/<slug>/.env` containing `TOSS_CLIENT_ID` and `TOSS_CLIENT_SECRET`,
it imports those values into Keychain before creating a new container. After a
successful import, the `.env` file is rewritten as a non-secret placeholder and
an audit event is written.

This is intentionally conservative: if an existing container is already running,
it can keep running; migration happens when the gateway needs to create or
recreate that user's container.

## Configuration

Gateway CLI:

```bash
python ops/multi_user_gateway.py --secret-backend auto
python ops/multi_user_gateway.py --secret-backend keychain
python ops/multi_user_gateway.py --secret-backend file
```

Secret deletion requires an explicit confirmation phrase:

```bash
python ops/multi_user_gateway.py --delete-user-secrets alice --confirm DELETE_USER_SECRETS
```

This removes the user's Toss app ID and app secret from the configured backend
and writes a `user_secrets_deleted` audit event. With the file backend, it
removes the user's `.env` file. With the Keychain backend, it deletes the two
Keychain items under the configured service namespace.

macOS launcher:

```bash
SECRET_BACKEND=keychain open ops/run-multi-user-gateway.command
KEYCHAIN_SERVICE=toss-trading-bot open ops/run-multi-user-gateway.command
```

## Remaining Work

- Verify the `security` command path on the target Mac with Docker Desktop
  running.
- Consider Docker secrets or an encrypted age/sops backend if the deployment
  moves beyond one trusted Mac.
