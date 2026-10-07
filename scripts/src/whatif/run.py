"""
Runs a what-if scenario through the day pipeline and adds it to data/scenarios.yaml.

The pipeline is the following:
1. scenario net + TAZ (network.py)
2. iterative assignment
3. O'/D' extension
4. final meso simulation

The baseline ("interventions": []) uses the reference net and TAZ.

Command to run on the terminal: uv run python -m scripts.src.whatif.run scenario.json [--check] [--json]
Another seed: DTSUMO_SEED=42 uv run python -m scripts.src.whatif.run scenario.json
"""

import contextlib, fcntl, json, yaml, random, sys
import click, sumolib, subprocess
from pathlib import Path
from colorama import Fore, init

import scripts.src.inputs.config as cfg
from scripts.detectors import edges_as_lanes, generate_all_detectors
from scripts.src.operations.marouter_cycle import (
    DEFAULT_DAY_SCALE,
    build_sumocfg_day,
    run_macro_assignment_day_iterative,
)
from scripts.src.operations.od_extension import extend_subset_trips
from scripts.src.operations.taz_zones import (
    AssignmentContext,
    _edge_taz_map,
    residential_edge_taz,
)
from scripts.src.whatif.network import build_scenario_net, scenario_taz
from scripts.src.whatif.scenario import (
    ScenarioError,
    check_scenario,
    disturbances,
    load_scenario,
    resolve_scenario,
)

init(autoreset=True)

VARIANTS = ["MA_no_TLS", "MA_with_TLS"]
N_ROUNDS = 6
FREQ = 60 * 5  # detectors
EXTENSION_SEED = (
    42  # same as assignment.main, so the 4.3 sampling matches the reference run
)
SCENARIOS_YAML = Path("data/scenarios.yaml")


def _print_report_(resolved_scen: dict, disturbance: list):
    for n, report in enumerate(resolved_scen["interventions"], 1):
        report = {k: v for k, v in report.items() if k != "edge_ids"}  # too many ids to print
        print(f"\tintervention {n}: {report}")
    print(f"\t{len(resolved_scen['lanes_closed'])} edges changed, {len(disturbance)} disturbed connections")
    
    
def _save_json_info_(info: dict, work_dir: Path):
    (work_dir / "scenario_resolved.json").write_text(json.dumps(info, ensure_ascii=False, indent=2))

    
def run_scenario(
    scenario: dict,
    variant: str = "MA_with_TLS",
    n_rounds: int = N_ROUNDS,
    scale: float = DEFAULT_DAY_SCALE,
    check_only: bool = False,
) -> dict:
    check_scenario(scenario)
    net = sumolib.net.readNet(str(cfg.NET_FILE))
    resolved_scen = resolve_scenario(net, scenario, cfg.TAZ)
    dist = disturbances(net, resolved_scen["edges"])
    _print_report_(resolved_scen, dist)

    scenario_taz(
        cfg.TAZ, set(resolved_scen["closed_edges"])
    )  # every zone keeps an access edge
    if check_only:
        return {"id": scenario["id"], **resolved_scen, "disturbances": dist}

    sid = scenario["id"]
    run_label = f"{variant}_{sid}_seed{cfg.SEED}"
    work_dir = (
        cfg.WORKDIRS[variant]["DAY"] / "iterative" / f"closure_{sid}_seed{cfg.SEED}"
    )
    output_sumo = cfg.SIM_OUT[variant]["DAY"] / f"closure_{sid}"
    work_dir.mkdir(parents=True, exist_ok=True)

    det_add = cfg.OUTPUT_DIR_ADD / f"Det_{variant}/detectors_{run_label}.add.xml"
    det_out = cfg.OUTPUT_DIR_ADD / f"Det_{variant}/DetOut_Day_seed{cfg.SEED}_{sid}"
    det_add.parent.mkdir(parents=True, exist_ok=True)
    det_out.mkdir(parents=True, exist_ok=True)
    generate_all_detectors(edges_as_lanes(cfg.EDG_PARSE_XML), FREQ, det_add, det_out)

    net_file, taz_file = cfg.NET_FILE, cfg.TAZ  # baseline
    if resolved_scen["lanes_closed"]:
        net_file = build_scenario_net(
            cfg.NET_FILE, net, resolved_scen["lanes_closed"], work_dir
        )
        taz_file = scenario_taz(
            cfg.TAZ, set(resolved_scen["closed_edges"]), work_dir / "scenario.taz.xml"
        )
    info = {
        "scenario": scenario,
        "variant": variant,
        "seed": cfg.SEED,
        "n_rounds": n_rounds,
        "scale": scale,
        **resolved_scen,
        "disturbances": dist,
    }
    _save_json_info_(info, work_dir)

    # Assignment and route extention
    routes = run_macro_assignment_day_iterative(
        scenario=variant,
        taz_file=taz_file,
        n_rounds=n_rounds,
        scale=scale,
        close_id=sid,
        det_file=det_add,
        regenerate_mtx=False,
        net_file=net_file,
        demand_rules=resolved_scen["demand"] or None,
    )

    ctx = AssignmentContext(
        net=net,
        taz_file=cfg.TAZ,
        edge_taz_map=_edge_taz_map(cfg.TAZ),
        residential_by_taz=residential_edge_taz(net, cfg.TAZ),
    )
    random.seed(EXTENSION_SEED)
    routes_ext = extend_subset_trips(
        scenario=variant,
        period="DAY",
        routes_macro=routes,
        ctx=ctx,
        work_dir=work_dir,
        net_file=net_file,
        closed_edges=set(resolved_scen["closed_edges"]),
    )

    # final meso simulation
    sumocfg = build_sumocfg_day(
        scenario=variant,
        taz_file=taz_file,
        routes_final=routes_ext,
        scale=scale,
        work_dir=work_dir,
        output_sumo=output_sumo,
        detectors_file=det_add,
        run_tag=f"_{sid}",
        net_file=net_file,
    )
    print(f"\tFinal meso simulation ({sid})...")
    try:
        subprocess.run(
            ["sumo", "-c", str(sumocfg)], check=True, capture_output=True, text=True
        )
    except subprocess.CalledProcessError as e:
        print(
            Fore.RED
            + f"ERROR final SUMO {sid}\nSTDOUT: {e.stdout}\nSTDERR: {e.stderr}"
        )
        raise

    info["outputs"] = {
        "net": str(net_file),
        "taz": str(taz_file),
        "routes": str(routes_ext),
        "edgedata": str(work_dir / "edge_output_final.xml"),
        "sim_out": str(output_sumo),
        "sumocfg": str(sumocfg),
        "det_dir": str(det_out),
    }
    (work_dir / "scenario_resolved.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2)
    )
    return {"id": run_label, "det_dir": str(det_out.resolve()), "disturbances": dist}


def record(entries: list, path: Path = SCENARIOS_YAML) -> None:
    # same id -> overwritten
    path.parent.mkdir(parents=True, exist_ok=True)
    # .lock so that two scenarios finishing together cannot not overwrite each other's entry
    with open(path.with_name(path.name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = {}
        if path.exists():
            prev = yaml.safe_load(path.read_text()) or {"scenarios": []}
            existing = {sc["id"]: sc for sc in prev.get("scenarios", [])}
        for entry in entries:
            existing[entry["id"]] = entry
        path.write_text(
            yaml.dump({"scenarios": list(existing.values())}, sort_keys=False)
        )


@click.command()
@click.argument("scenario_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--variant", type=click.Choice(VARIANTS), default="MA_with_TLS", show_default=True)
@click.option("--rounds", type=int, default=N_ROUNDS, show_default=True)
@click.option("--scale", type=float, default=DEFAULT_DAY_SCALE, show_default=True)
@click.option("--check", is_flag=True, help="Only resolve the scenario and print what it does.")
@click.option("--json","as_json",is_flag=True,help="With --check: print the result as JSON (for the chatbot).")

def cli(scenario_file, variant, rounds, scale, check, as_json):
    """Run one what-if scenario file."""
    if as_json:  # implies --check: one JSON line on stdout for the chatbot, the report goes to stderr
        try:
            with contextlib.redirect_stdout(sys.stderr):
                result = run_scenario(load_scenario(scenario_file), variant, rounds, scale, check_only=True)
            print(json.dumps({"ok": True, **result}, ensure_ascii=False))
        except ScenarioError as e:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return
    try:
        loaded_scen = load_scenario(scenario_file)
        entry = run_scenario(loaded_scen, variant, rounds, scale, check_only=check)
    except ScenarioError as e:
        raise click.ClickException(str(e))
    if not check:
        record([entry])
        print(Fore.GREEN + f"what-if {entry['id']} done, recorded in {SCENARIOS_YAML}")


if __name__ == "__main__":
    cli()