#!/usr/bin/env sh
cd "$(dirname "$0")" || exit 1
[ -x .venv/bin/python ] || python3 -m venv .venv || exit 1
[ -f .venv/.deps-ok ] || { .venv/bin/pip install -r requirements.txt && touch .venv/.deps-ok || exit 1; }
exec .venv/bin/python main.py "$@"
