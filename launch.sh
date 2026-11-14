#!/usr/bin/env bash
# Linux / macOS launcher (also works in Git Bash on Windows): ./launch.sh
#
# Starts the whole app with one command: the AI models (llama.cpp + llama-swap),
# the databases (MySQL + Qdrant in Docker), the dashboard build (frontend/) and
# the backend that serves it. Already-finished steps are skipped on later runs.
#
# Listens on 0.0.0.0 by default, so a server install is reachable from the web.
# For this computer only:  ./launch.sh --local
# A fixed address:         ./launch.sh --host 192.0.2.10
# Other options:           ./launch.sh --help
#
# All the work happens in Python (local_ai/), run through uv. uv also
# downloads a suitable Python by itself, so nothing else needs installing.
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

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

# Where everything should listen. A BIND_HOST already in the environment wins;
# otherwise the choice saved in .env is kept, so editing it there sticks; and a
# first run gets 0.0.0.0, reachable from the web when this is a server.
# --host / --local on the command line still win over all of this.
if [ -z "${BIND_HOST:-}" ] && [ -f "$HERE/.env" ]; then
    BIND_HOST="$(sed -n 's/^[[:space:]]*BIND_HOST[[:space:]]*=[[:space:]]*//p' "$HERE/.env" \
                 | tail -n 1 | tr -d '\r"'"'" )"
fi
export BIND_HOST="${BIND_HOST:-0.0.0.0}"

exec "$UV" run --directory "$HERE" --quiet python -m local_ai "$@"
