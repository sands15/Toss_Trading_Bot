# Encrypted Toss Secret Storage Plan

The current gateway writes each user's Toss app ID and app secret to
`.local/users/<user>/.env`. That file is ignored by git, but it is still
plaintext on disk. The target macOS deployment should move those values into an
encrypted local secret store.

## Target Architecture

Use macOS Keychain as the first production secret backend.

- Service namespace: `toss-trading-bot`
- Account keys:
  - `<slug>:toss_client_id`
  - `<slug>:toss_client_secret`
- Non-secret account sequence remains in
  `.local/users/<slug>/config/local.yaml`.
- Registry stores only metadata:
  - `slug`
  - `login_id`
  - `display_name`
  - `container_name`
  - `port`
  - secret backend name, for example `keychain`
  - secret key references, never secret values

## Runtime Flow

1. User signs up or updates Toss credentials in the gateway.
2. Gateway validates input and stores API values in Keychain.
3. Gateway writes a container-local env file just before container start, or
   injects values with Docker `--env` from Keychain reads.
4. The generated env file, if used, is treated as a short-lived runtime artifact
   and rewritten on every container start.
5. The dashboard process still reads `TOSS_CLIENT_ID` and `TOSS_CLIENT_SECRET`
   from environment variables, so app code does not need to know about Keychain.

## Backend Interface

Add a small Python abstraction in the gateway:

```text
SecretStore.put(user_slug, name, value)
SecretStore.get(user_slug, name) -> str
SecretStore.delete_user(user_slug)
SecretStore.healthcheck() -> SecretStoreStatus
```

Implement backends:

- `keychain`: macOS `security add-generic-password` and
  `security find-generic-password`.
- `file`: current plaintext `.env` behavior for Windows tests and local
  development only.

The gateway should default to `keychain` on macOS and `file` elsewhere unless
explicitly configured.

## Migration

1. Add `SECRET_BACKEND=keychain|file` and `--secret-backend`.
2. On gateway start, scan registered users.
3. If `.local/users/<slug>/.env` exists and Keychain values are missing, import
   the values into Keychain.
4. After a successful import, rewrite `.env` as a non-secret placeholder or move
   it to a timestamped local backup path under `.local/users/<slug>/backups/`.
5. Audit every import without logging secret values.

## Safety Requirements

- Never include secret values in registry JSON, audit logs, HTML, test failure
  messages, or command output.
- Do not pass secrets through command arguments when possible; use stdin for
  Keychain writes.
- Generated container env files must have `0600` permissions.
- Removing a user should delete that user's Keychain items after explicit admin
  confirmation.
- Tests must use the `file` backend and assert that public registry output never
  contains secret values.

## Follow-Up

After Keychain support works on macOS, consider Docker secrets or an encrypted
age/sops file backend if the deployment moves beyond one trusted Mac.
