#!/bin/bash
# Daily Taiwan stock analysis runner
cd /Users/apple/stock-analysis
source .venv/bin/activate
unset PYTHONPATH
# Run with Taiwan stocks
python3 main.py --stocks 2330.TW,2317.TW,2454.TW,2308.TW,2881.TW,3711.TW,3008.TW --market-review 2>&1
