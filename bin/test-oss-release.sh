#!/bin/bash
# Pre-release validation for OSS sync to egregore-core.
# Run this before /sync-public to catch issues that would break a clean clone.
#
# Usage: bash bin/test-oss-release.sh [--fix]
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  echo "Usage: test-oss-release.sh [--fix]"
  echo ""
  echo "Pre-release validation for OSS sync to egregore-core."
  echo "Checks syntax, internal references, .env handling,"
  echo "JSON construction, shellcheck, and local mode support."
  echo ""
  echo "Options:"
  echo "  --fix  Attempt to auto-fix issues (where possible)"
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PASS=0
FAIL=0
WARN=0

pass() { PASS=$((PASS + 1)); echo "  [PASS] $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  [FAIL] $1"; }
warn() { WARN=$((WARN + 1)); echo "  [WARN] $1"; }

echo "=== OSS Release Validation ==="
echo ""

# --- 1. Syntax check ---
echo "1. Syntax check (bash -n)"
for f in "$SCRIPT_DIR"/bin/*.sh; do
  fname=$(basename "$f")
  if bash -n "$f" 2>/dev/null; then
    pass "$fname"
  else
    fail "$fname — syntax error"
  fi
done
echo ""

# --- 2. No hardcoded internal references ---
echo "2. Internal reference check"
# These are legitimate public defaults (OAuth Client ID is public by design, upstream is the OSS repo).
# Check that they appear WITH a config override mechanism (not as bare hardcoded values).
CONFIGURABLE_DEFAULTS=(
  "Iv23li2obNsAjakoK2RE"    # GitHub App Client ID — must have config override nearby
  "Curve-Labs/egregore-core" # Upstream URL — must have config override nearby
)

for pattern in "${CONFIGURABLE_DEFAULTS[@]}"; do
  files=$(grep -rn "$pattern" "$SCRIPT_DIR/bin/" --include='*.sh' 2>/dev/null | grep -v 'test-' || true)
  if [ -z "$files" ]; then
    pass "Default not found (OK if moved to config): $pattern"
  else
    # Verify there's a config override nearby in the same file
    has_override=false
    for file in $(echo "$files" | cut -d: -f1 | sort -u); do
      if grep -q 'egregore.json' "$file" 2>/dev/null; then
        has_override=true
        break
      fi
    done
    if $has_override; then
      pass "Default with config override: $pattern"
    else
      warn "Default without config override: $pattern"
    fi
  fi
done
echo ""

# --- 3. No source .env (should use grep|cut) ---
echo "3. Safe .env handling"
UNSAFE_ENV=$(grep -rn 'source.*\.env' "$SCRIPT_DIR/bin/" --include='*.sh' 2>/dev/null | grep -v 'test-' | grep -v '#' || true)
if [ -n "$UNSAFE_ENV" ]; then
  fail "Found 'source .env' — should use grep|cut extraction:"
  echo "$UNSAFE_ENV" | head -5
else
  pass "No unsafe 'source .env' usage"
fi
echo ""

# --- 4. No manual JSON construction ---
echo "4. JSON construction safety"
# Look for the pattern: "{\"key\":\"$VAR\"}"
UNSAFE_JSON=$(grep -rn '"{\\\"' "$SCRIPT_DIR/bin/" --include='*.sh' 2>/dev/null | grep -v 'test-' || true)
if [ -n "$UNSAFE_JSON" ]; then
  fail "Found manual JSON construction (use jq -n instead):"
  echo "$UNSAFE_JSON" | head -5
else
  pass "All JSON uses jq -n construction"
fi
echo ""

# --- 5. ShellCheck (if available) ---
echo "5. ShellCheck"
if command -v shellcheck &>/dev/null; then
  SC_ERRORS=0
  for f in "$SCRIPT_DIR"/bin/*.sh; do
    fname=$(basename "$f")
    # Skip test scripts
    [[ "$fname" == test-* ]] && continue
    if ! shellcheck -S warning "$f" 2>/dev/null; then
      SC_ERRORS=$((SC_ERRORS + 1))
      warn "$fname — shellcheck warnings"
    fi
  done
  if [ "$SC_ERRORS" -eq 0 ]; then
    pass "All scripts pass shellcheck"
  fi
else
  warn "shellcheck not installed — skipping (brew install shellcheck)"
fi
echo ""

# --- 6. Local mode graceful degradation ---
echo "6. Local mode checks"
# Verify key scripts handle local mode
for script in graph.sh notify.sh graph-op.sh; do
  if grep -q '"local"' "$SCRIPT_DIR/bin/$script" 2>/dev/null; then
    pass "$script handles local mode"
  else
    warn "$script may not handle local mode"
  fi
done
echo ""

# --- Summary ---
echo "==================================="
echo "Results: $PASS passed, $FAIL failed, $WARN warnings"
echo "==================================="

if [ "$FAIL" -gt 0 ]; then
  echo ""
  echo "Fix failures before running /sync-public."
  exit 1
else
  echo ""
  echo "Ready for OSS sync."
  exit 0
fi
