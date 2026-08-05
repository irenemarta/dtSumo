"""
@file       cmdOperations.py
@author     Irene Marta
@date       2026
"""

import os
import subprocess
from pathlib import Path
import shutil
from typing import List, Dict
from scripts.src.helpers import parse_edges, _process_multiple_ods
from scripts.src.inputs import config as cfg
from scripts.src.operations.filtering import filter_zero_flows, _spread_depart_trips

"""shutil.rmtree(): Deletes a directory along with its contents. 
It takes the directory path as an argument and removes all the files and subdirectories within it."""


### SUMO operations from cmd - helpers

ROUTERS = ["dijkstra", "astar", "CH"]


def _ensure_sumo_home():
    if "SUMO_HOME" not in os.environ:
        os.environ["SUMO_HOME"] = "/usr/share/sumo"


def _sumo_bin(tool: str) -> str:
    return os.path.join(os.environ["SUMO_HOME"], "bin", tool)


def _sumo_tool(*parts: str) -> str:
    return os.path.join(os.environ["SUMO_HOME"], "tools", *parts)


def run_cmd(cmd: list, tool_name: str = "tool"):
    """Run a subprocess, raising clear errors on failure."""
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        # if result.stdout:
        #     print(result.stdout)
        return result

    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"{tool_name} error:\n{e.stderr}") from e

    except FileNotFoundError:
        raise FileNotFoundError(
            f"{tool_name} not found. Check SUMO_HOME: {os.environ.get('SUMO_HOME')}"
        )


### DUAROUTER helper
def run_duarouter(
    net_file, trips_input, routes_output, routing_alg="dijkstra", additional_files: str | list=None, extra_args=None
):

    # out_path = Path(routes_output)
    # out_dir  = out_path.parent / routing_alg
    # out_dir.mkdir(parents=True, exist_ok=True)

    # final_routes_output = out_dir / out_path.name # "output/dijkstra/od_route.rou.xml"

    cmd = [
        _sumo_bin("duarouter"),
        "-n",
        str(net_file),
        "-r",
        str(trips_input),
        "-o",
        str(routes_output),
        # "-o", str(final_routes_output),
        "--routing-algorithm",
        routing_alg,
    ]
    if extra_args:
        cmd += extra_args
    
    if additional_files:
        cmd += [str(additional_files)]

    result = run_cmd(cmd, "duarouter")
    print(f"Routes saved to {routes_output}")
    # print(f"Routes saved to {final_routes_output}")

    return result, routes_output


### run simulation


def run_simulation(config_file):
    _ensure_sumo_home()

    run_cmd(
        [
            _sumo_bin("sumo"),
            "-c",
            config_file,
            "--no-step-log",
            "--duration-log.disable",
            "--no-warnings",
        ],
        tool_name="sumo",
    )


### Random trips/routes

VEHICLE_CONFIGS = [
    {
        "name": "cars",
        "vehicle_class": "passenger",
        "prefix": "car",
        "extra": [],
    },
    {
        "name": "bikes",
        "vehicle_class": "bicycle",
        "prefix": "bike",
        "extra": [],
    },
    {
        "name": "pedestrians",
        "vehicle_class": "pedestrian",
        "prefix": "pedestrian",
        "extra": ["--persontrips"],
    },
]


def random_routes(net_file: Path, out_dir: Path):
    _ensure_sumo_home()

    trips = {
        c["name"]: os.path.join(out_dir, f"{c['name']}.trips.xml")
        for c in VEHICLE_CONFIGS
    }
    routes = {
        c["name"]: os.path.join(out_dir, f"{c['name']}.rou.xml")
        for c in VEHICLE_CONFIGS
    }
    duration = 500

    print("\nCREATING TRIPS")
    for c in VEHICLE_CONFIGS:
        run_cmd(
            [
                "python3",
                _sumo_tool("randomTrips.py"),
                '-n', str(net_file),
                "-o",
                trips[c["name"]],
                "-b",
                "0",
                "-e",
                str(duration),
                "--vehicle-class",
                c["vehicle_class"],
                "--prefix",
                c["prefix"],
                "--validate",
            ]
            + c["extra"],
            tool_name="randomTrips.py",
        )
        print(f"{c['name'].capitalize()} trips → {out_dir}")

    print("\nCREATING ROUTES")

    for c in VEHICLE_CONFIGS:
        run_duarouter(net_file, trips[c["name"]], routes[c["name"]])

    return (
        trips[VEHICLE_CONFIGS[0]["name"]],
        routes[VEHICLE_CONFIGS[0]["name"]],
        trips[VEHICLE_CONFIGS[1]["name"]],
        routes[VEHICLE_CONFIGS[1]["name"]],
        trips[VEHICLE_CONFIGS[2]["name"]],
        routes[VEHICLE_CONFIGS[2]["name"]],
    )


### OD to trips/routes

_OD_DUAROUTER_ARGS = [
    "--ignore-errors",
    "--departlane",
    "best",
    "--departspeed",
    "max",
    "--departpos",
    "free",
    "--repair", "true",
    "--repair.from", "true",
    "--repair.to", "true",
    "--remove-loops", "true",
    "--weights.priority-factor", "0.3",
    "--weights.random-factor", "1.5",
    "--route-length", "true",
    "--randomize-flows", "true",
    # "--with-taz", "true",
]


def _od2trips(taz_file: Path, od_matrix: Path | List[str], trips_output: Path, extra_args=None):
    #path_config_save = os.path.join(trips_output.parent, "od2trips_config")
    if isinstance(od_matrix, (list, tuple)):
        od_str = ",".join(str(f) for f in od_matrix)
    else:
        od_str = str(od_matrix)
    cmd = [
        _sumo_bin("od2trips"),
        "-n",
        str(taz_file),
        "--od-matrix-files",
        od_str,
        "-o",
        str(trips_output),
        #"-C", # --save-configuration
        #str(path_config_save)
    ]
    if extra_args:
        cmd += extra_args
    run_cmd(cmd, "od2trips")
    print(f"Trips from O/D to {trips_output}")


def trips_routes_from_od(net_file: Path, taz_file, od_matrix, local_priority_threshold, out_dir="."):
    _ensure_sumo_home()
    #print("DEBUG out_dir:", type(out_dir), repr(out_dir))
    trips = os.path.join(out_dir, "od_trips_file.odtrips.xml")
    trips_via_path = os.path.join(out_dir, "od_trips_via.trips.xml")
    routes_output = os.path.join(out_dir, "od_route_file.odtrips.rou.xml")
    
    edges = parse_edges(edg_file=cfg.EDG_PARSE_XML)

    _od2trips(taz_file, od_matrix, trips)

    routes_by_router = {}
    for r in ROUTERS:
        _, final_path = run_duarouter(
            net_file=net_file,
            trips_input=trips,
            routes_output=routes_output,
            routing_alg=r,
            extra_args=_OD_DUAROUTER_ARGS,
        )
        routes_by_router[r] = final_path

    return trips, routes_by_router



### duaIterate helpers -> for Dynamic User Equilibrium.
def _find_last_complete_iteration(out_dir: str):
    out_dir = Path(out_dir)
    if not out_dir.exists():
        return None

    # Last route file = last iteration working directory
    ### last_iter = str(iterations - 1).zfill(3)  -> no se equilibrio raggiunto prima dell'ultima iterazione
    subdirs = [
        sdir.name for sdir in out_dir.iterdir() if sdir.is_dir() and sdir.name.isdigit()
    ]
    if not subdirs:
        return None

    last_iter = int(sorted(subdirs, key=int)[-1])

    return last_iter


def _find_last_dua_iteration(trips_str: str, out_dir: str):
    out_dir = Path(out_dir)

    # Last route file = last iteration working directory
    ### last_iter = str(iterations - 1).zfill(3)  -> no se equilibrio raggiunto prima dell'ultima iterazione
    subdirs = [
        sdir.name for sdir in out_dir.iterdir() if sdir.is_dir() and sdir.name.isdigit()
    ]
    if not subdirs:
        return None

    last_iter = sorted(subdirs, key=int)[-1]
    trips_stem = Path(trips_str.split(",")[0]).stem.replace(".odtrips", "")
    routes_iterative = out_dir / last_iter / f"{trips_stem}_{last_iter}.rou.xml"

    if not routes_iterative.exists():
        # fallback 1: compressed version of the routes (.gz)
        routes_iterative_gz = (
            out_dir / last_iter / f"{trips_stem}_{last_iter}.rou.xml.gz"
        )
        if routes_iterative_gz.exists():
            routes_iterative = routes_iterative_gz
        else:
            # fallback 2: cerca qualsiasi .rou.xml o .rou.xml.gz
            candidates = list((out_dir / last_iter).glob("*.rou.xml")) + list(
                (out_dir / last_iter).glob("*.rou.xml.gz")
            )
            if candidates:
                routes_iterative = candidates[0]
            else:
                raise FileNotFoundError(f"No .rou.xml found in {out_dir / last_iter}")

    return routes_iterative

    ### Stochastic User Equilibrium - duaiterate with Gawron / Logit

    """
    REFERENCE: https://github.com/argos-research/sumo/blob/master/tools/assign/duaIterate.py
    """

def drop_iteration_snapshot(workdir: Path, step: int, checkpoints: set):
    """Avoid to save unnecesary data; store information output every x iterations."""
    save_step_str = str(step).zfill(3) # to make sure the directory has a valid name format
    checkpoints.add(save_step_str)
    
    all_dirs = sorted(
        [int(s) for s in checkpoints if s.isdigit()]
    )
    protected = set(checkpoints)
    if all_dirs:
        predecessor = str(all_dirs[-1] - 1).zfill(3)
        protected.add(predecessor)
    
    for subdir in workdir.iterdir():
        if subdir.is_dir() and subdir.name.isdigit():
            if subdir.name not in protected:
                shutil.rmtree(subdir) # removes directory which doesn't correspond to the save step
                print(f"CLEAN STORAGE: Removed {subdir.name}")


def run_duaIterate(
    net_file: Path,
    trips_file: list | tuple | Path,
    out_dir: Path,
    iterations: int = 50,
    begin: int = None,
    end: int = None,
    aggregation: int = 900,
    routing_alg: str = "dijkstra",
    save_every: int = None, # save output every save_every step
    # if SUE:
    route_choice: str = None,  # DUE if none, else SUE ("gawron"/"logit")
    gawron_beta: float = 0.3,
    gawron_a: float = 0.05,
    logit_theta: float = 0.01,  # parameter to adapt the cost unit
    logit_gamma: float = 1,  # "use the c-logit model for route choice
    logit_beta: float = 0.15,  # use the c-logit model for route choice; logit model when beta = 0
    use_meso: bool = False,
    extra_args: list = None,
    additional_files: list | tuple | Path=None,
) -> Path:
    """
    Performs DUE or SUE basing on the route_choice parameter

    SUE: duaiterate with stocasthic route choice (Gawron o Logit).

    NB: it is important to set --convergence-steps in order to garantuee convergence
    """

    out_dir = Path(out_dir)
    net_file = Path(net_file)
    label = route_choice if route_choice else "Iterative DUE"

    print(f"\nRunning DUAITERATE ({label}) in {out_dir}")

    _ensure_sumo_home()
    os.makedirs(out_dir, exist_ok=True)

    """
    The isinstance() function returns True 
    if the specified object is of the specified type, otherwise False.
    """
    
    trips_str = (
        ",".join(str(t) for t in trips_file)
        if isinstance(trips_file, (list, tuple))
        else str(trips_file)
    )


    def _cmd_definition(first_step: int, last_step: int) -> list:
        cmd = [
            "python3",
            _sumo_tool("assign", "duaIterate.py"),
            "-n", str(net_file),
            "-t", trips_str,
            "-l", str(last_step),
            "--first-step", str(first_step),  # 0 or equal to the last one found
            "--aggregation", str(aggregation),
            "--convergence-steps", "5",
            "--routing-algorithm", routing_alg,
            "--continue-on-unbuild",
        ]

        # stocasthic route choice
        if route_choice == "gawron":
            cmd += [
                "-B", str(gawron_beta),
                "-A", str(gawron_a),
            ]
        elif route_choice == "logit":
            cmd += [
                "--logitbeta", str(logit_beta),
                "--logitgamma", str(logit_gamma),
                "--logittheta", str(logit_theta),
            ]

        if additional_files:
            additionals_str = (
                ",".join(str(a) for a in additional_files)
                if isinstance(additional_files, (list, tuple))
                else str(additional_files)
            )
            cmd += ["-+", additionals_str]

        if begin is not None:
            cmd += ["-b", str(begin), "-e", str(end)]

        if use_meso:
            cmd += ["-mesosim"]

        if extra_args:
            cmd += extra_args
        
        return cmd

    original_dir = os.getcwd()
    try:
        os.chdir(out_dir)
        
        # Check if there are any pre-computed steps for the algorithm
        last_step = _find_last_complete_iteration(out_dir=out_dir)
        start_step = 0 if last_step is None else last_step
        
        if last_step is not None:
            print(f"Found checkpoint {last_step}\tResuming from step {start_step}")
        else:
            print(f"Starting from step {start_step}")

        if save_every is None:
            cmd = _cmd_definition(first_step=start_step, last_step=iterations)
            subprocess.run(cmd, check=True, text=True)
        else:
            checkpoints = {"000"}
            for step in range(start_step, iterations):
                print(f"Executing step {step}")
                cmd = _cmd_definition(first_step=step, last_step=step + 1)
                subprocess.run(cmd, check=True, text=True)

                is_check = ((step + 1) % save_every == 0) or ((step + 1) == iterations)

                if is_check:
                    print(f"DUAITERATE: Checkpoint at step {step}")
                    drop_iteration_snapshot(workdir=out_dir, step=step, checkpoints=checkpoints)
                else:
                    continue

            
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"duaIterate error (exit {e.returncode})") from e
    finally:
        os.chdir(original_dir)

    result = _find_last_dua_iteration(trips_str=trips_str, out_dir=out_dir)

    print(f"\nduaIterate performed: {result}")

    return result


def run_marouter(
    net_file: Path,
    od_matrices: list | tuple | Path,
    taz_file: Path,
    out_dir: Path,
    # weights: Path,
    trips_output: Path | List[Path],
    #taz_rel: Path,
    data_dir: Path = None,
    route_choice: str = "gawron", # defualt: "gawron"
    gawron_beta: float = 0.9, # Controls driver route memory # default = 0.3
    gawron_a: float = 0.3, # Controls the route set generation/update and convergence performance. # default = 0.05
    logit_theta: float = 0.1,  # parameter to adapt the cost unit (DEFAULT: 0.01)
    logit_gamma: float = 1,  # "use the c-logit model for route choice
    logit_beta: float = 0.0,  # use the c-logit model for route choice (default 0.15); pure logit model when beta = 0
    method: str = "SUE", # incremental, UE or SUE
    routing_alg: str = "dijkstra",
    scale: float = 1.0,
    #agg_interval: int = 1,
    netload_output: str = None,
    all_pair_output: str = None,
    weights_priority: float = 0.3,
    paths: int = 18,
    path_penalty: float = 15.0,
    max_iterations: int = 100,
    max_inner_iterations: int = 1000, # DEFAULT: 1000
    max_alternatives: int = 20,
    weights_tls: int | float = 5.0,
    weight_files: Path = None,
    begin: int = None,
    end: int = None,
    additional_files=None,
    tolerance: float = 0.001,
    extra_args=None
) -> Path:
    

    out_dir = Path(out_dir)
    net_file = Path(net_file)
    #path_config_save = os.path.join(trips_output.parent, "marouter_config")

    output_filename = "marouter_output.rou.xml"
    output_path = out_dir / output_filename

    _ensure_sumo_home()
    os.makedirs(out_dir, exist_ok=True)
    if data_dir:
        os.makedirs(Path(data_dir, "od_rounded"), exist_ok=True)

    """
    The isinstance() function returns True 
    if the specified object is of the specified type, otherwise False.
    """

    # ods_str = (
    #     ",".join(str(od) for od in od_matrices)
    #     if isinstance(od_matrices, (list, tuple))
    #     else str(od_matrices)
    # )
    
    if isinstance(od_matrices, str):
        od_matrices = od_matrices.split(",")
    elif not isinstance(od_matrices, (list, tuple)):
        od_matrices = [od_matrices]
    
    if od_matrices:
        if data_dir:
            out_path_od = Path(data_dir) / "od_rounded"
        else: 
            out_path_od = Path(od_matrices[0]).parent
        
        cleaned_matrices = _process_multiple_ods(
            od_matrices,
            out_path=out_path_od,
        )
        
        _od2trips(
            taz_file,
            cleaned_matrices,
            trips_output,
            extra_args=["--spread.uniform", "true",
                        "--flow-output", str(trips_output),
                        "--flow-output.probability", "true",
                        "--departspeed", "max",
                        "--departlane", "best"
            ],
        )
    #     trips_spread = _spread_depart_trips(Path(trips_output))
    #     filter_zero_flows(trips_xml=trips_spread)
    
    # trips_arg = (
    #     ",".join(str(t) for t in trips_spread)
    #     if isinstance(trips_spread, (list, tuple))
    #     else str(trips_spread)
    # )

        filter_zero_flows(trips_xml=trips_output)
    
    trips_arg = (
        ",".join(str(t) for t in trips_output)
        if isinstance(trips_output, (list, tuple))
        else str(trips_output)
    )

    cmd = [
        _sumo_bin("marouter"),
        "-n",
        str(net_file),
        "-o",
        str(output_path),
        #"-C", # save configuration
        #str(path_config_save),
        "-r", trips_arg,
        #"-m", str(ods_str),
        #"--scale",
        #str(scale_factor),
        "--assignment-method",
        method,
        "--routing-algorithm",
        routing_alg,
        # "--tazrelation-files",
        # str(taz_rel),
        "--paths", str(paths),
        "--paths.penalty", str(path_penalty),
        "--max-alternatives", str(max_alternatives),
        #"--aggregation-interval", str(agg_interval),
        #"--skip-new-routes", "false", # force new routes research
        "--ignore-errors", "true", # continue even in case of errors
        # "--with-taz", "true",
        # TO MANAGE TLS EFFECT
        "--weights.expand", "true", # infinitelly expands last interval weight file
        "--weights.tls-penalty", str(weights_tls),  # seconds penalty for each tls
        "--weights.priority-factor", str(weights_priority), # Consider edge priorities in addition to travel times, weighted by factor
        "--vtype", "passenger",
        "--capacities.default", "true",
        #"--verbose"
        
        # "--route-choice-method", str(route_choice),
        # "--max-iterations", str(max_iterations),
        # "--taz-param", "weight", # Parameter key(s) defining source (and sink) taz
        "--weight-files", str(weight_files),
        ]
    
    if netload_output:
        cmd += ["--netload-output", str(netload_output)]
    if all_pair_output:
        cmd += ["--all-pairs-output", str(netload_output)]
        
    if scale:
        cmd += ['--scale', str(scale)]

    if method.upper() == "SUE":
        cmd += [
            "--max-inner-iterations", str(max_inner_iterations),
            "--max-iterations", str(max_iterations),
            "--route-choice-method", str(route_choice),
            "--tolerance", str(tolerance)
        ]

        if route_choice == "gawron":
            cmd += [
                "--gawron.beta",
                str(gawron_beta),
                "--gawron.a",
                str(gawron_a),
            ]
        elif route_choice == "logit":
            cmd += [
                "--logit.beta",
                str(logit_beta),
                "--logit.gamma",
                str(logit_gamma),
                "--logit.theta",
                str(logit_theta),
            ]
            
    elif method.lower() == "incremental":
        cmd += ["--route-choice-method", str(route_choice),
            "--max-iterations", str(max_iterations),
            "--taz-param", "weight", # Parameter key(s) defining source (and sink) taz
                ]

    additional_list = [str(taz_file)]
    if additional_files:
        if isinstance(additional_files, (list, tuple)):
            additional_list += [str(a) for a in additional_files]
        else:
            additional_list.append(str(additional_files))

    cmd += ["-a", ",".join(additional_list)]

    if begin is not None:
        cmd += ["-b", str(begin), "-e", str(end)]
        
    if extra_args:
        if isinstance(extra_args, (list, tuple)):
            cmd.extend([str(arg) for arg in extra_args])
        else:
            cmd.append(str(extra_args))

    print(f"Running marouter in {out_dir}")

    try:
        #cmd = [str(arg) for arg in cmd]
        result = subprocess.run(cmd, check=True, text=True, capture_output=True)
        print(result.stdout)
        print(f"marouter performed: {output_path}")
    except subprocess.CalledProcessError as e:
        print(f"Error output: {e.stderr}")
        print(e.stdout)
        raise RuntimeError(f"marouter error (exit {e.returncode})") from e

    return output_path