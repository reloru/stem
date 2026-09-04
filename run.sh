#!/usr/bin/env bash
# Start the server in the foreground:   bash run.sh
# Reads .env, puts the virtualenv on PATH so audio-separator resolves, and
# runs the stdlib HTTP server. Ctrl-C stops it.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

[ -f .env ] || { echo "no .env found — run: bash setup.sh" >&2; exit 1; }
[ -x .venv/bin/python ] || { echo "no .venv found — run: bash setup.sh" >&2; exit 1; }

set -a
# shellcheck disable=SC1091
. ./.env
set +a

export PATH="$here/.venv/bin:$PATH"
export PYTHONPATH="$here/server"
exec ./.venv/bin/python -m stemapp "$@"
