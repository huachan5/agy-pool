#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
if ! command -v python3.12 >/dev/null 2>&1; then
    printf '%s\n' 'Python 3.12 is required (python3.12 must be on PATH).' >&2
    exit 1
fi
exec python3.12 -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from agy_pool.cli import main; sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8"); raise SystemExit(main())' "$root" "$@"
