#!/usr/bin/env bash
# Convenience launcher for Appium MCP Studio.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Creando entorno virtual..."
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install --quiet -r requirements.txt

if [ "${1:-}" = "--mcp" ]; then
  shift
  exec python src/studio_mcp.py "$@"
fi

exec streamlit run src/app.py --server.address 127.0.0.1
