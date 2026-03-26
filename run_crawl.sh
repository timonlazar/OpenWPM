#!/usr/bin/env bash
# run_crawl.sh - Runner to start the crawl implemented in `demo.py`
# This variant always enables tranco and runs Firefox first, then Chrome
# Usage: ./run_crawl.sh [--headless] [--env PATH] [--credentials PATH]
set -euo pipefail

PYTHON="${PYTHON:-python}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_PY="$SCRIPT_DIR/demo.py"
ENV_FILE="$SCRIPT_DIR/.env"
CREDENTIALS=""

show_usage() {
  cat <<EOF
Usage: $0 [--headless] [--env PATH] [--credentials PATH]
Options:
  --headless            Run browsers in headless mode
  --env PATH            Path to .env file (default: ./ .env)
  --credentials PATH    Path to Google service account JSON (sets GOOGLE_APPLICATION_CREDENTIALS)

Note: tranco is always enabled. The script runs Firefox first, then Chrome.
EOF
}

# Always include --tranco; browser flags are added per sequential run
COMMON_ARGS=("--tranco")
# Parse args (no --tranco option; it's always enabled)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --headless)
      COMMON_ARGS+=("--headless")
      shift
      ;;
    --env)
      if [[ -z "${2-}" ]]; then
        echo "Missing value for --env" >&2
        exit 2
      fi
      ENV_FILE="$(realpath "$2")"
      shift 2
      ;;
    --credentials)
      if [[ -z "${2-}" ]]; then
        echo "Missing value for --credentials" >&2
        exit 2
      fi
      CREDENTIALS="$(realpath "$2")"
      shift 2
      ;;
    -h|--help)
      show_usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      show_usage
      exit 2
      ;;
  esac
done

# Load .env if present (exports variables). Note: shell must support `source`.
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
  echo "Loaded environment from $ENV_FILE"
else
  echo "No .env found at $ENV_FILE - continuing with current environment"
fi

# Optionally set GOOGLE_APPLICATION_CREDENTIALS
if [[ -n "$CREDENTIALS" ]]; then
  export GOOGLE_APPLICATION_CREDENTIALS="$CREDENTIALS"
  echo "Set GOOGLE_APPLICATION_CREDENTIALS=$GOOGLE_APPLICATION_CREDENTIALS"
fi

if [[ ! -f "$DEMO_PY" ]]; then
  echo "demo.py not found at $DEMO_PY" >&2
  exit 3
fi

echo "Starting Firefox crawl: $PYTHON $DEMO_PY ${COMMON_ARGS[*]} --firefox"
"$PYTHON" "$DEMO_PY" "${COMMON_ARGS[@]}" "--firefox"

echo "Starting Chrome crawl: $PYTHON $DEMO_PY ${COMMON_ARGS[*]} --chrome"
exec "$PYTHON" "$DEMO_PY" "${COMMON_ARGS[@]}" "--chrome"
