#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ENV_FILE="$ROOT/.env"
FAILURES=0
WARNINGS=0

pass() { printf 'PASS  %s\n' "$1"; }
warn() { printf 'WARN  %s\n' "$1"; WARNINGS=$((WARNINGS + 1)); }
fail() { printf 'FAIL  %s\n' "$1"; FAILURES=$((FAILURES + 1)); }

env_value() {
  awk -v key="$1" 'index($0, key "=") == 1 { print substr($0, length(key) + 2); exit }' "$ENV_FILE"
}

is_placeholder() {
  case "$1" in
    ""|replace-me|cli_replace_me|PLACEHOLDER|replace-with-*) return 0 ;;
    *) return 1 ;;
  esac
}

cd "$ROOT"

if [ ! -f "$ENV_FILE" ]; then
  fail '.env exists'
else
  pass '.env exists'
  for key in MODEL_API_KEY MODEL_API_BASE MODEL_NAME FEISHU_APP_ID FEISHU_APP_SECRET NANOBOT_WEBUI_SECRET; do
    value=$(env_value "$key")
    if is_placeholder "$value"; then
      fail "$key is configured"
    else
      pass "$key is configured"
    fi
  done

  audit_token=$(env_value AUDIT_ADMIN_TOKEN)
  if is_placeholder "$audit_token"; then
    warn 'AUDIT_ADMIN_TOKEN is not separately configured; audit admin falls back to NANOBOT_WEBUI_SECRET'
  else
    pass 'AUDIT_ADMIN_TOKEN is configured'
  fi

  mode=$(stat -f '%Lp' "$ENV_FILE" 2>/dev/null || stat -c '%a' "$ENV_FILE" 2>/dev/null || printf unknown)
  if [ "$mode" = 600 ]; then
    pass '.env mode is 600'
  else
    warn ".env mode is $mode (expected 600)"
  fi
fi

if ! command -v docker >/dev/null 2>&1; then
  fail 'docker is installed'
elif ! docker compose version >/dev/null 2>&1; then
  fail 'docker compose is available'
elif ! docker compose config --quiet >/dev/null 2>&1; then
  fail 'compose configuration is valid'
else
  pass 'compose configuration is valid'
fi

container_id=$(docker compose ps -q nanobot 2>/dev/null || true)
if [ -z "$container_id" ]; then
  fail 'nanobot container is running'
else
  for _attempt in $(seq 1 40); do
    current_health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)
    if [ "$current_health" = healthy ]; then
      break
    fi
    sleep 1
  done
  health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)
  if [ "$health" = healthy ]; then
    pass 'nanobot container is healthy'
  else
    fail "nanobot container health is $health"
  fi

  if docker compose logs --no-color --since=10m nanobot 2>/dev/null | grep -q 'bot started with WebSocket long connection'; then
    pass 'Feishu WebSocket client is connected'
  else
    warn 'no recent Feishu WebSocket startup signal'
  fi

  if docker compose exec -T nanobot python -c \
    'import nanobot.channels.feishu as f; from pathlib import Path; raise SystemExit(0 if "\"sender_id\": sender_id" in Path(f.__file__).read_text() else 1)' \
    >/dev/null 2>&1; then
    pass 'pinned Feishu sender identity patch is active'
  else
    fail 'pinned Feishu sender identity patch is active'
  fi

  if docker compose exec -T nanobot python -c \
    'from nanobot.channels.feishu import FeishuChannel; from nanobot.config.loader import load_config; cfg=load_config().channels.feishu; raise SystemExit(0 if cfg.get("processingCard") is True and cfg.get("streaming") is False and hasattr(FeishuChannel, "_update_processing_card_sync") else 1)' \
    >/dev/null 2>&1; then
    pass 'single-card processing status is active'
  else
    fail 'single-card processing status is active'
  fi

  plugin_count=$(docker compose exec -T nanobot python -c \
    'from importlib.metadata import entry_points; print(sum(1 for ep in entry_points(group="nanobot.tools") if ep.dist and ep.dist.name == "feishu-reminder-mcp"))' \
    2>/dev/null || printf 0)
  if [ "$plugin_count" = 7 ]; then
    pass 'seven identity-bound business tools are installed'
  else
    fail "identity-bound business tool count is $plugin_count (expected 7)"
  fi

  access=$(docker compose exec -T nanobot python -c \
    'from nanobot.config.loader import load_config; f=load_config().channels.feishu; print(f.get("allowFrom"), f.get("groupPolicy"))' \
    2>/dev/null || true)
  if [ "$access" = "['*'] mention" ]; then
    pass 'company-internal Feishu access policy is active'
  else
    warn "unexpected Feishu access policy: $access"
  fi

  target_count=$(docker compose exec -T nanobot python -c \
    'from reminder_mcp.server import store; print(len(store().list_targets()))' \
    2>/dev/null || printf 0)
  if [ "$target_count" -gt 0 ] 2>/dev/null; then
    pass "$target_count delivery target(s) registered"
  else
    warn 'no delivery target is registered'
  fi

  inbound_count=$(docker compose exec -T nanobot python -c \
    'from reminder_mcp.server import store; ctx=store().connect(); db=ctx.__enter__(); print(db.execute("SELECT COUNT(*) FROM message_records WHERE source_message_id IS NOT NULL AND source_chat_id IS NOT NULL").fetchone()[0]); ctx.__exit__(None, None, None)' \
    2>/dev/null || printf 0)
  if [ "$inbound_count" -gt 0 ] 2>/dev/null; then
    pass "$inbound_count real channel record(s) persisted"
  else
    warn 'no real Feishu message has been persisted yet'
  fi
fi

printf '\nManual gate: the Feishu app must select long-connection event delivery and subscribe to im.message.receive_v1.\n'
printf 'Summary: %s failure(s), %s warning(s).\n' "$FAILURES" "$WARNINGS"
[ "$FAILURES" -eq 0 ]
