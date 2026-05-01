#!/usr/bin/env bash
# =============================================================================
#  TMT Translator — run.sh
#  Starts the app locally (after setup.sh) or via Docker
#
#  Usage:
#    bash run.sh            # local mode (default)
#    bash run.sh --docker   # Docker Compose mode
#    bash run.sh --port 8080  # custom port
#    bash run.sh --help
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }

PORT=5050
USE_DOCKER=false

# ── Argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --docker)    USE_DOCKER=true; shift ;;
        --port)      PORT="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: bash run.sh [--docker] [--port PORT]"
            echo "  --docker   Run via Docker Compose (requires Docker)"
            echo "  --port N   Port to listen on (default: 5050)"
            exit 0 ;;
        *) error "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${CYAN}║    TMT Translator v2 — Trilingual Document Tool      ║${NC}"
echo -e "${BOLD}${CYAN}║    English ↔ Nepali ↔ Tamang  |  PDF DOCX CSV TSV   ║${NC}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""

# ── Check directory ───────────────────────────────────────────────────────────
if [[ ! -f "app.py" ]]; then
    error "Run this script from inside the tmt-translator/ directory"
    exit 1
fi

# ── Check .env ────────────────────────────────────────────────────────────────
if [[ ! -f ".env" ]]; then
    error ".env file not found — run 'bash setup.sh' first"
    exit 1
fi

if grep -q "YOUR_API_KEY_HERE" .env 2>/dev/null; then
    error "TMT_API_KEY is still a placeholder in .env"
    error "Edit .env and set your real API key, then re-run."
    exit 1
fi

# ─────────────────────────────────────────────────────────────────────────────
#  DOCKER MODE
# ─────────────────────────────────────────────────────────────────────────────
if [[ "$USE_DOCKER" == true ]]; then
    if ! command -v docker &>/dev/null; then
        error "Docker is not installed — visit https://docs.docker.com/get-docker/"
        exit 1
    fi

    info "Building and starting Docker container..."
    docker compose up --build -d

    echo ""
    success "Container started!"
    echo -e "  🌐 Open: ${BOLD}http://localhost:${PORT}${NC}"
    echo -e "  📋 Logs: ${BOLD}docker compose logs -f${NC}"
    echo -e "  🛑 Stop: ${BOLD}docker compose down${NC}"
    echo ""
    exit 0
fi

# ─────────────────────────────────────────────────────────────────────────────
#  LOCAL MODE
# ─────────────────────────────────────────────────────────────────────────────
if [[ ! -d "venv" ]]; then
    warn "venv not found — running setup.sh first..."
    bash setup.sh
fi

# Activate virtualenv
# shellcheck disable=SC1091
source venv/bin/activate

# Verify API key is loaded
export $(grep -v '^#' .env | grep -v '^$' | xargs)
if [[ -z "${TMT_API_KEY:-}" ]]; then
    error "TMT_API_KEY is not set. Check your .env file."
    exit 1
fi

success "Environment ready"
info "Starting TMT Translator on port $PORT..."
echo ""
echo -e "  🌐 Open: ${BOLD}http://localhost:${PORT}${NC}"
echo -e "  🛑 Stop: ${BOLD}Ctrl+C${NC}"
echo ""

python app.py --host 0.0.0.0 --port "$PORT"
