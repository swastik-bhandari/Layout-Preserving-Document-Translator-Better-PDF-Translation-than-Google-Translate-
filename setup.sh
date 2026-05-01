#!/usr/bin/env bash
# =============================================================================
#  TMT Translator — setup.sh
#  One-time environment setup for local (non-Docker) development
#  Usage:  bash setup.sh
# =============================================================================
set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
header()  { echo -e "\n${BOLD}${CYAN}$*${NC}"; }

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${CYAN}║    TMT Translator — Environment Setup                ║${NC}"
echo -e "${BOLD}${CYAN}║    LowResource Labs · Google TMT Hackathon 2026      ║${NC}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""

# ── Check we're in the right directory ───────────────────────────────────────
if [[ ! -f "app.py" ]]; then
    error "Run this script from inside the tmt-translator/ directory"
    error "Example: cd tmt-translator && bash setup.sh"
    exit 1
fi

# ── Check Python version ──────────────────────────────────────────────────────
header "Checking Python version..."
PYTHON_CMD=""
for cmd in python3.12 python3.11 python3.10 python3 python; do
    if command -v "$cmd" &>/dev/null; then
        VER=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        MAJOR=${VER%%.*}; MINOR=${VER##*.}
        if [[ "$MAJOR" -eq 3 && "$MINOR" -ge 10 ]]; then
            PYTHON_CMD="$cmd"
            success "Found $cmd (Python $VER)"
            break
        fi
    fi
done

if [[ -z "$PYTHON_CMD" ]]; then
    error "Python 3.10+ is required but not found."
    error "Install with: sudo apt install python3.12  OR  brew install python@3.12"
    exit 1
fi

# ── Check pip ─────────────────────────────────────────────────────────────────
header "Checking pip..."
if ! "$PYTHON_CMD" -m pip --version &>/dev/null; then
    error "pip not found for $PYTHON_CMD"
    error "Install with: $PYTHON_CMD -m ensurepip --upgrade"
    exit 1
fi
success "pip is available"

# ── System dependencies (Linux only) ─────────────────────────────────────────
header "Checking system dependencies..."
if [[ "$(uname -s)" == "Linux" ]]; then
    MISSING_PKGS=()
    for pkg in libpango-1.0-0 libcairo2; do
        if ! dpkg -s "$pkg" &>/dev/null 2>&1; then
            MISSING_PKGS+=("$pkg")
        fi
    done

    if [[ ${#MISSING_PKGS[@]} -gt 0 ]]; then
        info "Installing system dependencies (requires sudo)..."
        sudo apt-get update -qq
        sudo apt-get install -y --no-install-recommends \
            libpango-1.0-0 libpangoft2-1.0-0 libpangocairo-1.0-0 \
            libcairo2 libgdk-pixbuf2.0-0 shared-mime-info \
            fonts-freefont-ttf fonts-noto fonts-noto-core \
            && sudo fc-cache -f
        success "System dependencies installed"
    else
        success "System dependencies already present"
    fi
elif [[ "$(uname -s)" == "Darwin" ]]; then
    warn "macOS detected — if WeasyPrint fails, run: brew install pango cairo"
fi

# ── Virtual environment ───────────────────────────────────────────────────────
header "Setting up Python virtual environment..."
if [[ -d "venv" ]]; then
    warn "venv/ already exists — skipping creation (delete it to recreate)"
else
    "$PYTHON_CMD" -m venv venv
    success "Created venv/"
fi

# Activate
# shellcheck disable=SC1091
source venv/bin/activate
success "Activated venv"

# ── Python packages ───────────────────────────────────────────────────────────
header "Installing Python packages..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
success "All Python packages installed"

# ── .env check ────────────────────────────────────────────────────────────────
header "Checking .env configuration..."
if [[ ! -f ".env" ]]; then
    warn ".env file not found — creating template..."
    cat > .env <<'EOF'
# TMT API credentials — edit these before running!
TMT_API_URL=https://tmt.ilprl.ku.edu.np/lang-translate
TMT_API_KEY=YOUR_API_KEY_HERE
EOF
    warn "⚠  Edit .env and add your TMT_API_KEY before running the app"
else
    if grep -q "YOUR_API_KEY_HERE" .env 2>/dev/null; then
        warn "⚠  .env contains placeholder API key — update TMT_API_KEY before running"
    else
        success ".env looks configured"
    fi
fi

# ── Final summary ─────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${GREEN}║  ✅  Setup complete!                                 ║${NC}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Run the app:   ${BOLD}bash run.sh${NC}"
echo -e "  Or manually:   ${BOLD}source venv/bin/activate && python app.py${NC}"
echo ""
