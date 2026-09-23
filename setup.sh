#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
git submodule update --init --recursive external/dram-bender
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
