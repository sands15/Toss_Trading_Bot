# Multi-User Gateway Hardening

This document tracks the remaining work needed before inviting other people to
use the Tailscale Docker gateway.

## Done

- Per-user Docker containers are routed through one Tailscale-facing gateway.
- Users are routed by Tailscale Serve identity headers instead of client IP or
  an app password.
- First-time users see a setup form with Toss app ID, Toss app secret, and
  account sequence. The display name and identity come from Tailscale.
- Toss credentials are stored through a secret-store backend. macOS gateway
  deployments use Keychain by default; Windows/test runs use the file backend.
- User containers bind to `127.0.0.1:<internal-port>`, and the gateway binds to
  `127.0.0.1`. Tailscale Serve exposes the gateway to the tailnet and supplies
  identity headers.
- Setup forms use CSRF tokens and a maximum request body size.
- Failed provisioning rolls back the registry and removes the failed container.
- Registry helper commands can list users, unmap an IP, and delete registry
  entries.
- Setup submissions are rate-limited by client IP.
- Self-registration can be restricted to an explicit Tailscale IP/CIDR allowlist.
- Admin helper commands can stop, start, restart, and remove a user's Docker
  container without editing the registry by hand.
- Admin helper commands can delete a user's stored Toss credentials after an
  explicit confirmation phrase.
- Admin helper commands can list orphaned gateway containers and stale user
  folders, then clean them up after an explicit confirmation phrase. Stale user
  folders are moved into `_trash` instead of being deleted immediately.
- Admin helper commands can offboard a user in one step by removing the Docker
  container, deleting stored Toss credentials, removing registry mappings, and
  moving local user files into `_trash`.
- Admin helper commands can print a JSON status summary with user counts,
  Docker container state, orphan resources, and recent audit events.
- User containers have default Docker memory, CPU, and log-size limits.
- Identity-missing, setup, registration denial, rate-limit, and container
  lifecycle events are written as JSON lines to `.local/users/audit.log`.
- Secret storage behavior is documented in `docs/secret-storage-plan.md`.

## Next Hardening Passes

1. Verify the full Docker build/run path on the target Mac with Docker Desktop
   running.
2. Add an admin-only browser page for the same status data if command-line
   operations become inconvenient.
3. Add backup and restore documentation for registry/config/state while keeping
   Toss API secrets out of backups.

## Current Risk Notes

- The gateway must be reached through Tailscale Serve. Direct HTTP access to the
  localhost backend is for local debugging only because identity headers could
  be spoofed by local processes.
- Manual single-user Docker launches still use plaintext `.env` files; the
  multi-user gateway is the preferred path for other users.
- Live order submission remains intentionally disabled in the dashboard flow.
