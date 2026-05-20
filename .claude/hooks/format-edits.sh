#!/bin/bash
# .claude/hooks/format-edits.sh
#
# Fires on PostToolUse for Edit, Write, and MultiEdit. Two responsibilities:
#
#   1. Em-dash check on .py files. CLAUDE.md rule: never use a literal
#      em-dash (U+2014) in Python source; use the `—` escape instead.
#      CHANGELOG/ARCHITECTURE/README .md files are exempt.
#
#   2. Ruff format-check (best-effort, no-op when ruff isn't installed).
#
# Exit 2: blocks/surfaces an issue. Exit 0: clean.
# Always best-effort -- a missing ruff binary or jq error should not
# block edits.

set -u

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

[ -z "$FILE_PATH" ] && exit 0
[ ! -f "$FILE_PATH" ] && exit 0

# 1. Em-dash check (Python only). U+2014 is the literal em-dash byte
# sequence 0xE2 0x80 0x94. BSD grep on macOS does not support -P, so
# we match the raw bytes directly. Works on both BSD and GNU grep.
case "$FILE_PATH" in
  *.py)
    if grep -l $'\xe2\x80\x94' "$FILE_PATH" >/dev/null 2>&1; then
      LINES=$(grep -n $'\xe2\x80\x94' "$FILE_PATH" 2>/dev/null | head -5 | sed 's/^/    /')
      echo "Em-dash literal (U+2014) found in $FILE_PATH. Replace with the \\u2014 escape (CLAUDE.md rule):" >&2
      echo "$LINES" >&2
      exit 2
    fi
    ;;
esac

# 2. Ruff format-check (best-effort).
case "$FILE_PATH" in
  *.py)
    if command -v ruff >/dev/null 2>&1; then
      ruff format --check "$FILE_PATH" 2>&1 | tail -5 || true
    fi
    ;;
esac

exit 0
