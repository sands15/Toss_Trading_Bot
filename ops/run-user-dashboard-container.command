#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker was not found. Install Docker Desktop on this Mac first." >&2
  exit 1
fi

user_slug="${TOSS_DASHBOARD_USER:-${1:-}}"
if [ -z "$user_slug" ]; then
  printf "Dashboard user id: "
  read -r user_slug
fi

user_slug="$(printf "%s" "$user_slug" | tr "[:upper:]" "[:lower:]" | tr -c "a-z0-9_-" "-")"
user_slug="${user_slug#-}"
user_slug="${user_slug%-}"
if [ -z "$user_slug" ]; then
  echo "User id must contain letters, numbers, underscore, or dash." >&2
  exit 1
fi

dashboard_port="${DASHBOARD_PORT:-}"
if [ -z "$dashboard_port" ]; then
  printf "Host port for %s [8765]: " "$user_slug"
  read -r dashboard_port
  dashboard_port="${dashboard_port:-8765}"
fi

case "$dashboard_port" in
  *[!0-9]*|"")
    echo "DASHBOARD_PORT must be a number." >&2
    exit 1
    ;;
esac

tailscale_ip=""
if command -v tailscale >/dev/null 2>&1; then
  tailscale_ip="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
fi

dashboard_host="${DASHBOARD_HOST:-${tailscale_ip:-127.0.0.1}}"
image_name="${TOSS_DASHBOARD_IMAGE:-toss-trading-bot:local}"
container_name="toss-dashboard-${user_slug}"
user_root=".local/users/${user_slug}"
config_dir="${user_root}/config"
state_dir="${user_root}/state"
log_dir="${user_root}/logs"
env_file="${user_root}/.env"

mkdir -p "$config_dir" "$state_dir" "$log_dir"
if [ ! -f "${config_dir}/local.yaml" ]; then
  cp "config/local.example.yaml" "${config_dir}/local.yaml"
  echo "Created ${config_dir}/local.yaml"
fi
if [ ! -f "$env_file" ]; then
  cat >"$env_file" <<'EOF'
# Put this user's Toss API credentials here.
# Never commit this file.
# TOSS_CLIENT_ID=
# TOSS_CLIENT_SECRET=
EOF
  echo "Created ${env_file}"
fi

echo "Building ${image_name}..."
docker build -t "$image_name" .

if docker ps -a --format "{{.Names}}" | grep -Fxq "$container_name"; then
  echo "Replacing existing container ${container_name}..."
  docker rm -f "$container_name" >/dev/null
fi

docker run -d \
  --name "$container_name" \
  --restart unless-stopped \
  --env-file "$env_file" \
  -p "${dashboard_host}:${dashboard_port}:8765" \
  -v "${repo_root}/${config_dir}:/app/config" \
  -v "${repo_root}/${state_dir}:/app/state" \
  -v "${repo_root}/${log_dir}:/app/logs" \
  "$image_name" \
  --dashboard-server \
  --config /app/config/local.yaml \
  --state-db /app/state/turtle.sqlite3 \
  --host 0.0.0.0 \
  --port 8765

cat <<EOF

Started container:
  ${container_name}

User data:
  ${user_root}/

Local URL:
  http://${dashboard_host}:${dashboard_port}/
EOF

if [ -n "$tailscale_ip" ] && { [ "$dashboard_host" = "$tailscale_ip" ] || [ "$dashboard_host" = "0.0.0.0" ]; }; then
  cat <<EOF

Tailscale URL:
  http://${tailscale_ip}:${dashboard_port}/
EOF
fi

cat <<EOF

Manage:
  docker logs -f ${container_name}
  docker stop ${container_name}
  docker start ${container_name}

EOF
