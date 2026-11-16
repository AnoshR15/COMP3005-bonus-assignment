#!/bin/sh
# Runs the whole suite from the project root, with python3 when that is the installed name.
cd "$(dirname "$0")" || exit 1
if command -v python3 >/dev/null 2>&1; then
    python3 -m unittest -v
else
    python -m unittest -v
fi
