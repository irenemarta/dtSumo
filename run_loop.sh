#!/bin/bash

N=3

PYTHON_CMD="python3 -m scripts.src.operations.assignment --day-only --scale 0.7"
SUMO_CMD_AM="sumo -c "/home/fullsuper/irene/dtSumo/scripts/output/config/francia_peschiera_MAROUTER_no_TLS_AM.sumocfg""
SUMO_CMD_PM="sumo -c "/home/fullsuper/irene/dtSumo/scripts/output/config/francia_peschiera_MAROUTER_no_TLS_PM.sumocfg""
SUMO_CMD_DAY="sumo -c "/home/fullsuper/irene/dtSumo/scripts/output/config/francia_peschiera_MAROUTER_no_TLS_DAY_scaled0.5.sumocfg""

echo "LOOP FOR $N ITERATIONS..."

for (( i=1; i<=N; i++ ))
do
    echo "--------Iteration $i of $N--------"

    # Traffic assignment
    echo "[1/2] Avvio dello script Python..."
    $PYTHON_CMD

    # Check for Python errors
    if [ $? -ne 0 ]; then
        echo "Error while executing Python script!"
        exit 1
    fi

    # SUMO command
    echo "[2/2] Avvio del comando SUMO..."
    # $SUMO_CMD_AM
    # $SUMO_CMD_PM
    $SUMO_CMD_DAY

    # Check for SUMO errors
    if [ $? -ne 0 ]; then
        echo "Error while executing SUMO!"
        exit 1
    fi

    echo "Iteration $i successfully completed."
done

echo "LOOP OF $N ITERATIONS COMPLETED"