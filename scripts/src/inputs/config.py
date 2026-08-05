"""
CONFIG.PY
Path Management for SUMO Traffic Assignment Project
"""

from pathlib import Path

# Main roots
# HACK: change BASE_PATH with the correct project path
BASE_PATH = Path("/home/marta/dtSumo")
SCRIPTS_ROOT = BASE_PATH / "scripts"
DATA_PATH = BASE_PATH / "data"
OUTPUT_BASE = SCRIPTS_ROOT / "output"
OUTPUT_DASHBOARDS = OUTPUT_BASE / "dashboards"
IMAGES = OUTPUT_BASE / "img"
SENS_LANE_LOOKUP = DATA_PATH / "sens_lanes_match.csv"
VIEW = SCRIPTS_ROOT / "views/vehicles.view.xml"

# Inputs
NET_FILE = DATA_PATH / "raw/francia_peschiera_passenger.net.xml"
ZONES = DATA_PATH / "raw" / "TOC_modello_Visum_CsoFrancia/SHP Zone/ZoneSVR_CsoFrancia.shp"
CONNECTORS = DATA_PATH / "raw" / "TOC_modello_Visum_CsoFrancia/Domanda/IFER v03/NewConnectors_v03_connector.SHP"
OD_DOMANDA = DATA_PATH / "raw" / "TOC_modello_Visum_CsoFrancia/Domanda"
VTYPE = DATA_PATH / "raw" / "VType.add.xml"

# Time logics
PERIODS = {
    "AM":  {"start": 28800, "end": 36000, "label": "Morning", "mtx": "IFER v03/201 TOTAL_MTX_08-09.mtx", "mtx_is_dir": False},
    "PM":  {"start": 64800, "end": 72000, "label": "Evening", "mtx": "IFER v03/202 TOTAL_MTX_18-19.mtx", "mtx_is_dir": False},
    "DAY": {"start": 0, "end": 86400, "label": "Day", "mtx": "DAY", "mtx_is_dir": True}
}

# Simulation scenarios
# DUA, LOGIT, GAWRON: no TLS distinction (TLS already in net.xml)
# MA and base: keep TLS distinction
DUA_METHODS = ["DUA", "LOGIT", "GAWRON"]
TLS_VARIANTS = ["no_TLS", "with_TLS"]

SCENARIOS  = list(DUA_METHODS)  # "DUA", "LOGIT", "GAWRON"
SCENARIOS += [f"MA_{t}" for t in TLS_VARIANTS]  # "MA_no_TLS", "MA_with_TLS"
SCENARIOS += [f"base_{t}" for t in TLS_VARIANTS]  # "base_no_TLS", "base_with_TLS"

# Output subdirs
CFG_DIR  = OUTPUT_BASE / "config"
ROUTES_DIR  = OUTPUT_BASE / "routes"
SUMO_OUTPUT = OUTPUT_BASE / "simulation-out"
OUTPUT_DIR_ADD = OUTPUT_BASE / "additionals"
OUTPUT_DIR_PARSING = OUTPUT_BASE / "parsed"
DUA_WORKDIR = OUTPUT_BASE / "dua-workdir-3"
MAROUTER_WORKDIR = OUTPUT_BASE / "marouter-workdir"

# Generated files
TAZ = OUTPUT_DIR_ADD     / "francia_peschiera_TAZ.taz.xml"
EDG_PARSE_XML = OUTPUT_DIR_PARSING / "francia_peschiera.edg.xml"
NOD_PARSE_XML = OUTPUT_DIR_PARSING / "francia_peschiera.nod.xml"

## TLS
OUTPUT_DIR_TLS = OUTPUT_DIR_ADD / "tls"

## OD matrices
OD_MATRICES = {period: OD_DOMANDA / info["mtx"] for period, info in PERIODS.items()}

## Routes
OD_TRIPS_DIR = {period: ROUTES_DIR / f"OD_{info['label']}" for period, info in PERIODS.items()}
OD_ROUTES = {
    period: OD_TRIPS_DIR[period] / (
        "od_route_daily.odtrips.rou.xml" if period == "DAY" else "od_route_file.odtrips.rou.xml"
    )
    for period in PERIODS
}

## Dashboards
OUTPUT_DIR_COMPARISON = OUTPUT_DASHBOARDS / "comparison"
OUTPUT_DIR_DUAITERATE = OUTPUT_DASHBOARDS / "duaiterate"
OUTPUT_DIR_DUAROUTER  = OUTPUT_DASHBOARDS / "duarouter"
OUTPUT_DAY_PROFILE = OUTPUT_DASHBOARDS / "out-DataExtr-OD-allDay"

## TAZ relations -> TAZREL['AM']
TAZREL = {period: OUTPUT_DIR_ADD / f"taz-rel/{period}/francia_peschiera.TAZREL.add.xml" for period in PERIODS}

## Detectors -> DETECTORS['LOGIT']['AM']
DETECTORS = {
    scenario: {
        period: OUTPUT_DIR_ADD / f"Det_{scenario}/detectors_{scenario}_{period}.add.xml"
        for period in PERIODS
    } for scenario in SCENARIOS
}

## Detector outputs -> DET_OUT['LOGIT']['AM']
DET_OUT = {
    scenario: {
        period: OUTPUT_DIR_ADD / f"Det_{scenario}/DetOut_{PERIODS[period]['label']}"
        for period in PERIODS
    } for scenario in SCENARIOS
}

## Simulation outputs -> SIM_OUT['LOGIT']['AM']
SIM_OUT = {
    scenario: {
        period: SUMO_OUTPUT / f"out_{scenario}"
        for period in PERIODS
    } for scenario in SCENARIOS
}

## Workdirs -> WORKDIRS['LOGIT']['AM']
WORKDIRS = {}
for period in PERIODS:
    # DUA, LOGIT, GAWRON: no TLS distinction
    for method in DUA_METHODS:
        WORKDIRS.setdefault(method, {})[period] = DUA_WORKDIR / f"{method}_{period}"
    # MA: TLS distinction
    for tls in TLS_VARIANTS:
        scenario_name = f"MA_{tls}"
        WORKDIRS.setdefault(scenario_name, {})[period] = MAROUTER_WORKDIR / f"{scenario_name}_{period}"

## Route paths -> MAP_ROUTES_WORKDIR['LOGIT']['AM']
MAP_ROUTES_WORKDIR = {}

for method in DUA_METHODS:
    MAP_ROUTES_WORKDIR[method] = {
        period: DUA_WORKDIR / f"{method}_{period}" / "od_route_file.odtrips.rou.xml"
        for period in PERIODS
    }
    

for tls in TLS_VARIANTS:
    scenario_name = f"MA_{tls}"
    MAP_ROUTES_WORKDIR[scenario_name] = {
        period: MAROUTER_WORKDIR / f"{scenario_name}_{period}" / "marouter_output.rou.xml"
        for period in PERIODS
    }

for tls in TLS_VARIANTS:
    scenario_name = f"base_{tls}"
    MAP_ROUTES_WORKDIR[scenario_name] = {
        period: OUTPUT_BASE / f"routes/OD_{PERIODS[period]['label']}/od_route_file.odtrips.rou.xml"
        for period in PERIODS
    }

# Helpers
def create_project_structure():
    """Create all required output directories."""
    print("FOLDER STRUCTURE DEFINITION")
    base_folders = [
        OUTPUT_BASE, CFG_DIR, SUMO_OUTPUT, OUTPUT_DIR_ADD,
        OUTPUT_DIR_PARSING, DUA_WORKDIR, MAROUTER_WORKDIR, IMAGES
    ]
    try:
        for folder in base_folders:
            folder.mkdir(parents=True, exist_ok=True)

        for period in PERIODS:
            TAZREL[period].parent.mkdir(parents=True, exist_ok=True)
            OD_TRIPS_DIR[period].mkdir(parents=True, exist_ok=True)

        for scenario in SCENARIOS:
            for period in PERIODS:
                MAP_ROUTES_WORKDIR[scenario][period].parent.mkdir(parents=True, exist_ok=True)
                DET_OUT[scenario][period].mkdir(parents=True, exist_ok=True)
                WORKDIRS.get(scenario, {}).get(period, Path(".")).mkdir(parents=True, exist_ok=True)
                SIM_OUT[scenario][period].mkdir(parents=True, exist_ok=True)

        print("All folders successfully created\n")
    except OSError as e:
        print(f"ERROR while creating folder structure: {e}")


if __name__ == "__main__":
    create_project_structure()