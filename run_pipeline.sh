#!/bin/bash

# Interrupt in case of errors (exit code != 0)
set -e
export PYTHONPATH="${PYTHONPATH}:$(pwd)/.." # avoid ModuleNotFoundError

SCALE=0.6

echo "=== DTSUMO PIPELINE START... ==="

echo "[1/5] Task: parse..."
python3 -m dtSumo.scripts.src.inputs.parser

echo "[2/5] Task: detectors..."
python3 -m dtSumo.scripts.detectors

# # echo "[3/5] Task: route assignment..."
# # python3 -m dtSumo.scripts.src.operations.assignment --peaks-only

# echo "[4/5] Task: day-sim..."
# python3 -m scripts.src.operations.assignment --day-only --scale "$SCALE"

# echo "[5/5] Task: summary..."
# python3 -m scripts.sumoResults

echo "----------------------------------------"
echo "Completed successfully!"
echo "----------------------------------------"