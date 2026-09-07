#!/usr/bin/env bash
# Run the test suite:   bash test.sh
#
# Standard-library unittest only -- no ffmpeg, no audio-separator, no network,
# no data directory. That is deliberate: it means this is runnable on the box
# itself, between `git pull` and `sudo systemctl restart stem`, on the same
# Python the service runs on rather than whichever version a sandbox happens
# to have.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

# Prefer the virtualenv's interpreter, so the tests run against the same
# Python the service does. Fall back to the system one so a fresh checkout
# with no .venv can still be tested.
if [ -x .venv/bin/python ]; then
  python=".venv/bin/python"
else
  python="python3"
fi

echo "python $("$python" -c 'import sys; print(sys.version.split()[0])') — $("$python" -c 'import sys; print(sys.executable)')"

export PYTHONPATH="$here/server"
exec "$python" -m unittest discover -s tests "$@"
