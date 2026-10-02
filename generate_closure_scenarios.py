# generate_closure_scenarios.py — orchestratore Fase B
import yaml
import sumolib
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
import scripts.src.inputs.config as cfg
from scripts.src.operations.feedback_cycle import (
    run_macroscopic_assignment_day_iterative,
    build_closure_weight_file,
    compute_closure_penalty,
    DEFAULT_DAY_SCALE,
)
from scripts.detectors import generate_all_detectors, edges_as_lanes
from scripts.src.inputs.dayODs import generate_hour_matrices

# --- Posizioni di chiusura, dedotte da inspect_edge_connections.py -----------
# Regola di direzione (confermata in chat): penalizzare TUTTE le connessioni
# esistenti, entranti E uscenti, qualunque sia il loro numero.
CLOSURE_POSITIONS = {
    "pos9": {  # esistente, priority 9
        "edge_id": "125665632#1",
        "connections": [
            ("125665632#0", "125665632#1"),
            ("125665632#1", "135863195#1"),
        ],
    },
    "pos4": {  # esistente, priority 7
        "edge_id": "38319544#20",
        "connections": [
            ("-154986395#12", "38319544#20"),
            ("154986395#9", "38319544#20"),
            ("38319544#18", "38319544#20"),
            ("38319544#20", "-39671247"),
            ("38319544#20", "38319544#22"),
        ],
    },
    "pos_prio9b": {  # nuovo, priority 9
        "edge_id": "1307179596",
        "connections": [
            ("-1307179596", "1307179596"),
            ("1307179597#0", "1307179596"),
            ("1307179596", "-1307179596"),
            ("1307179596", "37756577#0"),
        ],
    },
    "pos_prio3": {  # nuovo, priority 3 - unico tier davvero nuovo
        "edge_id": "1305446509#5",
        "connections": [
            ("1305446509#4", "1305446509#5"),
            ("1305446509#5", "1305446509#7"),
        ],
    },
    "pos_prio7b": {  # nuovo, priority 7
        "edge_id": "121952564#4",
        "connections": [
            ("-110531712#0", "121952564#4"),
            ("121952564#2", "121952564#4"),
            ("121952564#4", "121952564#5"),
        ],
    },
}
# SEVERITY_LEVELS = [0.7, 0.4, 0.0]
SEVERITY_LEVELS = [0.5, 0.2]
TLS_VARIANTS_TO_RUN = ["MA_with_TLS"]
N_ROUNDS = 6
FREQ = 60 * 5  # 5 minuti, coerente con la granularità di previsione
MAX_WORKERS = 2  # <-- da tarare sulla tua macchina, vedi nota sotto

WEIGHT_FILES_DIR = cfg.OUTPUT_DIR_ADD / "closure_weights"
WEIGHT_FILES_DIR.mkdir(parents=True, exist_ok=True)

# --- Disegno dei 7 config di chiusura (2 posizioni x 3 severity + baseline) --
closure_configs = [{"name": "baseline", "position": None, "severity": 1.0}]
for pos_name in CLOSURE_POSITIONS:
    for sev in SEVERITY_LEVELS:
        closure_configs.append(
            {"name": f"{pos_name}_sev{sev}", "position": pos_name, "severity": sev}
        )

RUNS = [
    {"scenario": scenario, **cc}
    for scenario in TLS_VARIANTS_TO_RUN
    for cc in closure_configs
]


def run_one(run: dict) -> dict:
    """Esegue un singolo (scenario, closure_config) e ritorna la voce scenarios.yaml."""
    scenario, cc = run["scenario"], run
    run_label = f"{scenario}_{cc['name']}_seed{cfg.SEED}"
    print(f"\n########## {run_label} ##########")

    net_wide = sumolib.net.readNet(str(cfg.NET_FILE))
    lanes_data = edges_as_lanes(cfg.EDG_PARSE_XML)

    closure_add_path = cfg.OUTPUT_DIR_ADD / f"Det_{scenario}/detectors_{run_label}.add.xml"
    closure_det_out = cfg.OUTPUT_DIR_ADD / f"Det_{scenario}/DetOut_Day_seed{cfg.SEED}_{cc['name']}"
    closure_add_path.parent.mkdir(parents=True, exist_ok=True)
    closure_det_out.mkdir(parents=True, exist_ok=True)
    generate_all_detectors(lanes_data, FREQ, closure_add_path, closure_det_out)

    if cc["position"] is not None:
        edge_id = CLOSURE_POSITIONS[cc["position"]]["edge_id"]
        weight_path = WEIGHT_FILES_DIR / f"initial_{run_label}.xml"
        initial_weight_file = build_closure_weight_file(
            net=net_wide, edge_id=edge_id, severity=cc["severity"], out_path=weight_path,
        )
        closure_penalty_traveltime = compute_closure_penalty(net_wide, edge_id, cc["severity"])
    else:
        edge_id = None
        initial_weight_file = None
        closure_penalty_traveltime = None

    run_macroscopic_assignment_day_iterative(
        scenario=scenario,
        taz_file=cfg.TAZ,
        n_rounds=N_ROUNDS,
        closure_id=cc["name"],
        initial_weight_file=initial_weight_file,
        detectors_file=closure_add_path,
        closure_edge_id=edge_id,
        closure_penalty_traveltime=closure_penalty_traveltime,
        regenerate_matrices=False, 
    )
    
    disturbances = []
    if cc["position"] is not None:
        for (frm, to) in CLOSURE_POSITIONS[cc["position"]]["connections"]:
            disturbances.append({"from": frm, "to": to, "severity": cc["severity"]})

    return {"id": run_label, "det_dir": str(closure_det_out.resolve()), "disturbances": disturbances}


if __name__ == "__main__":
    data_dir = cfg.OD_MATRICES["DAY"] / f"scaled_{DEFAULT_DAY_SCALE}"
    if len(list(data_dir.glob("h*.mtx"))) < 24:
        generate_hour_matrices(
            od_morning=cfg.OD_MATRICES["AM"],
            od_evening=cfg.OD_MATRICES["PM"],
            output_dir_data=data_dir,
            input_data=cfg.SENS_DATA_FOLDER / "flows.csv",
            demand_scale=DEFAULT_DAY_SCALE,
        )
    else:
        print(f"Matrici orarie già presenti in {data_dir}, skip.")
    scenarios_out = []
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(run_one, run): run for run in RUNS}
        for future in as_completed(futures):
            scenarios_out.append(future.result())
            
    existing = {}
    scenarios_path = Path("data/scenarios.yaml")
    if scenarios_path.exists():
        with open(scenarios_path) as f:
            prev = yaml.safe_load(f) or {"scenarios": []}
        existing = {sc["id"]: sc for sc in prev.get("scenarios", [])}

    for sc in scenarios_out:
        existing[sc["id"]] = sc  # aggiunge nuovi, sovrascrive solo se stesso id (stesso seed+config)

    with open(scenarios_path, "w") as f:
        yaml.dump({"scenarios": list(existing.values())}, f, sort_keys=False)

    # with open("data/scenarios.yaml", "w") as f:
    #     yaml.dump({"scenarios": scenarios_out}, f, sort_keys=False)

    print(f"\nScritti {len(scenarios_out)} scenari in inputs/scenarios.yaml")