#!/usr/bin/env bash
# preflight.sh — verify PollyPM's runtime dependencies before installation.
#
# The previous install story was `git clone` + `uv pip install -e .`, which
# fails opaquely when (for example) uv itself isn't on the user's PATH or
# their Python is too old. This script checks every dep first and prints
# per-item install hints so the user knows exactly what to fix.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/samhotchkiss/pollypm/main/scripts/preflight.sh | bash
#   # or, from a cloned repo:
#   bash scripts/preflight.sh
#
# Exit codes:
#   0 — every required dep is present and the version checks pass.
#   1 — one or more required deps are missing.
#   2 — the script itself hit an unexpected shell error (set -u / set -e).

set -u

# -- OS + package-manager detection ------------------------------------------
# Stored as two env vars the install hints can interpolate. Falls back to
# sensible defaults (apt for Linux, brew for macOS) when detection can't
# pin the distro.
UNAME="$(uname -s 2>/dev/null || echo unknown)"
case "$UNAME" in
  Darwin) PKG_MGR="brew" ;;
  Linux)
    if command -v apt-get >/dev/null 2>&1; then
      PKG_MGR="apt"
    elif command -v dnf >/dev/null 2>&1; then
      PKG_MGR="dnf"
    elif command -v pacman >/dev/null 2>&1; then
      PKG_MGR="pacman"
    else
      PKG_MGR="apt"
    fi
    ;;
  *) PKG_MGR="brew" ;;
esac

# -- Terminal colors (degrade to plain text on non-TTYs / NO_COLOR) ----------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_BOLD=$(printf '\033[1m')
  C_GREEN=$(printf '\033[0;32m')
  C_RED=$(printf '\033[0;31m')
  C_DIM=$(printf '\033[2m')
  C_RESET=$(printf '\033[0m')
else
  C_BOLD=''
  C_GREEN=''
  C_RED=''
  C_DIM=''
  C_RESET=''
fi

MISSING=0
OPTIONAL_MISSING=0

# install_hint "tool" → echo the right install command for this OS
install_hint() {
  local tool="$1"
  case "$tool:$PKG_MGR" in
    uv:brew)   echo "brew install uv" ;;
    uv:*)      echo "curl -LsSf https://astral.sh/uv/install.sh | sh" ;;
    tmux:brew) echo "brew install tmux" ;;
    tmux:apt)  echo "sudo apt install tmux" ;;
    tmux:dnf)  echo "sudo dnf install tmux" ;;
    tmux:pacman) echo "sudo pacman -S tmux" ;;
    git:brew)  echo "brew install git" ;;
    git:apt)   echo "sudo apt install git" ;;
    git:dnf)   echo "sudo dnf install git" ;;
    git:pacman) echo "sudo pacman -S git" ;;
    gh:brew)   echo "brew install gh" ;;
    gh:apt)    echo "sudo apt install gh" ;;
    gh:dnf)    echo "sudo dnf install gh" ;;
    gh:pacman) echo "sudo pacman -S github-cli" ;;
    claude:*)  echo "npm install -g @anthropic-ai/claude-cli" ;;
    codex:*)   echo "npm install -g @openai/codex" ;;
    python3:brew) echo "brew install python@3.12" ;;
    python3:apt)  echo "sudo apt install python3.12" ;;
    python3:*)    echo "install Python 3.11+ from https://www.python.org/" ;;
    *) echo "" ;;
  esac
}

# check name binary required
#   required=1 → missing increments $MISSING and shows ✗.
#   required=0 → missing increments $OPTIONAL_MISSING and shows ○.
check() {
  local label="$1"
  local bin="$2"
  local required="${3:-1}"
  local hint
  hint="$(install_hint "$bin")"
  if command -v "$bin" >/dev/null 2>&1; then
    printf "  ${C_GREEN}✓${C_RESET} %-18s ${C_DIM}%s${C_RESET}\n" \
      "$label" "$(command -v "$bin")"
  else
    if [ "$required" -eq 1 ]; then
      printf "  ${C_RED}✗${C_RESET} %-18s ${C_RED}MISSING${C_RESET}  — %s\n" \
        "$label" "$hint"
      MISSING=$((MISSING + 1))
    else
      printf "  ${C_DIM}○${C_RESET} %-18s ${C_DIM}optional, not found. $hint${C_RESET}\n" \
        "$label"
      OPTIONAL_MISSING=$((OPTIONAL_MISSING + 1))
    fi
  fi
}

printf "\n${C_BOLD}PollyPM preflight check${C_RESET}  ${C_DIM}(OS=%s, pkg=%s)${C_RESET}\n\n" \
  "$UNAME" "$PKG_MGR"

echo "Required:"
check "python 3.11+"    python3 1
check "uv"              uv 1
check "tmux"            tmux 1
check "git"             git 1

echo
echo "Providers (at least one required for any real work):"
PROVIDERS_FOUND=0
if command -v claude >/dev/null 2>&1; then PROVIDERS_FOUND=$((PROVIDERS_FOUND + 1)); fi
if command -v codex  >/dev/null 2>&1; then PROVIDERS_FOUND=$((PROVIDERS_FOUND + 1)); fi
check "claude CLI"      claude 0
check "codex CLI"       codex 0
if [ "$PROVIDERS_FOUND" -eq 0 ]; then
  printf "  ${C_RED}✗ no provider CLI found${C_RESET} — PollyPM can boot, but every\n"
  printf "    session will fail to launch. Install at least one before first run.\n"
  MISSING=$((MISSING + 1))
fi

echo
echo "Optional but recommended:"
check "gh (GitHub CLI)" gh 0

# -- Python version check (beyond "python3 exists") --------------------------
PY_BIN="$(command -v python3 2>/dev/null || true)"
if [ -n "$PY_BIN" ]; then
  PY_VERSION="$("$PY_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "?")"
  PY_MAJOR="${PY_VERSION%%.*}"
  PY_MINOR="${PY_VERSION##*.}"
  if [ "$PY_MAJOR" = "3" ] && [ -n "$PY_MINOR" ] && [ "$PY_MINOR" -lt 11 ] 2>/dev/null; then
    echo
    printf "  ${C_RED}✗ python3 found but version is %s — PollyPM needs 3.11+${C_RESET}\n" \
      "$PY_VERSION"
    printf "    %s\n" "$(install_hint python3)"
    MISSING=$((MISSING + 1))
  fi
fi

# -- Final verdict ----------------------------------------------------------
echo
if [ "$MISSING" -gt 0 ]; then
  printf "${C_RED}✗ %d required dependency/dependencies missing.${C_RESET}\n" "$MISSING"
  echo "Install the items marked ${C_RED}✗${C_RESET} above, then re-run this script."
  exit 1
fi

printf "${C_GREEN}✓ All required dependencies present.${C_RESET}\n"
if [ "$OPTIONAL_MISSING" -gt 0 ]; then
  printf "${C_DIM}  (%d optional dep(s) missing — fine for first run.)${C_RESET}\n" \
    "$OPTIONAL_MISSING"
fi
echo
echo "Ready to install PollyPM:"
printf "  ${C_BOLD}git clone https://github.com/samhotchkiss/pollypm ~/dev/pollypm${C_RESET}\n"
printf "  ${C_BOLD}cd ~/dev/pollypm && uv pip install -e .${C_RESET}\n"
printf "  ${C_BOLD}pm doctor${C_RESET}\n"
printf "  ${C_BOLD}pm${C_RESET}\n"
