# Multi-User Gateway Hardening

This document tracks the remaining work needed before inviting other people to
use the Tailscale Docker gateway.

## Done

- Per-user Docker containers are routed through one Tailscale-facing gateway.
- Unknown Tailscale client IPs see a first-run setup form.
- Toss credentials are written only to local ignored user files.
- User containers bind to `127.0.0.1:<internal-port>`; only the gateway binds to
  the Tailscale address.
- Setup forms use CSRF tokens and a maximum request body size.
- Failed provisioning rolls back the registry and removes the failed container.
- Registry helper commands can list users, unmap an IP, and delete registry
  entries.
- Setup submissions are rate-limited by client IP.
- Self-registration can be restricted to an explicit Tailscale IP/CIDR allowlist.
- Admin helper commands can stop, start, restart, and remove a user's Docker
  container without editing the registry by hand.
- User containers have default Docker memory, CPU, and log-size limits.
- Setup, registration denial, rate-limit, and container lifecycle events are
  written as JSON lines to `.local/users/audit.log`.

## Next Hardening Passes

1. Add cleanup commands for orphaned containers and stale user folders.
2. Verify the full Docker build/run path on the target Mac with Docker Desktop
   running.
3. Replace IP-only routing with Tailscale identity or an authenticated app login
   before this becomes anything beyond a private Tailnet tool.
4. Move Toss secrets from plaintext `.env` files to macOS Keychain or another
   encrypted secret store.

## Current Risk Notes

- IP-based routing is device-based, not person-based. If two people share one
  Tailscale device, they share the same dashboard.
- A new Tailscale IP is treated as a new visitor unless it is manually mapped.
- Local `.env` files are ignored by git, but they are still plaintext on disk.
- Live order submission remains intentionally disabled in the dashboard flow.
