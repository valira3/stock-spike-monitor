#!/bin/bash
# .claude/hooks/audit-command.sh
#
# Fires on PreToolUse for Bash. Logs every command with timestamp and
# session ID. Pattern-matches against risky shapes and routes flagged
# commands to a separate log. Audit trail, NOT a blocker -- always
# exits 0 so the operator can still take destructive actions when
# they mean to.
#
# Adapted from vscarpenter/claude-code-build-system. Project-specific
# risky patterns are tailored to the trading-bot context (force pushes,
# admin merges, hook bypass, etc).

set -u

AUDIT_DIR="${CLAUDE_AUDIT_DIR:-$HOME/.claude/audit/stock-spike-monitor}"
mkdir -p "$AUDIT_DIR"

DATE=$(date '+%Y-%m-%d')
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
LOG_FILE="$AUDIT_DIR/audit-$DATE.log"
FLAGGED_FILE="$AUDIT_DIR/flagged-$DATE.log"

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"')
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // ""')

echo "[$TIMESTAMP] [$SESSION_ID] $COMMAND" >> "$LOG_FILE"

RISKY_PATTERNS=(
  'curl[^|]*\| *sh'
  'curl[^|]*\| *bash'
  'wget[^|]*\| *sh'
  'wget[^|]*\| *bash'
  'eval '
  'rm -rf /'
  'rm -rf \*'
  'sudo rm'
  'chmod 777'
  'git push.* --force'
  'git push.* -f($| )'
  'git reset --hard'
  'git checkout -- '
  '--no-verify'
  '--no-gpg-sign'
  'gh pr merge.*--admin'
  '> *~/\.ssh'
  '> *~/\.aws'
  '\.env\.monitor'
  'FMP_API_KEY='
  'RAILWAY_API_TOKEN='
  'ALPACA_.*_KEY='
  'TELEGRAM.*TOKEN='
)

for pattern in "${RISKY_PATTERNS[@]}"; do
  if echo "$COMMAND" | grep -qE "$pattern"; then
    echo "[$TIMESTAMP] [$SESSION_ID] [pattern: $pattern] $COMMAND" >> "$FLAGGED_FILE"
    break
  fi
done

exit 0
