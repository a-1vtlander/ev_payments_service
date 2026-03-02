#!/usr/bin/env bash
# run_tests_shuffle.sh – run the full test suite N times with a fresh random
# order each pass.  Unit/mock tests are shuffled; network-dependent sandbox and
# e2e tests are always run last (unshuffled) to avoid DNS flakiness from being
# misread as ordering bugs.
#
# Each pass prints its random seed so failures can be reproduced exactly:
#
#   python -m pytest tests/ --ignore=tests/test_square_sandbox.py \
#          --ignore=tests/test_e2e.py -p randomly --randomly-seed=<SEED>
#
# Usage:
#   ./run_tests_shuffle.sh            # 3 passes (default)
#   PASSES=5 ./run_tests_shuffle.sh   # custom number of passes

set -euo pipefail

PASSES=${PASSES:-3}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

FAILURES=0
SEEDS=()

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Shuffle test run: ${PASSES} passes"
echo "  Unit/mock tests: shuffled   |   sandbox+e2e: last (unshuffled)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

cd "$SCRIPT_DIR"

for ((i = 1; i <= PASSES; i++)); do
    # Generate a unique integer seed for this pass (modulo 2^32 avoids sign issues)
    SEED=$(( (RANDOM * 65536 + RANDOM) % 4294967296 ))
    SEEDS+=("$SEED")

    echo "──────────────────────────────────────────────────"
    echo "  Pass ${i}/${PASSES}   seed=${SEED}"
    echo "──────────────────────────────────────────────────"

    set +e
    python -m pytest tests/ \
        --ignore=tests/test_square_sandbox.py \
        --ignore=tests/test_e2e.py \
        -p randomly --randomly-seed="${SEED}" \
        -q
    UNIT_EXIT=$?

    # Always run network-dependent tests last, unshuffled, so DNS / Square API
    # isn't stressed by a prior wave of async activity.
    python -m pytest tests/test_square_sandbox.py tests/test_e2e.py -q
    NET_EXIT=$?
    set -e

    EXIT=$(( UNIT_EXIT != 0 || NET_EXIT != 0 ? 1 : 0 ))

    if [[ $EXIT -ne 0 ]]; then
        FAILURES=$((FAILURES + 1))
        echo ""
        echo "  ✗ Pass ${i} FAILED (seed=${SEED})"
        echo "    To reproduce: python -m pytest tests/ -p randomly --randomly-seed=${SEED}"
    else
        echo ""
        echo "  ✓ Pass ${i} passed"
    fi
    echo
done

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Results: $((PASSES - FAILURES))/${PASSES} passes succeeded"
echo "  Seeds used: ${SEEDS[*]}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
