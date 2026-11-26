#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
if command -v python3.12 >/dev/null 2>&1; then
    py_cmd=python3.12
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1; then
    py_cmd=python3
else
    printf '%s\n' 'Python 3.12+ is required (python3.12 or python3 >= 3.12 must be on PATH).' >&2
    exit 1
fi
exec "$py_cmd" -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from agy_pool.cli import main; sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8"); raise SystemExit(main())' "$root" "$@"
