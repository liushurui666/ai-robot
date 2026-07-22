#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
STATE="$ROOT/data/nanobot"
REFRESH_CONFIG=false

case "${1:-}" in
  "") ;;
  --refresh-config) REFRESH_CONFIG=true ;;
  *)
    echo "Usage: $0 [--refresh-config]" >&2
    exit 2
    ;;
esac

mkdir -p \
  "$ROOT/data/audit" \
  "$STATE/workspace/skills/message-digest" \
  "$STATE/workspace/skills/direct-message" \
  "$STATE/workspace/skills/calendar-booking" \
  "$STATE/workspace/skills/image-analysis" \
  "$STATE/reminder"

if [ ! -f "$ROOT/.env" ]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "Created .env; fill in model and Feishu credentials before starting."
fi
chmod 600 "$ROOT/.env"

if [ "$REFRESH_CONFIG" = true ]; then
  cp "$ROOT/config/config.example.json" "$STATE/config.json"
  echo "Refreshed data/nanobot/config.json from the current template"
elif [ ! -f "$STATE/config.json" ]; then
  cp "$ROOT/config/config.example.json" "$STATE/config.json"
  echo "Created data/nanobot/config.json"
fi

cp "$ROOT/config/workspace/skills/message-digest/SKILL.md" \
  "$STATE/workspace/skills/message-digest/SKILL.md"
cp "$ROOT/config/workspace/skills/direct-message/SKILL.md" \
  "$STATE/workspace/skills/direct-message/SKILL.md"
cp "$ROOT/config/workspace/skills/calendar-booking/SKILL.md" \
  "$STATE/workspace/skills/calendar-booking/SKILL.md"
cp "$ROOT/config/workspace/skills/image-analysis/SKILL.md" \
  "$STATE/workspace/skills/image-analysis/SKILL.md"

echo "Bootstrap complete. Next: edit .env, then run docker compose up -d --build"
echo "Audit admin will listen on server localhost port 8780."
