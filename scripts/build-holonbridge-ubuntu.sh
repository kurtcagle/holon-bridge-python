#!/usr/bin/env bash
#
# build-holonbridge-ubuntu.sh — one-time (re-runnable) build of
# kurtcagle/holon-bridge-python on an Ubuntu box.
#
# What this does NOT do:
#   - Install or configure Fuseki. That's a separate prerequisite
#     (FUSEKI_HOME in scripts/holonbridge-ctl.sh points at it).
#   - Start anything. Once this finishes, use the repo's own
#     scripts/holonbridge-ctl.sh {start|stop|status} to run the stack.
#   - Touch ngrok config. Do that by hand — see the note this script
#     prints at the end.
#
# What it does:
#   1. Installs Ubuntu build prerequisites (git, python3-venv/dev, build-essential)
#   2. Clones or updates the repo at INSTALL_DIR (git, not the GitHub MCP —
#      cheaper for a full checkout)
#   3. Creates/reuses a venv at INSTALL_DIR/.venv
#   4. pip installs the package in editable mode, pinned via constraints.txt
#   5. Seeds .env from .env.example if missing, and auto-generates BEARER_TOKEN
#   6. Verifies the holonbridge / holonbridge-mcp console scripts exist
#
# Usage:
#   ./build-holonbridge-ubuntu.sh
#   INSTALL_DIR=/opt/holon-bridge-python BRANCH=main HOLONBRIDGE_EXTRAS=mcp,shacl,dev ./build-holonbridge-ubuntu.sh
#
# Safe to re-run: existing clone is updated in place, existing venv and
# .env are left untouched (pass --reset-venv to force a clean venv).

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — override via environment
# ---------------------------------------------------------------------------

REPO_URL="${REPO_URL:-https://github.com/kurtcagle/holon-bridge-python.git}"
BRANCH="${BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/holon-bridge-python}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
MIN_PY_MINOR=10   # pyproject.toml requires-python = ">=3.10"

# Matches the repo's own documented default (mcp,dev). shacl is added here
# because SHACL_REQUIRED/SHACL_DELTA are live on Kurt's actual deployments
# (causalspark, vm-fuseki) — override with HOLONBRIDGE_EXTRAS if not wanted.
HOLONBRIDGE_EXTRAS="${HOLONBRIDGE_EXTRAS:-mcp,shacl,dev}"

RESET_VENV="false"
for arg in "$@"; do
  [[ "$arg" == "--reset-venv" ]] && RESET_VENV="true"
done

log()  { echo "[build-holonbridge] $*"; }
err()  { echo "[build-holonbridge] ERROR: $*" >&2; }

# ---------------------------------------------------------------------------
# 0. Sanity checks
# ---------------------------------------------------------------------------

if [[ ! -f /etc/os-release ]] || ! grep -qiE 'ubuntu|debian' /etc/os-release; then
  err "This script uses apt-get and expects Ubuntu/Debian. Detected:"
  cat /etc/os-release 2>/dev/null || true
  exit 1
fi

if [[ $EUID -eq 0 ]]; then
  err "Don't run this as root directly — it calls sudo only where needed"
  err "(apt-get, and creating $INSTALL_DIR if it doesn't exist yet)."
  exit 1
fi

# ---------------------------------------------------------------------------
# 1. System prerequisites
# ---------------------------------------------------------------------------

log "Installing system prerequisites (sudo apt-get)..."
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
  git curl ca-certificates \
  python3 python3-venv python3-dev python3-pip \
  build-essential

PY_MINOR="$("$PYTHON_BIN" -c 'import sys; print(sys.version_info[1])')"
PY_MAJOR="$("$PYTHON_BIN" -c 'import sys; print(sys.version_info[0])')"
if [[ "$PY_MAJOR" -ne 3 || "$PY_MINOR" -lt "$MIN_PY_MINOR" ]]; then
  err "$PYTHON_BIN is $PY_MAJOR.$PY_MINOR — holonbridge requires >=3.$MIN_PY_MINOR"
  err "This box's default python3 is too old. Options:"
  err "  - Add the deadsnakes PPA and install python3.${MIN_PY_MINOR}, then re-run with"
  err "    PYTHON_BIN=python3.${MIN_PY_MINOR} ./build-holonbridge-ubuntu.sh"
  err "  - Or use an Ubuntu release whose default python3 is >=3.${MIN_PY_MINOR} (22.04+)"
  exit 1
fi
log "Using $($PYTHON_BIN --version)"

# ---------------------------------------------------------------------------
# 2. Clone or update the repo
# ---------------------------------------------------------------------------

if [[ ! -d "$INSTALL_DIR" ]]; then
  log "Creating $INSTALL_DIR (sudo, then handing ownership to $USER)..."
  sudo mkdir -p "$(dirname "$INSTALL_DIR")"
  sudo git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
  sudo chown -R "$USER":"$USER" "$INSTALL_DIR"
elif [[ -d "$INSTALL_DIR/.git" ]]; then
  log "Updating existing checkout at $INSTALL_DIR..."
  git -C "$INSTALL_DIR" fetch origin "$BRANCH"
  git -C "$INSTALL_DIR" checkout "$BRANCH"
  git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
else
  err "$INSTALL_DIR exists but isn't a git checkout — refusing to overwrite it."
  err "Move it aside or set INSTALL_DIR to a fresh path."
  exit 1
fi

cd "$INSTALL_DIR"

# ---------------------------------------------------------------------------
# 3. Virtualenv
# ---------------------------------------------------------------------------

VENV_DIR="$INSTALL_DIR/.venv"
if [[ "$RESET_VENV" == "true" && -d "$VENV_DIR" ]]; then
  log "--reset-venv passed — removing existing venv..."
  rm -rf "$VENV_DIR"
fi

if [[ ! -d "$VENV_DIR" ]]; then
  log "Creating venv at $VENV_DIR..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
else
  log "Reusing existing venv at $VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
pip install --upgrade pip --quiet

# ---------------------------------------------------------------------------
# 4. Install the package (constrained, per the repo's own README recipe)
# ---------------------------------------------------------------------------

log "Installing holonbridge[$HOLONBRIDGE_EXTRAS] with constraints.txt..."
if [[ -f "$INSTALL_DIR/constraints.txt" ]]; then
  pip install -c constraints.txt -e ".[${HOLONBRIDGE_EXTRAS}]"
else
  log "No constraints.txt found — installing unconstrained"
  pip install -e ".[${HOLONBRIDGE_EXTRAS}]"
fi

deactivate

# ---------------------------------------------------------------------------
# 5. .env
# ---------------------------------------------------------------------------

ENV_FILE="$INSTALL_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  if [[ -f "$INSTALL_DIR/.env.example" ]]; then
    log "Seeding .env from .env.example..."
    cp "$INSTALL_DIR/.env.example" "$ENV_FILE"
    TOKEN="$(openssl rand -hex 32)"
    # Only the bare BEARER_TOKEN= line — leaves ANTHROPIC_API_KEY,
    # GITHUB_OAUTH_*, MCP_PUBLIC_URL etc. for you to fill in by hand.
    sed -i "s/^BEARER_TOKEN=.*/BEARER_TOKEN=${TOKEN}/" "$ENV_FILE"
    log "Generated a fresh BEARER_TOKEN and wrote it into .env"
  else
    err ".env.example not found in $INSTALL_DIR — skipping .env setup"
  fi
else
  log ".env already exists — leaving it untouched"
fi

# ---------------------------------------------------------------------------
# 6. Verify
# ---------------------------------------------------------------------------

log "Verifying console scripts..."
for bin in holonbridge holonbridge-mcp; do
  if [[ -x "$VENV_DIR/bin/$bin" ]]; then
    log "  OK: $VENV_DIR/bin/$bin"
  else
    err "  MISSING: $VENV_DIR/bin/$bin — install did not complete cleanly"
    exit 1
  fi
done

cat <<EOF

[build-holonbridge] Build complete.

  Install dir : $INSTALL_DIR
  Venv        : $VENV_DIR
  Env file    : $ENV_FILE

Still manual, by design:
  - Fuseki itself (FUSEKI_HOME in scripts/holonbridge-ctl.sh must point at
    a real Jena 6.x install — this script never touches Fuseki)
  - Any secrets .env didn't get auto-filled: ANTHROPIC_API_KEY,
    GITHUB_OAUTH_CLIENT_ID/SECRET, MCP_PUBLIC_URL, MCP_JWT_SECRET,
    MCP_INBOUND_TOKEN, MCP_ALLOWED_GITHUB_LOGINS — see README.md >
    "GitHub OAuth, as a second credential kind" if you're doing remote MCP
  - ngrok: the checked-in authtoken in scripts/holonbridge-ctl.sh should be
    rotated and removed from git history before you rely on this build,
    then re-added locally with 'ngrok config add-authtoken <new-token>'
    (out of scope for this script deliberately — it's a credential, not a
    build step)

Next:
  cd $INSTALL_DIR
  ./scripts/holonbridge-ctl.sh start [--with-mcp]
  ./scripts/holonbridge-ctl.sh status
EOF
