#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if [ -f "venv/bin/activate" ]; then
  # shellcheck source=/dev/null
  source venv/bin/activate
else
  echo "Setting up virtual environment..."
  python3 -m venv venv
  # shellcheck source=/dev/null
  source venv/bin/activate
  pip install -r requirements.txt
fi
python run.py
