#!/usr/bin/env bash
# v5.8.0 preflight.sh -- mirrors CI checks locally so a subagent can
# catch issues before `git push`. BLOCKS on the 5 listed checks.
#
# Em-dash and forbidden-word checks are scoped to files CHANGED in this
# PR (vs origin/main), not the entire repo, because the pre-v5.8.0
# codebase contains hundreds of pre-existing literal em-dashes that are
# grandfathered. Future PRs are held to the new standard.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Determine the diff base. Prefer origin/main; fall back to main; then HEAD.
BASE_REF=""
if git rev-parse --verify --quiet origin/main >/dev/null; then
  BASE_REF="origin/main"
elif git rev-parse --verify --quiet main >/dev/null; then
  BASE_REF="main"
fi

CHANGED_PY=()
CHANGED_MD=()
if [ -n "$BASE_REF" ]; then
  while IFS= read -r f; do
    [ -n "$f" ] && [ -f "$f" ] && CHANGED_PY+=("$f")
  done < <(git diff --name-only "$BASE_REF"...HEAD -- '*.py' 2>/dev/null; git diff --name-only -- '*.py' 2>/dev/null; git ls-files --others --exclude-standard -- '*.py' 2>/dev/null)
  while IFS= read -r f; do
    [ -n "$f" ] && [ -f "$f" ] && CHANGED_MD+=("$f")
  done < <(git diff --name-only "$BASE_REF"...HEAD -- '*.md' 2>/dev/null; git diff --name-only -- '*.md' 2>/dev/null; git ls-files --others --exclude-standard -- '*.md' 2>/dev/null)
fi

echo "[1/5] pytest..."
# Run any test_*.py (root level legacy + tests/ dir). -q for terse output.
if [ -d tests ] || ls test_*.py >/dev/null 2>&1; then
  pytest -q tests/ test_*.py 2>/dev/null || pytest -q
else
  echo "  (no tests found)"
fi

echo "[2/5] BOT_VERSION <-> CHANGELOG consistency..."
VERSION=$(grep -E '^BOT_VERSION' bot_version.py | sed -E 's/.*"([0-9.]+)".*/\1/')
CHANGELOG_TOP=$(grep -m1 -E '^## v' CHANGELOG.md | sed -E 's/## v([0-9.]+).*/\1/')
TG_VERSION=$(grep -E '^BOT_VERSION' trade_genius.py | sed -E 's/.*"([0-9.]+)".*/\1/')
if [ "$VERSION" != "$CHANGELOG_TOP" ]; then
  echo "FAIL: bot_version.py BOT_VERSION=$VERSION but CHANGELOG top is v$CHANGELOG_TOP"
  exit 1
fi
if [ "$VERSION" != "$TG_VERSION" ]; then
  echo "FAIL: bot_version.py BOT_VERSION=$VERSION but trade_genius.py BOT_VERSION=$TG_VERSION"
  exit 1
fi
echo "  OK: v$VERSION (bot_version.py == trade_genius.py == CHANGELOG)"

echo "[3/5] em-dash literal check (lines added in this PR only)..."
# Pre-v5.8.0 codebase has hundreds of pre-existing literal em-dashes.
# Only fail on lines this PR ADDS (the '+' side of the diff), so the
# new standard is enforced going forward without grandfathering work.
EM_FOUND=0
EM_DASH=$(python3 -c "import sys; sys.stdout.write('\u2014')")
if [ -n "$BASE_REF" ] && [ "${#CHANGED_PY[@]}" -gt 0 ]; then
  for f in "${CHANGED_PY[@]}"; do
    # Added lines start with '+' (but not '+++' which is the file header).
    ADDED_EM=$(git diff "$BASE_REF"...HEAD -- "$f" 2>/dev/null \
      | grep -E '^\+([^+]|$)' \
      | grep -F "$EM_DASH" || true)
    # Also include uncommitted changes.
    UNCOMMITTED_EM=$(git diff -- "$f" 2>/dev/null \
      | grep -E '^\+([^+]|$)' \
      | grep -F "$EM_DASH" || true)
    # Untracked new files: every line counts as "added".
    if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
      :
    else
      UNCOMMITTED_EM="$UNCOMMITTED_EM"$'\n'"$(grep -nF "$EM_DASH" "$f" 2>/dev/null || true)"
    fi
    COMBINED="$ADDED_EM"$'\n'"$UNCOMMITTED_EM"
    if [ -n "$(echo "$COMBINED" | tr -d '[:space:]')" ]; then
      echo "  FAIL: literal em-dash added by this PR in $f -- use \\u2014 escape"
      echo "$COMBINED" | grep -F "$EM_DASH" | head -5
      EM_FOUND=1
    fi
  done
fi
if [ "$EM_FOUND" -eq 1 ]; then
  exit 1
fi
echo "  OK"

echo "[4/5] forbidden-word check (lines added in this PR only)..."
# Same scoping rationale as em-dash check: enforce going forward.
# Docs that document the rule itself (CLAUDE.md, AGENTS.md, this
# script, CHANGELOG.md, ARCHITECTURE.md) are excluded entirely.
FW_FOUND=0
EXCLUDE_DOCS_RE='^(CLAUDE\.md|AGENTS\.md|scripts/preflight\.sh|CHANGELOG\.md|ARCHITECTURE\.md)$'
FW_RE='\b(scrape|crawl|scraping|crawling)\b'
ALL_CHANGED=("${CHANGED_PY[@]}" "${CHANGED_MD[@]}")
if [ -n "$BASE_REF" ] && [ "${#ALL_CHANGED[@]}" -gt 0 ]; then
  for f in "${ALL_CHANGED[@]}"; do
    if [[ "$f" =~ $EXCLUDE_DOCS_RE ]]; then
      continue
    fi
    ADDED_FW=$(git diff "$BASE_REF"...HEAD -- "$f" 2>/dev/null \
      | grep -E '^\+([^+]|$)' \
      | grep -iE "$FW_RE" || true)
    UNCOMMITTED_FW=$(git diff -- "$f" 2>/dev/null \
      | grep -E '^\+([^+]|$)' \
      | grep -iE "$FW_RE" || true)
    if ! git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
      UNCOMMITTED_FW="$UNCOMMITTED_FW"$'\n'"$(grep -niE "$FW_RE" "$f" 2>/dev/null || true)"
    fi
    COMBINED="$ADDED_FW"$'\n'"$UNCOMMITTED_FW"
    if [ -n "$(echo "$COMBINED" | tr -d '[:space:]')" ]; then
      echo "  FAIL: forbidden word added by this PR in $f"
      echo "$COMBINED" | grep -iE "$FW_RE" | head -5
      FW_FOUND=1
    fi
  done
fi
if [ "$FW_FOUND" -eq 1 ]; then
  exit 1
fi
echo "  OK"

echo "[5/5] format check (ruff)..."
if command -v ruff >/dev/null 2>&1; then
  if [ "${#CHANGED_PY[@]}" -gt 0 ]; then
    ruff check "${CHANGED_PY[@]}" --quiet
    ruff format --check "${CHANGED_PY[@]}" --quiet
  fi
  echo "  OK"
else
  echo "  SKIP: ruff not installed (install with: pip install ruff)"
fi

echo
echo "preflight PASS"
