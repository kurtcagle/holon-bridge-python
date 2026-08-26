#!/usr/bin/env bash
#
# holonbridge-ctl.sh — start/stop/status for the HolonBridge stack
#                       (Python build: kurtcagle/holon-bridge-python)
#
# Launches, in order:
#   1. Jena 6.0 Fuseki      (localhost:3030, internal only)
#   2. holonbridge (REST)   (localhost:3031, FastAPI console script from the venv)
#   3. holonbridge-mcp      (stdio — only started standalone if you pass --with-mcp;
#                            normally Claude Code/Desktop launches this itself
#                            as a child process, per the mcpServers config)
#   4. ngrok tunnel         (public endpoint for the MCP layer — see NGROK_* below)
#
# Usage:
#   ./holonbridge-ctl.sh start [--with-mcp]
#   ./holonbridge-ctl.sh stop
#   ./holonbridge-ctl.sh status
#   ./holonbridge-ctl.sh restart [--with-mcp]
#
# Configure the paths/env vars below (or export them before running) to match
# your machine. Defaults assume the project lives at /opt/holon-bridge-python
# with a venv at .venv inside it, per the repo's own README:
#
#   cd /opt/holon-bridge-python
#   python3 -m venv .venv
#   source .venv/bin/activate
#   pip install -e ".[mcp,dev]"
#   cp .env.example .env   # then set BEARER_TOKEN and NGROK_AUTHTOKEN
#
# This is the Linux/bash counterpart to scripts/start-holonbridge.ps1. It
# takes a different shape deliberately: rather than a foreground watcher you
# Ctrl+C to tear everything down, this tracks each process by PID file under
# ~/.holonbridge/run/ so start/stop/restart/status all work independently
# and the stack can run detached (e.g. from a systemd unit or a plain
# `nohup ... &` login-shell exit) instead of needing a terminal held open.
#
# CHANGED 2026-08-26: NGROK_AUTHTOKEN is read from the environment or from
# .env, never hardcoded in this file — an earlier version of this script
# had a real token committed in plain text. If that token is still active,
# rotate it in the ngrok dashboard; simply removing it from this file does
# not remove it from git history. The ngrok tunnel itself also moved from
# an unconditional top-of-file command (which fired on every invocation of
# this script, including `stop` and `status`) into do_start(), with proper
# PID tracking like every other service here.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — override via environment or edit here
# ---------------------------------------------------------------------------

FUSEKI_HOME="${FUSEKI_HOME:-/opt/fuseki}"
FUSEKI_DATASET="${FUSEKI_DATASET:-ds}"
FUSEKI_MODE="${FUSEKI_MODE:-mem}"          # "mem" or "loc"
FUSEKI_DATA_DIR="${FUSEKI_DATA_DIR:-/opt/fuseki}"   # used only when FUSEKI_MODE=loc
FUSEKI_PORT="${FUSEKI_PORT:-3030}"

HOLONBRIDGE_DIR="${HOLONBRIDGE_DIR:-/opt/holon-bridge-python}"
HOLONBRIDGE_VENV="${HOLONBRIDGE_VENV:-$HOLONBRIDGE_DIR/.venv}"
HOLONBRIDGE_PORT="${HOLONBRIDGE_PORT:-3031}"

# Optional one-time-per-identity bootstrap admin (see holonbridge.bootstrap,
# console script holonbridge-bootstrap-admin). Solves the AnimusDep
# chicken-and-egg problem on a genuinely fresh dataset, where no route
# through the REST API can create the very first Person. Idempotent — checks
# by external id before writing, never touches an existing identity's role —
# so it is safe to leave set across every start; a no-op once the identity
# already exists. Unset BOOTSTRAP_ADMIN_GITHUB_USER (the default) means
# nothing changes here at all.
BOOTSTRAP_ADMIN_GITHUB_USER="${BOOTSTRAP_ADMIN_GITHUB_USER:-}"
BOOTSTRAP_ADMIN_SLUG="${BOOTSTRAP_ADMIN_SLUG:-}"
BOOTSTRAP_ADMIN_NAME="${BOOTSTRAP_ADMIN_NAME:-}"
BOOTSTRAP_ADMIN_ROLE="${BOOTSTRAP_ADMIN_ROLE:-admin}"
BOOTSTRAP_ADMIN_DATASET="${BOOTSTRAP_ADMIN_DATASET:-}"   # default: the bank's own dataset
BOOTSTRAP_ADMIN_BANK="${BOOTSTRAP_ADMIN_BANK:-local}"

# ngrok tunnel for the MCP layer. NGROK_AUTHTOKEN is resolved below (env var
# first, then HOLONBRIDGE_DIR/.env) — never hardcoded here. See the header
# note above if a token was previously committed in this file.
NGROK_AUTHTOKEN="${NGROK_AUTHTOKEN:-}"
NGROK_URL="${NGROK_URL:-https://causalspark.ngrok.io}"
NGROK_LOCAL_PORT="${NGROK_LOCAL_PORT:-3034}"

RUN_DIR="${RUN_DIR:-$HOME/.holonbridge/run}"
LOG_DIR="${LOG_DIR:-$HOME/.holonbridge/logs}"

FUSEKI_PID_FILE="$RUN_DIR/fuseki.pid"
BRIDGE_PID_FILE="$RUN_DIR/holonbridge.pid"
MCP_PID_FILE="$RUN_DIR/holonbridge-mcp.pid"
NGROK_PID_FILE="$RUN_DIR/ngrok.pid"

FUSEKI_LOG="$LOG_DIR/fuseki.log"
BRIDGE_LOG="$LOG_DIR/holonbridge.log"
MCP_LOG="$LOG_DIR/holonbridge-mcp.log"
NGROK_LOG="$LOG_DIR/ngrok.log"

STARTUP_TIMEOUT=30   # seconds to wait for each service to come up

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log()  { echo "[holonbridge-ctl] $*"; }
err()  { echo "[holonbridge-ctl] ERROR: $*" >&2; }

is_running() {
  # $1 = pid file
  [[ -f "$1" ]] && kill -0 "$(cat "$1")" 2>/dev/null
}

wait_for_port() {
  # $1 = port, $2 = friendly name, $3 = path to probe (default "/")
  local port="$1" name="$2" path="${3:-/}" waited=0
  while ! curl -s -o /dev/null "http://localhost:${port}${path}"; do
    sleep 1
    waited=$((waited + 1))
    if (( waited >= STARTUP_TIMEOUT )); then
      err "$name did not come up on port $port within ${STARTUP_TIMEOUT}s — check $LOG_DIR"
      return 1
    fi
  done
  log "$name is up on port $port (took ${waited}s)"
}

stop_pid_file() {
  # $1 = pid file, $2 = friendly name
  local pid_file="$1" name="$2"
  if is_running "$pid_file"; then
    local pid
    pid="$(cat "$pid_file")"
    log "Stopping $name (PID $pid)..."
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 10); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
      log "$name did not exit after SIGTERM, sending SIGKILL"
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$pid_file"
  else
    log "$name is not running (no valid PID file)"
    rm -f "$pid_file"
  fi
}

read_env_var() {
  # $1 = key, $2 = .env file path. Prints the value (unquoted), or nothing
  # if the key or file is absent. Reads just this one key rather than
  # sourcing the whole file — .env may contain values this script has no
  # business executing, and dotenv values can contain characters bash
  # would otherwise try to interpret.
  local key="$1" file="$2"
  [[ -f "$file" ]] || return 0
  grep -E "^${key}=" "$file" | tail -n1 | cut -d= -f2- \
    | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'\$//"
}

if [[ -z "$NGROK_AUTHTOKEN" ]]; then
  NGROK_AUTHTOKEN="$(read_env_var NGROK_AUTHTOKEN "$HOLONBRIDGE_DIR/.env")"
fi

# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

do_bootstrap_admin() {
  # Opt-in and idempotent — see the Configuration section above. Talks
  # directly to Fuseki (never through the REST bridge, so it works even
  # before holonbridge itself has started), which is why this runs
  # immediately after Fuseki is confirmed up rather than after holonbridge.
  if [[ -z "$BOOTSTRAP_ADMIN_GITHUB_USER" ]]; then
    return 0
  fi
  if [[ -z "$BOOTSTRAP_ADMIN_SLUG" || -z "$BOOTSTRAP_ADMIN_NAME" ]]; then
    err "BOOTSTRAP_ADMIN_GITHUB_USER is set but BOOTSTRAP_ADMIN_SLUG and/or"
    err "BOOTSTRAP_ADMIN_NAME are not — skipping bootstrap admin"
    return 0
  fi
  if [[ ! -x "$HOLONBRIDGE_VENV/bin/holonbridge-bootstrap-admin" ]]; then
    err "holonbridge-bootstrap-admin not found at"
    err "  $HOLONBRIDGE_VENV/bin/holonbridge-bootstrap-admin"
    err "Rebuild the venv to pick it up: pip install -e \".[mcp,dev]\""
    return 0
  fi

  local dataset_args=()
  [[ -n "$BOOTSTRAP_ADMIN_DATASET" ]] && dataset_args=(--dataset "$BOOTSTRAP_ADMIN_DATASET")

  log "Ensuring bootstrap identity for $BOOTSTRAP_ADMIN_GITHUB_USER (idempotent)..."
  (
    cd "$HOLONBRIDGE_DIR"
    "$HOLONBRIDGE_VENV/bin/holonbridge-bootstrap-admin" \
      --slug "$BOOTSTRAP_ADMIN_SLUG" \
      --name "$BOOTSTRAP_ADMIN_NAME" \
      --github-user "$BOOTSTRAP_ADMIN_GITHUB_USER" \
      --role "$BOOTSTRAP_ADMIN_ROLE" \
      --bank "$BOOTSTRAP_ADMIN_BANK" \
      "${dataset_args[@]}"
  )
}

do_start_ngrok() {
  if is_running "$NGROK_PID_FILE"; then
    log "ngrok already running (PID $(cat "$NGROK_PID_FILE")), skipping"
    return 0
  fi
  if [[ -z "$NGROK_AUTHTOKEN" ]]; then
    err "NGROK_AUTHTOKEN not set (checked the environment and"
    err "  $HOLONBRIDGE_DIR/.env) — skipping the ngrok tunnel."
    err "Add NGROK_AUTHTOKEN=<token> to .env, or export it before running"
    err "this script. Get a token from https://dashboard.ngrok.com if the"
    err "previous one was rotated out."
    return 0
  fi
  if ! command -v ngrok >/dev/null 2>&1; then
    err "ngrok not found on PATH — skipping the tunnel"
    return 0
  fi

  ngrok config add-authtoken "$NGROK_AUTHTOKEN"
  log "Starting ngrok tunnel ($NGROK_LOCAL_PORT -> $NGROK_URL)..."
  nohup ngrok http "$NGROK_LOCAL_PORT" --url "$NGROK_URL" >"$NGROK_LOG" 2>&1 &
  echo $! > "$NGROK_PID_FILE"
}

do_start() {
  local with_mcp="${1:-false}"

  mkdir -p "$RUN_DIR" "$LOG_DIR"

  # --- Fuseki ---
  if is_running "$FUSEKI_PID_FILE"; then
    log "Fuseki already running (PID $(cat "$FUSEKI_PID_FILE")), skipping"
  else
    if [[ ! -x "$FUSEKI_HOME/fuseki-server" ]]; then
      err "fuseki-server not found or not executable at $FUSEKI_HOME/fuseki-server"
      err "Set FUSEKI_HOME, or edit this script, to point at your Jena 6.0 install."
      exit 1
    fi

    log "Starting Fuseki on port $FUSEKI_PORT (dataset: /$FUSEKI_DATASET, mode: $FUSEKI_MODE)..."
    if [[ "$FUSEKI_MODE" == "loc" ]]; then
      mkdir -p "$FUSEKI_DATA_DIR"
      nohup "$FUSEKI_HOME/fuseki-server" --update --loc "$FUSEKI_DATA_DIR" \
        --port "$FUSEKI_PORT" "/$FUSEKI_DATASET" >"$FUSEKI_LOG" 2>&1 &
    else
      nohup "$FUSEKI_HOME/fuseki-server" --update --mem \
        --port "$FUSEKI_PORT" "/$FUSEKI_DATASET" >"$FUSEKI_LOG" 2>&1 &
    fi
    echo $! > "$FUSEKI_PID_FILE"
    wait_for_port "$FUSEKI_PORT" "Fuseki"
  fi

  # --- Bootstrap admin identity (opt-in, idempotent) ---
  # Runs on every start regardless of whether Fuseki was already up — cheap
  # and safe, since it no-ops once the identity exists.
  do_bootstrap_admin

  # --- HolonBridge REST API (Python: FastAPI, console script from the venv) ---
  if is_running "$BRIDGE_PID_FILE"; then
    log "HolonBridge already running (PID $(cat "$BRIDGE_PID_FILE")), skipping"
  else
    if [[ ! -x "$HOLONBRIDGE_VENV/bin/holonbridge" ]]; then
      err "holonbridge console script not found at $HOLONBRIDGE_VENV/bin/holonbridge"
      err "Build it first:"
      err "  cd $HOLONBRIDGE_DIR && python3 -m venv .venv && source .venv/bin/activate"
      err "  pip install -e \".[mcp,dev]\""
      exit 1
    fi
    if [[ ! -f "$HOLONBRIDGE_DIR/.env" ]]; then
      err "$HOLONBRIDGE_DIR/.env not found — copy .env.example to .env and set BEARER_TOKEN"
      exit 1
    fi

    log "Starting HolonBridge on port $HOLONBRIDGE_PORT..."
    (
      cd "$HOLONBRIDGE_DIR"
      nohup "$HOLONBRIDGE_VENV/bin/holonbridge" >"$BRIDGE_LOG" 2>&1 &
      echo $! > "$BRIDGE_PID_FILE"
    )
    # holonbridge exposes GET /health with no auth required — probe that
    # rather than "/", since FastAPI won't serve anything meaningful there.
    wait_for_port "$HOLONBRIDGE_PORT" "HolonBridge" "/health"
  fi

  # --- holonbridge-mcp (standalone, optional) ---
  if [[ "$with_mcp" == "true" ]]; then
    if is_running "$MCP_PID_FILE"; then
      log "holonbridge-mcp already running (PID $(cat "$MCP_PID_FILE")), skipping"
    else
      if [[ ! -x "$HOLONBRIDGE_VENV/bin/holonbridge-mcp" ]]; then
        err "holonbridge-mcp console script not found at $HOLONBRIDGE_VENV/bin/holonbridge-mcp"
        err "It's built by the same 'pip install -e \".[mcp,dev]\"' step as holonbridge."
        exit 1
      fi
      log "Starting holonbridge-mcp (stdio, standalone test mode)..."
      log "NOTE: Claude Code/Desktop normally launches this itself as a child process"
      log "via its own mcpServers config, pointed at $HOLONBRIDGE_VENV/bin/python"
      log "(-m holonbridge_mcp.server) with HOLONBRIDGE_ENV_FILE set to your .env."
      log "Run it standalone only for a smoke test — logs go to $MCP_LOG, but there's"
      log "no interactive stdin/stdout here since nothing is piping into it."
      log "NOTE: this is the stdio variant, not the --transport sse process the"
      log "ngrok tunnel below actually needs for a real remote connection — that"
      log "one currently has to be started separately (see start-holonbridge.ps1"
      log "for the equivalent invocation: python -m holonbridge_mcp --transport"
      log "sse --port $NGROK_LOCAL_PORT)."
      (
        cd "$HOLONBRIDGE_DIR"
        nohup "$HOLONBRIDGE_VENV/bin/holonbridge-mcp" >"$MCP_LOG" 2>&1 &
        echo $! > "$MCP_PID_FILE"
      )
      sleep 1
      if is_running "$MCP_PID_FILE"; then
        log "holonbridge-mcp started (PID $(cat "$MCP_PID_FILE"))"
      else
        err "holonbridge-mcp exited immediately — check $MCP_LOG"
      fi
    fi
  else
    log "Skipping holonbridge-mcp (pass --with-mcp to launch it standalone for testing)."
    log "For normal use, let Claude Code/Desktop start it via its own MCP config."
  fi

  # --- ngrok tunnel ---
  do_start_ngrok

  log "Stack up. Fuseki: http://localhost:$FUSEKI_PORT  HolonBridge: http://localhost:$HOLONBRIDGE_PORT"
}

do_stop() {
  stop_pid_file "$NGROK_PID_FILE" "ngrok"
  stop_pid_file "$MCP_PID_FILE" "holonbridge-mcp"
  stop_pid_file "$BRIDGE_PID_FILE" "HolonBridge"
  stop_pid_file "$FUSEKI_PID_FILE" "Fuseki"
}

do_status() {
  for pair in "Fuseki:$FUSEKI_PID_FILE" "HolonBridge:$BRIDGE_PID_FILE" "holonbridge-mcp:$MCP_PID_FILE" "ngrok:$NGROK_PID_FILE"; do
    local name="${pair%%:*}" pid_file="${pair##*:}"
    if is_running "$pid_file"; then
      echo "  $name: running (PID $(cat "$pid_file"))"
    else
      echo "  $name: not running"
    fi
  done
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

WITH_MCP="false"
for arg in "$@"; do
  [[ "$arg" == "--with-mcp" ]] && WITH_MCP="true"
done

case "${1:-}" in
  start)
    do_start "$WITH_MCP"
    ;;
  stop)
    do_stop
    ;;
  restart)
    do_stop
    sleep 1
    do_start "$WITH_MCP"
    ;;
  status)
    do_status
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status} [--with-mcp]"
    exit 1
    ;;
esac
