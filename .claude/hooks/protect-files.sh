#!/bin/bash
# .claude/hooks/protect-files.sh
#
# Fires on PreToolUse for Edit, Write, and MultiEdit. Blocks edits to
# files that should never be touched without explicit operator action.
#
# Exit 2: blocks the edit + surfaces the reason to the model + the user.
# Exit 0: allows the edit.
#
# Adapted from vscarpenter/claude-code-build-system for the v10 ORB stack.

set -u

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

[ -z "$FILE_PATH" ] && exit 0

BASENAME=$(basename "$FILE_PATH")

case "$BASENAME" in
  .env|.env.*|*.env|.env.monitor|.env.monitor.example)
    echo "Refused: $FILE_PATH contains live API keys (Alpaca paper keys, Telegram tokens, Railway API token, FMP key). Use Railway dashboard or export-then-edit if you need to rotate them." >&2
    exit 2
    ;;
  bot_version.py)
    echo "Refused: bot_version.py is release-managed. Bumping it triggers the version-bump-check CI gate. Update it only as part of a major-release commit, mirrored in trade_genius.py and the CHANGELOG top heading." >&2
    exit 2
    ;;
  requirements.txt)
    echo "Refused: requirements.txt is hand-curated. Changes here can break Railway boot. Confirm the intent with the operator first." >&2
    exit 2
    ;;
  dashboard_secret.key|paper_state*.json|trade_log.jsonl|state.db)
    echo "Refused: $FILE_PATH is runtime state (committed-by-mistake risk). It lives on the Railway /data volume in production." >&2
    exit 2
    ;;
esac

# Also block edits anywhere under the corpus dirs that are now gitignored.
case "$FILE_PATH" in
  *data/.cache_v2/*|*data/20[0-9][0-9]-*|*data/bars/*|*data/tick-data/*|*data/dynamic_universe/*)
    echo "Refused: $FILE_PATH is in a corpus/cache directory (gitignored). Bar archives + parquet caches are regenerated from the data pipeline." >&2
    exit 2
    ;;
esac

exit 0
