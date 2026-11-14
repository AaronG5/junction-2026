#!/usr/bin/env bash
# Linux / macOS launcher (also works in Git Bash on Windows): ./launch.sh
# All the work happens in Python (local_ai/); this only finds Python 3.10+.
set -euo pipefail
cd "$(dirname "$0")"

for candidate in python3 python py; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
        exec "$candidate" -m local_ai "$@"
    fi
done

echo "Python 3.10 or newer is needed and was not found." >&2
case "$(uname -s)" in
    Linux*)  echo "Install it with your package manager, e.g.:  sudo apt install python3" >&2 ;;
    Darwin*) echo "Install it from https://www.python.org/downloads/ or with:  brew install python" >&2 ;;
    *)       echo "Download it from https://www.python.org/downloads/" >&2 ;;
esac
exit 1
