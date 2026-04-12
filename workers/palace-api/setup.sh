#!/usr/bin/env bash
# ============================================================
# MemPalace Cloudflare Setup + Import
# Sets up D1, Vectorize, deploys the Worker, then imports
# your existing local palace into Cloudflare.
#
# Usage:
#   ./setup.sh                          # full setup + import
#   ./setup.sh --skip-deploy            # import only (Worker already deployed)
#   ./setup.sh --palace /custom/path    # custom palace path
#   ./setup.sh --dry-run                # preview import, no changes
# ============================================================

set -euo pipefail

# ── Colors ──────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

ok()   { echo -e "${GREEN}  ✓${RESET} $*"; }
info() { echo -e "${CYAN}  →${RESET} $*"; }
warn() { echo -e "${YELLOW}  ⚠${RESET} $*"; }
err()  { echo -e "${RED}  ✗${RESET} $*" >&2; }
sep()  { echo -e "\n${BOLD}$*${RESET}"; }

# ── Defaults ────────────────────────────────────────────────
PALACE_PATH="${MEMPALACE_PALACE_PATH:-$HOME/.mempalace/palace}"
SKIP_DEPLOY=false
DRY_RUN=false
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Args ────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-deploy)  SKIP_DEPLOY=true ;;
    --dry-run)      DRY_RUN=true ;;
    --palace)       PALACE_PATH="$2"; shift ;;
    --palace=*)     PALACE_PATH="${1#--palace=}" ;;
    *) err "Unknown option: $1"; exit 1 ;;
  esac
  shift
done

# ── Banner ───────────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║     MemPalace × Cloudflare Setup + Import    ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════════╝${RESET}"
echo ""

# ── Pre-flight checks ────────────────────────────────────────
sep "── Pre-flight checks"

# wrangler
if ! command -v wrangler &>/dev/null; then
  err "wrangler not found. Install it:"
  echo "     npm install -g wrangler"
  exit 1
fi
ok "wrangler $(wrangler --version 2>/dev/null | head -1)"

# node/npm
if ! command -v npm &>/dev/null; then
  err "npm not found. Install Node.js from https://nodejs.org"
  exit 1
fi
ok "npm $(npm --version)"

# python / mempalace
if ! command -v mempalace &>/dev/null; then
  err "mempalace CLI not found. Install it:"
  echo "     pip install -e ~/Projects/mempalace"
  exit 1
fi
ok "mempalace $(mempalace --version 2>/dev/null | head -1 || echo 'found')"

# wrangler auth
if ! wrangler whoami &>/dev/null; then
  warn "Not logged in to Cloudflare. Running wrangler login..."
  wrangler login
fi
ok "Cloudflare auth OK ($(wrangler whoami 2>/dev/null | grep -o '[a-zA-Z0-9._+-]*@[a-zA-Z0-9.-]*' | head -1 || echo 'authenticated'))"

cd "$SCRIPT_DIR"

# ── Install npm deps ─────────────────────────────────────────
sep "── Installing Worker dependencies"
npm install --silent
ok "npm deps installed"

# ── Deploy Worker ────────────────────────────────────────────
if [[ "$SKIP_DEPLOY" == "false" ]]; then
  sep "── Deploying Cloudflare Worker"

  # Create D1 database
  info "Creating D1 database 'mempalace'..."
  D1_OUTPUT=$(wrangler d1 create mempalace 2>&1 || true)

  if echo "$D1_OUTPUT" | grep -q "database_id"; then
    DB_ID=$(echo "$D1_OUTPUT" | grep -oE 'database_id\s*=\s*"[^"]+"' | grep -oE '"[^"]+"' | tr -d '"')
    ok "D1 database created: $DB_ID"
  elif echo "$D1_OUTPUT" | grep -qi "already exists"; then
    # Already exists — fetch existing ID
    DB_ID=$(wrangler d1 list 2>/dev/null | grep mempalace | grep -oE '[0-9a-f-]{36}' | head -1 || true)
    if [[ -z "$DB_ID" ]]; then
      err "D1 database 'mempalace' already exists but couldn't get its ID."
      err "Run: wrangler d1 list"
      err "Then manually update database_id in wrangler.toml"
      exit 1
    fi
    warn "D1 database already exists, using ID: $DB_ID"
  else
    err "Unexpected output from wrangler d1 create:"
    echo "$D1_OUTPUT"
    exit 1
  fi

  # Patch wrangler.toml with real DB ID
  if [[ "$(uname)" == "Darwin" ]]; then
    sed -i '' "s/REPLACE_AFTER_CF_CREATE/$DB_ID/" wrangler.toml
  else
    sed -i "s/REPLACE_AFTER_CF_CREATE/$DB_ID/" wrangler.toml
  fi
  ok "wrangler.toml updated with database_id"

  # Run schema migration
  info "Running D1 schema migration..."
  wrangler d1 execute mempalace --file=./schema.sql 2>&1 | tail -3
  ok "Schema applied"

  # Create Vectorize index
  info "Creating Vectorize index 'mempalace-drawers'..."
  VECT_OUTPUT=$(wrangler vectorize create mempalace-drawers --dimensions=768 --metric=cosine 2>&1 || true)
  if echo "$VECT_OUTPUT" | grep -qi "already exists\|created"; then
    ok "Vectorize index ready"
  else
    warn "Vectorize output: $VECT_OUTPUT"
  fi

  # Set API key secret
  echo ""
  echo -e "${BOLD}  Set a Bearer token for the palace API.${RESET}"
  echo -e "  This protects your palace — pick a strong random string."
  echo -e "  (You'll need it in MEMPALACE_CF_API_KEY later)\n"
  wrangler secret put PALACE_API_KEY

  # Deploy
  info "Deploying Worker..."
  DEPLOY_OUTPUT=$(wrangler deploy 2>&1)
  WORKER_URL=$(echo "$DEPLOY_OUTPUT" | grep -oE 'https://[a-zA-Z0-9._-]+\.workers\.dev' | head -1 || true)

  if [[ -z "$WORKER_URL" ]]; then
    err "Couldn't parse Worker URL from deploy output."
    echo "$DEPLOY_OUTPUT"
    exit 1
  fi

  ok "Worker deployed: $WORKER_URL"

  # ── Write env snippet ──────────────────────────────────────
  ENV_SNIPPET="$HOME/.mempalace/cloudflare_env.sh"
  mkdir -p "$HOME/.mempalace"
  cat > "$ENV_SNIPPET" <<EOF
# MemPalace Cloudflare env — source this or add to ~/.zshrc / ~/.bashrc
export MEMPALACE_BACKEND=cloudflare
export MEMPALACE_CF_API_URL=$WORKER_URL
# Set MEMPALACE_CF_API_KEY to the secret you entered above
# export MEMPALACE_CF_API_KEY=your-secret-here
EOF
  ok "Env snippet saved to $ENV_SNIPPET"

  echo ""
  echo -e "${YELLOW}  ▶ Add to your shell:${RESET}"
  echo -e "    source $ENV_SNIPPET"
  echo -e "    export MEMPALACE_CF_API_KEY=<your-secret>\n"

else
  sep "── Skipping deploy (--skip-deploy)"

  # Validate required env vars
  if [[ -z "${MEMPALACE_CF_API_URL:-}" ]]; then
    err "MEMPALACE_CF_API_URL is not set. Export it before running with --skip-deploy."
    err "If you haven't deployed yet, run without --skip-deploy."
    exit 1
  fi
  if [[ -z "${MEMPALACE_CF_API_KEY:-}" ]]; then
    err "MEMPALACE_CF_API_KEY is not set. Export it before running with --skip-deploy."
    err "This is the secret you set with: wrangler secret put PALACE_API_KEY"
    exit 1
  fi
  WORKER_URL="$MEMPALACE_CF_API_URL"
  ok "Using Worker URL: $WORKER_URL"

  # Quick live check before proceeding
  info "Checking Worker is live..."
  HTTP_CODE=$(curl -sf -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer $MEMPALACE_CF_API_KEY" \
    "$WORKER_URL/status" 2>/dev/null || echo "000")

  if [[ "$HTTP_CODE" == "200" ]]; then
    ok "Worker is live"
  elif [[ "$HTTP_CODE" == "401" ]]; then
    err "Worker responded 401 — MEMPALACE_CF_API_KEY is wrong."
    err "Check the secret you set with: wrangler secret put PALACE_API_KEY"
    exit 1
  elif [[ "$HTTP_CODE" == "000" ]]; then
    err "Worker not reachable at $WORKER_URL"
    err "It may not be deployed yet. Run without --skip-deploy to deploy first."
    exit 1
  else
    warn "Worker returned HTTP $HTTP_CODE — proceeding anyway."
  fi
fi

# ── Verify Worker is alive ───────────────────────────────────
sep "── Verifying Worker"

if [[ -z "${MEMPALACE_CF_API_KEY:-}" ]]; then
  warn "MEMPALACE_CF_API_KEY not set in environment — skipping live check."
  warn "Set it and re-run with --skip-deploy to import your palace."
else
  STATUS=$(curl -sf -H "Authorization: Bearer $MEMPALACE_CF_API_KEY" \
    "$WORKER_URL/status" 2>/dev/null || true)
  if [[ -n "$STATUS" ]]; then
    TOTAL=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total_drawers',0))" 2>/dev/null || echo "?")
    ok "Worker responding — current drawers on CF: $TOTAL"
  else
    warn "Worker not responding or API key wrong. Check your MEMPALACE_CF_API_KEY."
    warn "You can still run the import manually: mempalace sync push --palace $PALACE_PATH"
  fi
fi

# ── Import local palace ──────────────────────────────────────
sep "── Importing local palace → Cloudflare"

if [[ ! -d "$PALACE_PATH" ]]; then
  warn "Local palace not found at $PALACE_PATH"
  warn "Nothing to import. Run later with:"
  warn "  mempalace sync push --palace <your-palace-path>"
else
  info "Palace path: $PALACE_PATH"

  if [[ "$DRY_RUN" == "true" ]]; then
    info "DRY RUN — previewing what would be pushed:"
    MEMPALACE_BACKEND=cloudflare \
      MEMPALACE_CF_API_URL="${MEMPALACE_CF_API_URL:-$WORKER_URL}" \
      MEMPALACE_CF_API_KEY="${MEMPALACE_CF_API_KEY:-}" \
      mempalace --palace "$PALACE_PATH" sync push --dry-run
  else
    MEMPALACE_BACKEND=cloudflare \
      MEMPALACE_CF_API_URL="${MEMPALACE_CF_API_URL:-$WORKER_URL}" \
      MEMPALACE_CF_API_KEY="${MEMPALACE_CF_API_KEY:-}" \
      mempalace --palace "$PALACE_PATH" sync push
  fi
fi

# ── Done ─────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║               Setup complete ✓               ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  Worker URL: ${CYAN}${WORKER_URL:-$MEMPALACE_CF_API_URL}${RESET}"
echo ""
echo -e "  Next steps:"
echo -e "    1. source $HOME/.mempalace/cloudflare_env.sh"
echo -e "    2. export MEMPALACE_CF_API_KEY=<your-secret>"
echo -e "    3. mempalace sync daemon   # keep in sync going forward"
echo ""
echo -e "  Hermes: add to ~/.hermes/.env"
echo -e "    MEMPALACE_BACKEND=cloudflare"
echo -e "    MEMPALACE_CF_API_URL=${WORKER_URL:-\$MEMPALACE_CF_API_URL}"
echo -e "    MEMPALACE_CF_API_KEY=<your-secret>"
echo ""
