"""
4.2 Macroscopic Traffic Assignment (Rapelli et al. - TuST)
-> run_sue_feedback_cycle(): iterative SUE assignment with real-travel-time
feedback (marouter -> sumo -> edgeData -> marouter -> ...), n_rounds times
-> filter_short_flows() removes trips that are not long enough

"""

import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import scripts.src.inputs.config as cfg
from scripts.src.operations.cmd import run_marouter
from scripts.src.operations.filtering import filter_short_flows, filter_zero_prob
from scripts.src.operations.taz_zones import AssignmentContext, build_edge_to_taz
from scripts.src.modules.entities import CfgAttributes

from colorama import init, Fore

init(autoreset=True)

DEFAULT_DAY_SCALE = 1.0

TLS_PENALTY_BY_VARIANT = {
    "no_TLS": 0.0,
    "with_TLS": 5.0,
}

# Default SUE params for the feedback cycle. Kept here as a single source of
# truth instead of scattered literals in the function call.
SUE_PARAMS = dict(
    method="SUE",
    route_choice="gawron",
    gawron_beta=0.3,
    gawron_a=0.15,
    paths=5,
    path_penalty=25.0,
    weights_priority=0.0,
    max_iterations=25,
    max_inner_iterations=100,
    weight_adaption=0.4,
)

INCREMENTAL_PARAMS = dict(
    method="incremental",
    route_choice="gawron",
    paths=10,
    path_penalty=15.0,
    weights_priority=0.0,
    max_iterations=100,
    weight_adaption=0.8,
)

# freq (secondi) del dump edgeData usato per il feedback SUE.
EDGEDATA_FREQ = 1800


# STEP 4.2 — Traffic Assignment with marouter (AM / PM), iterative


def _tls_variant_of(scenario: str) -> str:
    # "MA_no_TLS" -> "no_TLS" ; "MA_with_TLS" -> "with_TLS"
    return scenario.replace("MA_", "", 1)


def _edge_weights_path(work_dir: Path, round_idx: int) -> Path:
    """
    Single source of truth for the per-round weight file name. Used both by
    the writer (SUMO, via edgeData) and the reader (marouter, via
    --weight-files) so the two can never diverge into two different names
    again (that was the whole bug: edge_output2.xml vs edgeData.xml).
    """
    return work_dir / f"edge_weights_r{round_idx}.xml"


def _write_edgedata_additional(path: Path, output_file: Path, freq: int = EDGEDATA_FREQ) -> Path:
    """
    Writes an additional file for SUMO to produce an edgeData dump in output_file path.
    To be added to SUMO configuration using -a flag.
    """
    content = (
        "<additional>\n"
        f'    <edgeData id="dump" freq="{freq}" file="{output_file}"/>\n'
        "</additional>\n"
    )
    path.write_text(content)
    return path


def load_traveltimes(path: Path, aggregation: str = "weighted_mean") -> Dict[str, float]:
    """
    Aggregates multi-interval edgeData dumps by edge.
    """
    tree = ET.parse(path)
    samples: Dict[str, List[Tuple[float, float]]] = {}

    for interval in tree.getroot().findall("interval"):
        for edge in interval.findall("edge"):
            sampled = float(edge.get("sampledSeconds", 0))
            if sampled <= 0:
                continue
            tt = edge.get("traveltime")
            if tt is None:
                continue
            eid = edge.get("id")
            samples.setdefault(eid, []).append((float(tt), sampled))

    result: Dict[str, float] = {}
    for eid, vals in samples.items():
        if aggregation == "max":
            result[eid] = max(v[0] for v in vals)
        else:  # weighted_mean
            total_w = sum(v[1] for v in vals)
            result[eid] = sum(v[0] * v[1] for v in vals) / total_w
    return result


def run_feedback_cycle(
    scenario: str,
    period: str,
    ctx: AssignmentContext,
    n_rounds: int = 3,
    scouting_duration: Optional[int] = None,
) -> Path:
    """
    Runs n_rounds of: marouter (using previous round's measured
    travel times as weights) -> sumo -> edgeData dump -> next round.

    Round 0 has no external weights (pure marouter cost function).
    Returns the path to the cleaned (short-flows + zero-prob filtered)
    route file of the LAST round, WITHOUT the O'/D' extension — that is
    applied once, separately, by the caller after the cycle converges.

    NB: scouting_duration restricts simulation end to begin + scouting_duration.
    """
    taz_file = ctx.taz_file
    edge_taz_map = ctx.edge_taz_map

    METHOD = INCREMENTAL_PARAMS['method']
    work_dir = cfg.WORKDIRS[scenario][period] / METHOD
    work_dir.mkdir(parents=True, exist_ok=True)

    tls_variant = _tls_variant_of(scenario)
    tls_penalty = TLS_PENALTY_BY_VARIANT[tls_variant]

    period_begin = cfg.PERIODS[period]["start"]
    period_end = cfg.PERIODS[period]["end"]

    if scouting_duration:
        sumo_end = period_begin + scouting_duration
    else:
        sumo_end = period_end

    prev_weight_file: Optional[Path] = None # no weigths for the first round
    routes_final: Optional[Path] = None

    for round_idx in range(n_rounds):
        # alpha_n = 1 / (round_idx +1)
        print(f"\n[{scenario}/{period}] ROUND {round_idx} (end={sumo_end}) =====")

        trips_output = work_dir / f"od_trips_r{round_idx}.odtrips.xml"
        netload_output = work_dir / f"netload_r{round_idx}.xml"

        routes_macro = run_marouter(
            net_file=cfg.NET_FILE,
            od_matrices=cfg.OD_MATRICES[period],
            taz_file=taz_file,
            out_dir=work_dir,
            trips_output=trips_output,
            additional_files=[cfg.DETECTORS[scenario][period], cfg.VTYPE],
            netload_output=netload_output,
            weights_tls=tls_penalty,
            begin=period_begin,
            end=period_end,
            weight_files=str(prev_weight_file) if prev_weight_file else None,
            extra_args=["-l", str(work_dir / f"marouter_r{round_idx}.log"), 
                        "--weight-adaption", str(INCREMENTAL_PARAMS['weight_adaption']),
                        "--seed", "42"],
            **INCREMENTAL_PARAMS,
        )
        print(f"\tMacroscopic assignment (round {round_idx}) saved here: {routes_macro}")

        routes_clean = filter_short_flows(
            routes_macro,
            output_new=work_dir / f"marouter_output_clean_r{round_idx}.rou.xml",
            edge_taz_map=edge_taz_map,
            min_edges=2,
        )
        routes_final = filter_zero_prob(routes_clean)

        # Il file di pesi che QUESTO round scrivera' per il PROSSIMO round.
        next_weight_file = _edge_weights_path(work_dir, round_idx + 1)
        edgedata_additional = work_dir / f"edgedata_config_r{round_idx}.add.xml"
        _write_edgedata_additional(edgedata_additional, next_weight_file)

        cfg_name = f"francia_peschiera_SUE_{scenario[3:]}_r{round_idx}_{period}.sumocfg"
        CfgAttributes(
            net=cfg.NET_FILE,
            routes=routes_final,
            output_cfg=cfg.CFG_DIR,
            output_sumo=cfg.SIM_OUT[scenario][period],
            config_name=cfg_name,
            meso=False,
            setting=cfg.VIEW,
        ).build(
            method="marouter",
            taz=taz_file,
            begin=period_begin,
            end=sumo_end,
            detectors=cfg.DETECTORS[scenario][period],
            edgedata=edgedata_additional,
            vtype=cfg.VTYPE,
        )

        sumocfg_path = cfg.CFG_DIR / cfg_name

        cmd = ["sumo", "-c", str(sumocfg_path), "--end", str(sumo_end)]
        
        print(f"\tSUMO simulating round n°{round_idx}...")
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            print(Fore.RED + f"[ERROR SUMO round {round_idx}]")
            print("STDOUT:", e.stdout)
            print("STDERR:", e.stderr)
            raise
        
        if not next_weight_file.exists():
            raise RuntimeError(
                Fore.RED + f"ERROR: could not generate edgeData for round {round_idx}: {next_weight_file}"
            )
            
            
        print(f"\tRound {round_idx} completed. Weigths for next round{next_weight_file}")
        prev_weight_file = next_weight_file

    print(Fore.GREEN + f"\n[{scenario}/{period}] Cycle successfully completed: {n_rounds} rounds.")
    return routes_final


def build_final_sumocfg(
    scenario: str, period: str, taz_file: Path, routes_final: Path
) -> None:
    """
    Config for last run, over extended routes (post step 4.3), no scouting time.
    """
    work_dir = cfg.WORKDIRS[scenario][period]
    detectors_file = cfg.DETECTORS[scenario][period]
    final_edge_output = work_dir / "edge_output_final.xml"

    edgedata_additional = work_dir / "edgedata_config_final.add.xml"
    _write_edgedata_additional(edgedata_additional, final_edge_output)

    CfgAttributes(
        net=cfg.NET_FILE,
        routes=routes_final,
        output_cfg=cfg.CFG_DIR,
        output_sumo=cfg.SIM_OUT[scenario][period],
        config_name=f"francia_peschiera_MAROUTER_{scenario[3:]}_{period}.sumocfg",
        meso=True,
        setting=cfg.VIEW,
        teleport="300"
    ).build(
        method="marouter",
        taz=taz_file,
        begin=cfg.PERIODS[period]["start"],
        end=cfg.PERIODS[period]["end"],
        detectors=detectors_file,
        edgedata=edgedata_additional,
        vtype=cfg.VTYPE,
    )
    print(f"\t.sumocfg [{scenario}/{period}]  perfomed with route file: {routes_final}")


# STEP 4.2-DAY — Traffic Assignment for the entire day (24 matrici orarie)


def run_macroscopic_assignment_day(
    scenario: str, taz_file: Path, scale: float = DEFAULT_DAY_SCALE
) -> Path:
    """
    Executes marouter for the entire day, only once (begin=0, end=86400),
    using the folder of hour matrices as input (h00.mtx..h23.mtx).
    Kept as incremental (non-iterative), for computational scalability over entire day.
    """
    from scripts.src.inputs.dayODs import generate_hour_matrices

    generate_hour_matrices(
        od_morning=cfg.OD_MATRICES["AM"],
        od_evening=cfg.OD_MATRICES["PM"],
        output_dir_data=cfg.OD_MATRICES["DAY"] / f"scaled_{scale}",
        input_data=cfg.SENS_DATA_FOLDER / "flows.csv",
        demand_scale=scale,
    )

    work_dir = cfg.WORKDIRS[scenario]["DAY"]
    work_dir.mkdir(parents=True, exist_ok=True)

    data_dir = cfg.OD_MATRICES["DAY"] / f"scaled_{scale}"
    day_matrices = sorted(data_dir.glob("h*.mtx"))
    if not day_matrices:
        raise RuntimeError(f"No file h*.mtx found in {data_dir}")

    tls_variant = _tls_variant_of(scenario)
    tls_penalty = TLS_PENALTY_BY_VARIANT[tls_variant]
    detectors_file = cfg.DETECTORS[scenario]["DAY"]
    trips_output = work_dir / "od_trips_file.odtrips.xml"

    print(
        Fore.CYAN + f"[4.2-DAY] MAROUTER — scenario={scenario} ({len(day_matrices)} matrices, scale={scale})..."
    )

    routes_macro = run_marouter(
        net_file=cfg.NET_FILE,
        od_matrices=day_matrices,
        taz_file=taz_file,
        out_dir=work_dir,
        trips_output=trips_output,
        additional_files=[detectors_file, cfg.VTYPE],
        netload_output=work_dir / "netload_ouput.xml",
        method="incremental",
        route_choice="logit",
        logit_theta=0.3,
        logit_beta=0.2,
        paths=5,
        path_penalty=15.0,
        weights_priority=0.0,
        max_alternatives=10,
        max_iterations=100,
        tolerance=0.01,
        weights_tls=tls_penalty,
        begin=0,
        end=24 * 3600,
        extra_args=["--weight-adaption", str(INCREMENTAL_PARAMS['weight_adaption']),
                    "--seed", "42"]
    )
    print(f"\t\tDay macroscopic assignment run in {routes_macro}")

    edge_taz_map = build_edge_to_taz(taz_file)
    routes_clean = filter_short_flows(
        routes_macro,
        output_new=work_dir / "marouter_output_clean.rou.xml",
        edge_taz_map=edge_taz_map,
        min_edges=2,
    )
    routes_filtered = filter_zero_prob(routes_clean)

    return routes_filtered


def build_sumocfg_day(
    scenario: str, taz_file: Path, routes_final: Path, scale: float = DEFAULT_DAY_SCALE
) -> None:
    work_dir = cfg.WORKDIRS[scenario]["DAY"]
    detectors_file = cfg.DETECTORS[scenario]["DAY"]
    final_edge_output = work_dir / "edge_output_final.xml"

    edgedata_additional = work_dir / "edgedata_config_final.add.xml"
    _write_edgedata_additional(edgedata_additional, final_edge_output)

    CfgAttributes(
        net=cfg.NET_FILE,
        routes=routes_final,
        output_cfg=cfg.CFG_DIR,
        output_sumo=cfg.SIM_OUT[scenario]["DAY"],
        config_name=f"francia_peschiera_MAROUTER_{scenario[3:]}_DAY_scaled{scale}.sumocfg",
        teleport="300",
        meso=True,
        setting=cfg.VIEW,
    ).build(
        method="marouter",
        taz=taz_file,
        begin=0,
        end=86400,
        detectors=detectors_file,
        edgedata=edgedata_additional,
        vtype=cfg.VTYPE,
    )
    print(f"\t\t.sumocfg [{scenario}/DAY] output in: {routes_final}")
    
    
# STEP 4.2-DAY (iterativa) — stesso ciclo di feedback di AM/PM, ma sulle 24

def run_macroscopic_assignment_day_iterative(
    scenario: str,
    taz_file: Path,
    n_rounds: int = 3,
    scale: float = DEFAULT_DAY_SCALE,
) -> Path:
    """
    La domanda (le 24 matrici orarie) viene generata una volta sola, non
    ad ogni round — solo l'instradamento cambia round su round, non la
    domanda in ingresso.
    """
    from scripts.src.inputs.dayODs import generate_hour_matrices

    generate_hour_matrices(
        od_morning=cfg.OD_MATRICES["AM"],
        od_evening=cfg.OD_MATRICES["PM"],
        output_dir_data=cfg.OD_MATRICES["DAY"] / f"scaled_{scale}",
        input_data=cfg.SENS_DATA_FOLDER / "flows.csv",
        demand_scale=scale,
    )
    data_dir = cfg.OD_MATRICES["DAY"] / f"scaled_{scale}"
    day_matrices = sorted(data_dir.glob("h*.mtx"))
    if not day_matrices:
        raise RuntimeError(f"No file h*.mtx found in {data_dir}")

    work_dir = cfg.WORKDIRS[scenario]["DAY"] / "iterative"
    work_dir.mkdir(parents=True, exist_ok=True)

    edge_taz_map = build_edge_to_taz(taz_file)
    tls_variant = _tls_variant_of(scenario)
    tls_penalty = TLS_PENALTY_BY_VARIANT[tls_variant]
    detectors_file = cfg.DETECTORS[scenario]["DAY"]

    prev_weight_file: Optional[Path] = None
    routes_final: Optional[Path] = None

    for round_idx in range(n_rounds):
        print(f"\n===== [{scenario}/DAY] ROUND {round_idx} (24h, scale={scale}) =====")

        trips_output = work_dir / f"od_trips_r{round_idx}.odtrips.xml"
        netload_output = work_dir / f"netload_r{round_idx}.xml"

        routes_macro = run_marouter(
            net_file=cfg.NET_FILE,
            od_matrices=day_matrices,
            taz_file=taz_file,
            out_dir=work_dir,
            trips_output=trips_output,
            additional_files=[detectors_file, cfg.VTYPE],
            netload_output=netload_output,
            method="incremental",
            route_choice="logit",
            logit_theta=0.3,
            logit_beta=0.2,
            paths=10,
            path_penalty=15.0,
            weights_priority=0.0,
            max_alternatives=10,
            max_iterations=100,
            tolerance=0.01,
            weights_tls=tls_penalty,
            begin=0,
            end=24 * 3600,
            weight_files=str(prev_weight_file) if prev_weight_file else None,
            extra_args=["-l", str(work_dir / f"marouter_r{round_idx}.log"),
                        "--weight-adaption", str(INCREMENTAL_PARAMS['weight_adaption']),
                        "--seed", "42"],
        )
        print(f"\tDay macroscopic assignment (round {round_idx}) saved here: {routes_macro}")

        routes_clean = filter_short_flows(
            routes_macro,
            output_new=work_dir / f"marouter_output_clean_r{round_idx}.rou.xml",
            edge_taz_map=edge_taz_map,
            min_edges=2,
        )
        routes_final = filter_zero_prob(routes_clean)

        # Current weigth file, used in the next step
        next_weight_file = _edge_weights_path(work_dir, round_idx + 1)
        edgedata_additional = work_dir / f"edgedata_config_r{round_idx}.add.xml"
        _write_edgedata_additional(edgedata_additional, next_weight_file)

        cfg_name = f"francia_peschiera_DAYITER_{scenario[3:]}_r{round_idx}.sumocfg"
        CfgAttributes(
            net=cfg.NET_FILE,
            routes=routes_final,
            output_cfg=cfg.CFG_DIR,
            output_sumo=cfg.SIM_OUT[scenario]["DAY"],
            config_name=cfg_name,
            teleport="300",
            meso=True,
            setting=cfg.VIEW,
        ).build(
            method="marouter",
            taz=taz_file,
            begin=0,
            end=24 * 3600,
            detectors=detectors_file,
            edgedata=edgedata_additional,
            vtype=cfg.VTYPE,
        )

        sumocfg_path = cfg.CFG_DIR / cfg_name
        cmd = ["sumo", "-c", str(sumocfg_path)]
        print(f"\tLancio SUMO (meso) round {round_idx}...")
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            print(f"[ERROR for SUMO round {round_idx}]")
            print("STDOUT:", e.stdout)
            print("STDERR:", e.stderr)
            raise

        if not next_weight_file.exists():
            raise RuntimeError(
                f"edgeData non generato per il round {round_idx}: {next_weight_file}"
            )

        print(f"\tRound {round_idx} completo. Pesi per il prossimo round: {next_weight_file}")
        prev_weight_file = next_weight_file

    print(f"\n[{scenario}/DAY] Ciclo iterativo completato: {n_rounds} round eseguiti.")
    return routes_final