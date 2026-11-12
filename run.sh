#!/usr/bin/env bash
# Chạy CV Tailor: http://127.0.0.1:8000  (cấu hình trong file .env)
cd "$(dirname "$0")"
[ -x .venv/bin/python ] && .venv/bin/python -c "import sys" 2>/dev/null || { rm -rf .venv; python3 -m venv .venv; }
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt || exit 1
exec .venv/bin/python -m app "$@"
