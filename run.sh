#!/usr/bin/env bash
# evo-supervisor launcher
# Usage: ./run.sh [--test-only] [--cycles N] [--path /path/to/coreskill]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Defaults
CORESKILL_PATH="${CORESKILL_PATH:-../coreskill}"
CYCLES=5
TEST_ONLY=""
EXTRA_ARGS=""

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        --path|-p)      CORESKILL_PATH="$2"; shift 2 ;;
        --cycles|-n)    CYCLES="$2"; shift 2 ;;
        --test-only)    TEST_ONLY="--test-only"; shift ;;
        --docker)       USE_DOCKER=1; shift ;;
        *)              EXTRA_ARGS="$EXTRA_ARGS $1"; shift ;;
    esac
done

# Resolve path
CORESKILL_PATH="$(realpath "$CORESKILL_PATH")"

# Validate
if [ ! -f "$CORESKILL_PATH/main.py" ]; then
    echo "ERROR: main.py not found in $CORESKILL_PATH"
    echo "Usage: ./run.sh --path /path/to/coreskill"
    exit 1
fi

if [ -z "${OPENROUTER_API_KEY:-}" ]; then
    echo "WARNING: OPENROUTER_API_KEY not set"
    echo "  Some features (LLM analysis, chat tests) will fail"
    echo ""
fi

# Install deps if needed
if ! python3 -c "import structlog" 2>/dev/null; then
    echo "Installing dependencies..."
    pip install -r requirements.txt --quiet
fi

# Create workspace dirs
mkdir -p workspace logs patches

echo "============================================"
echo "  evo-supervisor"
echo "============================================"
echo "  Coreskill: $CORESKILL_PATH"
echo "  Cycles:    $CYCLES"
echo "  Mode:      ${TEST_ONLY:-full (test + fix)}"
echo "============================================"
echo ""

# Run
if [ "${USE_DOCKER:-}" = "1" ]; then
    CORESKILL_PATH="$CORESKILL_PATH" \
    docker compose up --build supervisor
else
    python3 -m src.main \
        --coreskill-path "$CORESKILL_PATH" \
        --cycles "$CYCLES" \
        $TEST_ONLY \
        $EXTRA_ARGS
fi
