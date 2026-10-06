"""
4.2 Macroscopic Traffic Assignment (Rapelli et al. - TuST)
- run_cycle(): iterative SUE assignment with real-traveltime
- feedback (marouter -> sumo -> edgeData -> marouter -> ...), n_rounds times
"""

import subprocess
from pathlib import Path
from typing import Optional

import scripts.src.inputs.config as cfg
from scripts.src.operations.cmd import run_marouter
from scripts.src.inputs.dayODs import generate_hour_matrices
from scripts.src.operations.filtering import filter_short_flows, filter_zero_prob
from scripts.src.operations.taz_zones import _edge_taz_map
from scripts.src.modules.entities import CfgAttributes, AssignmentContext
from colorama import init, Fore

init(autoreset=True)

DEFAULT_DAY_SCALE = 1.0

TLS_PENALTY_BY_VARIANT = {
    "no_TLS": 0.0,
    "with_TLS": 5.0,
}

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
    paths=5,
    path_penalty=15.0,
    weights_priority=0.0,
    max_iterations=100,
    weight_adaption=0.4,  # 0.8 no tls, 0.4 tls
)

EDGEDATA_FREQ = 1800 # frequency of data dump

def _time_window(
    period: str, scouting_duration: Optional[int]
) -> tuple[int, int, int]:
    period_begin = cfg.PERIODS[period]["start"]
    period_end = cfg.PERIODS[period]["end"]
    sumo_end = period_begin + scouting_duration if scouting_duration else period_end
    return period_begin, period_end, sumo_end

def _tls_variant_of(scenario: str) -> str:
    # "MA_no_TLS" -> "no_TLS" ; "MA_with_TLS" -> "with_TLS"
    return scenario.replace("MA_", "", 1)

def _edge_weights_path(work_dir: Path, round_idx: int) -> Path:
    """
    Consistent additional file naming for SUMO edgeData, used both by the writer (SUMO, via edgeData) 
    and the reader (marouter, via --weight-files).
    """
    return work_dir / f"edge_weights_r{round_idx}.xml"

def _write_edgedata_additional(
    path: Path, output_file: Path, freq: int = EDGEDATA_FREQ
) -> Path:
    """
    To be added to SUMO configuration using -a flag.
    """
    content = (
        "<additional>\n"
        f'    <edgeData id="dump" freq="{freq}" file="{output_file}"/>\n'
        "</additional>\n"
    )
    path.write_text(content)
    return path

# STEP 4.2 — Traffic Assignment with marouter for peaks (AM / PM)
def run_cycle(
    scenario: str,
    period: str,
    ctx: AssignmentContext,
    n_rounds: int = 3,
    scouting_step: Optional[int] = None,
) -> Path:
    """
    Runs n_rounds of marouter with computed traveltimes -> sumo -> edgeData dump -> next round. 
    NB: Round 0: no external weights (pure marouter cost function); scouting_step restricts simulation end to begin + scouting_step for faster debug.
    """
    taz_file = ctx.taz_file
    edge_taz_map = ctx.edge_taz_map

    METHOD = INCREMENTAL_PARAMS["method"]
    work_dir = cfg.WORKDIRS[scenario][period] / METHOD
    work_dir.mkdir(parents=True, exist_ok=True)

    tls_variant = _tls_variant_of(scenario)
    tls_penalty = TLS_PENALTY_BY_VARIANT[tls_variant]
    period_begin, period_end, sumo_end = _time_window(period, scouting_step)

    prev_weight_file: Optional[Path] = None  # no weigths for the first round
    routes_final: Optional[Path] = None

    for round_idx in range(n_rounds):
        print(f"\n{scenario}/{period} round {round_idx} (end={sumo_end})")

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
            extra_args=[
                "-l",
                str(work_dir / f"marouter_r{round_idx}.log"),
                "--weight-adaption",
                str(INCREMENTAL_PARAMS["weight_adaption"]),
                "--seed",
                cfg.SEED,
            ],
            **INCREMENTAL_PARAMS,
        )
        print(
            f"\tMacroscopic assignment (round {round_idx}) saved here: {routes_macro}"
        )

        routes_clean = filter_short_flows(
            routes_macro,
            output_new=work_dir / f"marouter_output_clean_r{round_idx}.rou.xml",
            edge_taz_map=edge_taz_map,
            min_edges=2,
        )
        routes_final = filter_zero_prob(routes_clean)
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
                Fore.RED
                + f"ERROR: could not generate edgeData for round {round_idx}: {next_weight_file}"
            )

        print(
            f"\tRound {round_idx} completed. Weigths for next round{next_weight_file}"
        )
        prev_weight_file = next_weight_file

    print(
        Fore.GREEN
        + f"\nCycle successfully completed for {scenario}/{period}: {n_rounds} rounds."
    )
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
        teleport="300",
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


# STEP 4.2-DAY — config of the final day simulation
def build_sumocfg_day(
    scenario: str, taz_file: Path, routes_final: Path, scale: float = DEFAULT_DAY_SCALE,
    work_dir: Optional[Path] = None, # what-if: scenario folders (defaults = calibrated run)
    output_sumo: Optional[Path] = None,
    detectors_file: Optional[Path] = None,
    run_tag: str = "",
    net_file: Optional[Path] = None,
) -> Path:
    work_dir = work_dir or cfg.WORKDIRS[scenario]["DAY"]
    detectors_file = detectors_file or cfg.DETECTORS[scenario]["DAY"]
    final_edge_output = work_dir / "edge_output_final.xml"

    edgedata_additional = work_dir / "edgedata_config_final.add.xml"
    _write_edgedata_additional(edgedata_additional, final_edge_output)

    config_name = f"francia_peschiera_MAROUTER_{scenario[3:]}{run_tag}_DAY_scaled{scale}.sumocfg"
    CfgAttributes(
        net=net_file or cfg.NET_FILE,
        routes=routes_final,
        output_cfg=cfg.CFG_DIR,
        output_sumo=output_sumo or cfg.SIM_OUT[scenario]["DAY"],
        config_name=config_name,
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
    print(f"\t\t.sumocfg {scenario}/DAY output in: {routes_final}")
    return cfg.CFG_DIR / config_name


# STEP 4.2-DAY (iterative) over 24 hours
def run_macroscopic_assignment_day_iterative(
    scenario: str,
    taz_file: Path,
    n_rounds: int = 3,
    scale: float = DEFAULT_DAY_SCALE,
    close_id: Optional[str] = None, # for what-if scenarios
    initial_wfile: Optional[Path] = None,
    det_file: Optional[Path] = None, # parallelisation (override of cfg.DETECTORS[scenario]["DAY"])
    regenerate_mtx: bool = True,
    net_file: Optional[Path] = None, # what-if: scenario network (default cfg.NET_FILE)
) -> Path:
    net_file = net_file or cfg.NET_FILE

    if regenerate_mtx:
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
        raise RuntimeError(Fore.RED + f"No file h*.mtx found in {data_dir}")

    work_dir = cfg.WORKDIRS[scenario]["DAY"] / "iterative"
    output_sumo = cfg.SIM_OUT[scenario]["DAY"]
    run_tag = ""
    if close_id is not None:
        # one folder per what-if scenario: parallel runs must not overwrite each other
        work_dir = work_dir / f"closure_{close_id}_seed{cfg.SEED}"
        output_sumo = output_sumo / f"closure_{close_id}"
        run_tag = f"_{close_id}"
    work_dir.mkdir(parents=True, exist_ok=True)

    edge_taz_map = _edge_taz_map(taz_file)
    tls_variant = _tls_variant_of(scenario)
    tls_penalty = TLS_PENALTY_BY_VARIANT[tls_variant]
    det_file = det_file or cfg.DETECTORS[scenario]["DAY"] # override or default

    prev_weight_file: Optional[Path] = initial_wfile
    routes_final: Optional[Path] = None

    for round_idx in range(n_rounds):
        print(f"\n {scenario}/DAY: ROUND {round_idx} (24h at {scale*100}% load)")

        trips_output = work_dir / f"od_trips_r{round_idx}.odtrips.xml"
        netload_output = work_dir / f"netload_r{round_idx}.xml"

        routes_macro = run_marouter(
            net_file=net_file,
            od_matrices=day_matrices,
            taz_file=taz_file,
            out_dir=work_dir,
            trips_output=trips_output,
            additional_files=[det_file, cfg.VTYPE],
            netload_output=netload_output,
            data_dir=cfg.OD_MATRICES["DAY"] / f"scaled_{scale}",
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
            extra_args=[
                "-l",
                str(work_dir / f"marouter_r{round_idx}.log"),
                "--weight-adaption",
                str(INCREMENTAL_PARAMS["weight_adaption"]),
                "--seed", str(cfg.SEED),
            ],
        )
        print(
            f"\tDay macroscopic assignment round {round_idx} saved here: {routes_macro}"
        )

        routes_clean = filter_short_flows(
            routes_macro,
            output_new=work_dir / f"marouter_output_clean_r{round_idx}.rou.xml",
            edge_taz_map=edge_taz_map,
            min_edges=2,
        )
        routes_final = filter_zero_prob(routes_clean)
        next_wfile = _edge_weights_path(work_dir, round_idx+1)
        edgedata_add = work_dir / f"edgedata_config_r{round_idx}.add.xml"
        _write_edgedata_additional(edgedata_add, next_wfile)

        cfg_name = f"francia_peschiera_DAYITER_{scenario[3:]}{run_tag}_r{round_idx}_seed{cfg.SEED}.sumocfg"
        CfgAttributes(
            net=net_file,
            routes=routes_final,
            output_cfg=cfg.CFG_DIR,
            output_sumo=output_sumo,
            config_name=cfg_name,
            teleport="300",
            meso=True,
            setting=cfg.VIEW,
        ).build(
            method="marouter",
            taz=taz_file,
            begin=0,
            end=24 * 3600,
            detectors=det_file,
            edgedata=edgedata_add,
            vtype=cfg.VTYPE,
            seed=cfg.SEED,
        )

        sumocfg_path = cfg.CFG_DIR / cfg_name
        cmd = ["sumo", "-c", str(sumocfg_path)]
        print(Fore.CYAN + f"\tSUMO round {round_idx}...")
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            print(Fore.RED + f"ERROR for SUMO round {round_idx}")
            print("STDOUT:", e.stdout)
            print("STDERR:", e.stderr)
            raise

        if not next_wfile.exists():
            raise RuntimeError(
                f"edgeData not generated for round {round_idx}: {next_wfile}"
            )
        prev_weight_file = next_wfile
        print(
            f"\tRound {round_idx} completed. Weight file for next round: {prev_weight_file}"
        )

    print(Fore.GREEN + f"\n{scenario}/DAY completed ({n_rounds} rounds).")
    return routes_final