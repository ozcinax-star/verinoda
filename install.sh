#!/bin/sh
# Verinoda installer for macOS and Linux (also works in Git Bash on Windows).
#
#   curl -LsSf https://raw.githubusercontent.com/ozcinax-star/verinoda/main/install.sh | sh
#
# What it does, in order (read it before running it - that is good practice for any piped script):
#   1. uses `uv` if it is on PATH, otherwise installs uv with its official installer (https://astral.sh/uv);
#   2. installs Verinoda as an isolated uv tool: no git and no pre-installed Python needed
#      (uv downloads a suitable Python if none is found). Files are copied, not hardlinked, so
#      sandboxed agents can import the package;
#   3. makes sure the tool directory is on PATH (`uv tool update-shell`);
#   4. prints the next step: `verinoda setup` inside a project.
# Running it again upgrades to the latest code on the chosen ref.
#
# Options (environment variables):
#   VERINODA_REF              branch, tag or commit to install (default: main); a team pins one build
#                             with a full commit sha, and `verinoda --version` names the commit it runs
#   VERINODA_EXTRAS           extras to install (default: precise; set to "none" for none)
#   VERINODA_SPEC             full requirement to install instead (advanced / testing)
#   VERINODA_NO_MODIFY_PATH   set to 1 to leave shell profiles alone
#   VERINODA_NO_UV_INSTALL    set to 1 to fail instead of installing uv

set -eu

say() { printf 'verinoda-install: %s\n' "$*"; }

REPO_URL="https://github.com/ozcinax-star/verinoda"
REF="${VERINODA_REF:-main}"
EXTRAS="${VERINODA_EXTRAS-precise}"

# 1. uv
if ! command -v uv >/dev/null 2>&1; then
    if [ "${VERINODA_NO_UV_INSTALL:-}" = "1" ]; then
        say "uv is not installed and VERINODA_NO_UV_INSTALL=1; install uv first: https://docs.astral.sh/uv/"
        exit 1
    fi
    say "uv not found - installing it with the official installer (https://astral.sh/uv)"
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        say "neither curl nor wget is available; install uv first: https://docs.astral.sh/uv/"
        exit 1
    fi
    for d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
        if [ -x "$d/uv" ]; then PATH="$d:$PATH"; export PATH; fi
    done
    if ! command -v uv >/dev/null 2>&1; then
        say "uv was installed but is not on PATH yet; open a new terminal and run this again"
        exit 1
    fi
fi
say "using $(uv --version)"

# 2. Verinoda
if [ -n "${VERINODA_SPEC:-}" ]; then
    SPEC="$VERINODA_SPEC"
else
    SOURCE="$REPO_URL/archive/$REF.zip"
    if [ -n "$EXTRAS" ] && [ "$EXTRAS" != "none" ]; then
        SPEC="verinoda[$EXTRAS] @ $SOURCE"
    else
        SPEC="verinoda @ $SOURCE"
    fi
fi
say "installing $SPEC"
uv tool install --force --reinstall-package verinoda --link-mode copy "$SPEC"

# 3. PATH
if [ "${VERINODA_NO_MODIFY_PATH:-}" != "1" ]; then
    uv tool update-shell >/dev/null 2>&1 || true
fi
BIN_DIR="$(uv tool dir --bin)"
EXE="$BIN_DIR/verinoda"
[ -x "$EXE" ] || EXE="$BIN_DIR/verinoda.exe"
if [ ! -x "$EXE" ]; then
    say "verinoda not found in $BIN_DIR"
    exit 1
fi
say "installed $("$EXE" --version)"

# 4. next steps
cat <<'EOF'

Next, inside a project folder:
    verinoda setup          # index the code and connect Claude Code / Codex if they are installed
EOF
echo
if [ "${VERINODA_NO_MODIFY_PATH:-}" = "1" ]; then
    echo "PATH was not changed (VERINODA_NO_MODIFY_PATH=1); the program is $EXE"
else
    echo 'If `verinoda` is not found, open a new terminal (PATH was updated for new shells).'
fi
