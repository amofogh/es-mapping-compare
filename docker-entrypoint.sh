#!/bin/sh
set -eu

cd /app
mkdir -p /app/results

# Resolve host owner for bind-mounted outputs.
# Prefer HOST_UID/HOST_GID; otherwise inherit from prefixes.json (host file).
resolve_host_owner() {
  if [ -n "${HOST_UID:-}" ]; then
    echo "${HOST_UID}:${HOST_GID:-$HOST_UID}"
    return 0
  fi
  if [ -e /app/prefixes.json ]; then
    stat -c '%u:%g' /app/prefixes.json 2>/dev/null || true
    return 0
  fi
  # Fall back to project dir ownership if visible
  if [ -d /app ]; then
    stat -c '%u:%g' /app 2>/dev/null || true
  fi
}

# After root writes to bind mounts, hand ownership back to the host user.
fix_host_owner() {
  if [ "$(id -u)" != "0" ]; then
    return 0
  fi
  owner="$(resolve_host_owner || true)"
  if [ -z "$owner" ] || [ "$owner" = "0:0" ]; then
    # Still make results group/world-writable so local python can overwrite.
    chmod -R a+rwX /app/results 2>/dev/null || true
    return 0
  fi
  echo "==> chown outputs to ${owner}"
  chown -R "$owner" /app/results 2>/dev/null || true
  if [ -e /app/prefixes.json ]; then
    chown "$owner" /app/prefixes.json 2>/dev/null || true
  fi
  chmod -R u+rwX,g+rwX /app/results 2>/dev/null || true
}

mode="${1:-compare}"

case "$mode" in
  discover|prefixes)
    echo "==> Discovering index prefixes..."
    python discover_prefixes.py
    fix_host_owner
    echo "==> prefixes.json updated."
    ;;
  compare|mappings)
    echo "==> Comparing mappings (ENABLE_BETA=${ENABLE_BETA:-false})..."
    set +e
    python compare_es_mappings.py
    code=$?
    set -e
    fix_host_owner
    echo "==> Done. Results written to /app/results (mounted on host)."
    ls -la /app/results | head -20 || true
    exit "$code"
    ;;
  all)
    echo "==> [1/2] Discovering index prefixes..."
    python discover_prefixes.py
    echo "==> [2/2] Comparing mappings (ENABLE_BETA=${ENABLE_BETA:-false})..."
    set +e
    python compare_es_mappings.py
    code=$?
    set -e
    fix_host_owner
    echo "==> Done. Results written to /app/results (mounted on host)."
    ls -la /app/results | head -20 || true
    exit "$code"
    ;;
  panel|dashboard|streamlit)
    port="${STREAMLIT_SERVER_PORT:-8501}"
    echo "==> Starting EFK Schema Migration panel on 0.0.0.0:${port}"
    echo "    Open http://localhost:${port}"
    exec streamlit run app.py \
      --server.port="${port}" \
      --server.address=0.0.0.0 \
      --server.headless=true \
      --browser.gatherUsageStats=false
    ;;
  *)
    if [ "$mode" = "fix-perms" ]; then
      echo "==> Fixing ownership/permissions on bind mounts..."
      # Force chmod even when already root-owned so local python can rewrite.
      if [ "$(id -u)" = "0" ]; then
        owner="$(resolve_host_owner || true)"
        if [ -n "$owner" ] && [ "$owner" != "0:0" ]; then
          echo "==> chown to ${owner}"
          chown -R "$owner" /app/results /app/prefixes.json 2>/dev/null || true
        fi
        chmod -R a+rwX /app/results 2>/dev/null || true
        chmod a+rw /app/prefixes.json 2>/dev/null || true
        ls -la /app/results | head -15 || true
        echo "==> Done."
        exit 0
      fi
      echo "fix-perms must run as root in the container" >&2
      exit 1
    fi
    echo "Usage: $0 {discover|compare|all|panel|fix-perms}" >&2
    exit 1
    ;;
esac
