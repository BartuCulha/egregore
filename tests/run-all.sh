#!/bin/bash
# Run all Egregore tests
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TOTAL_PASS=0
TOTAL_FAIL=0

for test_file in "$SCRIPT_DIR"/test-*.sh; do
  [ -f "$test_file" ] || continue
  echo ""
  if bash "$test_file"; then
    TOTAL_PASS=$((TOTAL_PASS + 1))
  else
    TOTAL_FAIL=$((TOTAL_FAIL + 1))
  fi
done

echo ""
echo "================================"
echo "Suites: $TOTAL_PASS passed, $TOTAL_FAIL failed"
if [ "$TOTAL_FAIL" -gt 0 ]; then
  echo "SOME TESTS FAILED"
  exit 1
else
  echo "ALL TESTS PASSED"
fi
