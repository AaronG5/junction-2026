#!/usr/bin/env bash
# Linux / macOS launcher (also works in Git Bash on Windows): ./launch.sh
# All the work happens in Python (local_ai/), run through uv. uv also
# downloads a suitable Python by itself, so nothing else needs installing.
set -euo pipefail
cd "$(dirname "$0")"

find_uv() {
    if command -v uv >/dev/null 2>&1; then command -v uv; return; fi
    # The uv installer puts uv here; a fresh install may not be on PATH yet.
    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$candidate" ]; then echo "$candidate"; return; fi
    done
}

UV="$(find_uv || true)"
if [ -z "$UV" ]; then
    echo "uv is needed to run Veritas Lens and was not found."
    echo "It is a small tool that manages Python for this project: https://docs.astral.sh/uv/"
    answer="n"
    if [ -t 0 ]; then
        read -r -p "Install uv now? [Y/n] " answer || true
        answer="${answer:-y}"
    fi
    case "$answer" in
        [Yy]*)
            if command -v curl >/dev/null 2>&1; then
                curl -LsSf https://astral.sh/uv/install.sh | sh
            else
                wget -qO- https://astral.sh/uv/install.sh | sh
            fi
            UV="$(find_uv || true)"
            ;;
    esac
fi

if [ -z "$UV" ]; then
    echo "Install uv from https://docs.astral.sh/uv/getting-started/installation/ and run ./launch.sh again." >&2
    exit 1
fi

exec "$UV" run --quiet python -m local_ai "$@"
