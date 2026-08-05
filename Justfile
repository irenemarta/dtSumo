# Variables
python := "uv run python"
VENV_DIR := ".venv"
REQUIREMENTS_FILE := "requirements.txt"

# Defualt task
default: help

backup: 
    bash ./scripts/backup_git.sh

# Create requirements file
requirements:
    uv pip compile pyproject.toml -o {{ REQUIREMENTS_FILE }}
    @echo "Requirements file created: {{ REQUIREMENTS_FILE }}"

# Requirements installation
init:
    uv sync

# Create single XML files for both static and dynamic (random simulation) elements
parse:
    {{ python }} -c "from scripts.src.inputs import parser; parser.main()"
    @echo "PARSE module executed"

# Create a CSV containing all the useful information for eahc edge
database:
    {{ python }} -c "from scripts import database; database.main()"
    @echo "DATABASE module executed"

# Define TAZ (Traffic Assigments Zones) basing on SV assignments and create routes from O/Ds peak-hour-data
taz-od:
    {{ python }} -c "from scripts import tazOD; tazOD.main()"
    #echo "tazOD module executed"

# Analysis of sensors data from PASTA/BRIDGE db
sensors:
    {{ python }} -c "from scripts import sensors; sensors.main()"
    @echo "SENSORS module executed"

# Create complete dashboard rapresenting SUMO simulation output (Summary.xml)
summary:
    {{ python }} -c "from scripts import sumoResults; sumoResults.main()"
    @echo "sumoResults module performed"

# Set configuration files correctly
configuration:
    {{ python }} -c "from scripts import createSumoCfg; createSumoCfg.main()"
    @echo "Configuration saved successfully"

# Interpolate OD and sensors data to obtain a simulation for daily scenario
day-sim: 
    {{ python }} -c "from scripts import allDayOD; allDayOD.main()"
    @echo "Whole day simulation performed"


# using duaiterate SUE
dua-day:
    {{ python }} -c "from scripts import allDay_duaiterate; allDay_duaiterate.main()"
    @echo "Whole day simulation with duaiterate performed"


# Add detectors to the net
detectors:
    {{ python }} -c "from scripts import detectors; detectors.main()"
    @echo "Detectors module performed"

# Adjust TLL (Traffic Light Logic) - NB: first, WRITE tls_phases.csv FILE MANUALLY
tls: parse
    {{ python }} -c "from scripts import tls; tls.main()"
    @echo "TLS module performed"

# Run all tasks in sequence
all: parse database taz-od detectors tls sensors day-sim configuration summary
    @echo "Completed successfully!"

# Clean up the cache
clean:
    rm -rf scripts/__pycache__
    find . -type d -name __pycache__ -exec rm -rf {} +
    find . -type f -name "*.pyc" -delete

# Suggestions
help:
    @just --list
