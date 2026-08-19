"""
TuST section 4 routing logic applied by this script:

4.1  Road Graph + TAZ:
-> parse_edges() + read_revisioned_TAZ(): to produce a unice taz file

4.2  Traffic Assignment macroscopico
-> run_sue_feedback_cycle(): iterative SUE assignment with real-travel-time
   feedback (marouter -> sumo -> edgeData -> marouter -> ...), n_rounds times
-> filter_short_flows() removes trips that are not long enough

4.3  Extension O'/D' using duarouter, applied ONCE after the feedback cycle
     has converged (not on every round, to keep the O'/D' random sampling
     from adding noise to the round-to-round comparison).
"""

import math
import random
import subprocess
import xml.etree.ElementTree as ET
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import sumolib

import scripts.src.inputs.config as cfg
from scripts.src.helpers import parse_edges
from scripts.src.operations.cmd import run_marouter, run_duarouter
from scripts.src.operations.filtering import filter_short_flows, filter_zero_prob
from scripts.tazOD import read_revisioned_TAZ

from scripts.src.modules.entities import CfgAttributes

# Configuration

SCENARIOS_MA = ["MA_no_TLS", "MA_with_TLS"]
PERIODS_TO_RUN = ["AM", "PM"]  # "DAY" separately managed

FRACTION_TRIPS_TO_EXTEND = 1.0  # fraction to extend to O'/D'
DEFAULT_DAY_SCALE = 1.0
RESIDENTIAL_TYPES = {
    "highway.residential",
    #"highway.unclassified",
    "highway.service",
}

USE_PRIORITY_FALLBACK = True
RESIDENTIAL_PRIORITY_THRESHOLD = 4

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
)

INCREMENTAL_PARAMS = dict(
    method="incremental",
    route_choice="gawron",
    paths=10,
    path_penalty=25.0,
    weights_priority=0.0,
    max_iterations=50,
)

# freq (secondi) del dump edgeData usato per il feedback SUE.
EDGEDATA_FREQ = 1800


# STEP 4.1 — Road graph + TAZ


def build_network_zones() -> Path:
    """
    Builds Taz file from VISUM shapefile (see tazOD.py)
    """
    edges = parse_edges(cfg.EDG_PARSE_XML)
    taz_file = read_revisioned_TAZ(
        edges,
        cfg.ZONES,
        cfg.CONNECTORS,
        output_path=cfg.OUTPUT_DIR_ADD,
    )
    return taz_file


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
    Legge un dump edgeData multi-intervallo e aggrega per edge,
    invece di prendere l'ultimo intervallo letto.
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
    taz_file: Path,
    net: "sumolib.net.Net",
    n_rounds: int = 3,
    scouting_duration: Optional[int] = None,
    edge_taz_map: Optional[Dict[str, str]] = None,
) -> Path:
    """
    Runs n_rounds of: marouter (using previous round's measured
    travel times as weights) -> sumo -> edgeData dump -> next round.

    Round 0 has no external weights (pure marouter cost function).
    Returns the path to the cleaned (short-flows + zero-prob filtered)
    route file of the LAST round, WITHOUT the O'/D' extension — that is
    applied once, separately, by the caller after the cycle converges.

    NB scouting_duration restricts simulation end to begin + scouting_duration.
    """
    
    METHOD = SUE_PARAMS['method']
    work_dir = cfg.WORKDIRS[scenario][period] / METHOD
    work_dir.mkdir(parents=True, exist_ok=True)

    if edge_taz_map is None:
        edge_taz_map = build_edge_to_taz(taz_file)

    tls_variant = _tls_variant_of(scenario)
    tls_penalty = TLS_PENALTY_BY_VARIANT[tls_variant]

    period_begin = cfg.PERIODS[period]["start"]
    period_end = cfg.PERIODS[period]["end"]

    if scouting_duration:
        sumo_end = period_begin + scouting_duration
    else:
        sumo_end = period_end

    prev_weight_file: Optional[Path] = None # no pesi primo giro
    routes_final: Optional[Path] = None

    for round_idx in range(n_rounds):
        # alpha_n = 1 / (round_idx +1)
        print(f"\n===== [{scenario}/{period}] SUE ROUND {round_idx} (end={sumo_end}) =====")

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
                        "--weight-adaption", str(0.8),
                        "--seed", "42"],
            **SUE_PARAMS,
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
            edgedata=edgedata_additional,  # file additional CHE DICE a SUMO di scrivere
        )

        sumocfg_path = cfg.CFG_DIR / cfg_name

        cmd = ["sumo", "-c", str(sumocfg_path), "--end", str(sumo_end)]
        print(f"\tLancio SUMO round {round_idx}...")
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            print(f"[ERRORE SUMO round {round_idx}]")
            print("STDOUT:", e.stdout)
            print("STDERR:", e.stderr)
            raise
        
        if not next_weight_file.exists():
            raise RuntimeError(
                f"edgeData non generato per il round {round_idx}: {next_weight_file}"
            )
            
            
        print(f"\tRound {round_idx} completo. Pesi per il prossimo round: {next_weight_file}")
        prev_weight_file = next_weight_file

    print(f"\n[{scenario}/{period}] Ciclo SUE completato: {n_rounds} round eseguiti.")
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
        meso=False,
        setting=cfg.VIEW,
    ).build(
        method="marouter",
        taz=taz_file,
        begin=cfg.PERIODS[period]["start"],
        end=cfg.PERIODS[period]["end"],
        detectors=detectors_file,
        edgedata=edgedata_additional,
    )
    print(f"\t\t.sumocfg [{scenario}/{period}] scritto puntando a: {routes_final}")


# STEP 4.2-DAY — Traffic Assignment for the entire day (24 matrici orarie)


def run_macroscopic_assignment_day(
    scenario: str, taz_file: Path, scale: float = DEFAULT_DAY_SCALE
) -> Path:
    """
    Executes marouter for the entire day, only once (begin=0, end=86400),
    using the folder of hour matrices as input (h00.mtx..h23.mtx).
    Kept as incremental (non-iterative), for computational scalability over entire day.
    """
    from scripts.allDayOD import generate_hour_matrices

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
        f"[4.2-DAY] MAROUTER — scenario={scenario} ({len(day_matrices)} matrices, scale={scale})..."
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
        max_iterations=50,
        tolerance=0.01,
        weights_tls=tls_penalty,
        begin=0,
        end=24 * 3600,
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
        teleport="100",
        meso=False,
        setting=cfg.VIEW,
    ).build(
        method="marouter",
        taz=taz_file,
        begin=0,
        end=86400,
        detectors=detectors_file,
        edgedata=edgedata_additional,
    )
    print(f"\t\t.sumocfg [{scenario}/DAY] scritto puntando a: {routes_final}")


# STEP 4.3a — TAZ -> find residential/service edges (controviali)


def _taz_edge_ids(taz_el: ET.Element) -> List[str]:
    ids = list(taz_el.get("edges", "").split())
    for t in taz_el:
        if t.tag in ("tazSource", "tazSink") and t.get("id"):
            ids.append(t.get("id"))
    seen = set()
    out = []
    for eid in ids:
        if eid not in seen:
            seen.add(eid)
            out.append(eid)
    return out

# Each candidate is stored as (edge_id, mid_x, mid_y) so the extension step
# can rank candidates by geographic proximity to the trip's attach point,
# instead of picking uniformly at random over the whole TAZ.
ResidentialCandidate = Tuple[str, float, float]


def build_residential_edges_by_taz(
    net: "sumolib.net.Net", taz_file: Path
) -> Dict[str, List[ResidentialCandidate]]:
    taz_tree = ET.parse(taz_file)
    all_tazs = taz_tree.getroot().findall(".//taz")

    taz_edges: Dict[str, List[ResidentialCandidate]] = {}
    n_total_edges = 0
    n_matched_edges = 0
    n_missing_in_net = 0
    types_seen = set()

    for taz in all_tazs:
        taz_id = taz.get("id")
        edge_ids = _taz_edge_ids(taz)
        n_total_edges += len(edge_ids)

        candidates: List[ResidentialCandidate] = []
        for eid in edge_ids:
            if not net.hasEdge(eid):
                n_missing_in_net += 1
                continue
            edge = net.getEdge(eid)
            etype = edge.getType()
            types_seen.add(etype)

            is_residential = etype in RESIDENTIAL_TYPES
            if not is_residential and USE_PRIORITY_FALLBACK:
                low_capacity_street = edge.getPriority() <= RESIDENTIAL_PRIORITY_THRESHOLD
                is_artery = any(kw in etype.lower() for kw in ["primary", "secondary", "tertiary"])
                if low_capacity_street and not is_artery:
                    is_residential = True

            if is_residential:
                shape = edge.getShape()
                mid = shape[len(shape) // 2]
                candidates.append((eid, mid[0], mid[1]))
                n_matched_edges += 1
        taz_edges[taz_id] = candidates

    n_taz_with_candidates = sum(1 for v in taz_edges.values() if v)
    print(
        f"      [debug] TAZ totali: {len(all_tazs)} | edge totali referenziati: {n_total_edges} "
        f"| edge non trovati in rete: {n_missing_in_net} | edge residenziali/service trovati: {n_matched_edges} "
        f"(type-match o priority<={RESIDENTIAL_PRIORITY_THRESHOLD}) "
        f"| TAZ con almeno un candidato: {n_taz_with_candidates}"
    )
    print(
        f"      [debug] typeID distinti visti sugli edge delle TAZ (primi 20): "
        f"{sorted(types_seen)[:20]}"
    )

    return taz_edges


def build_edge_to_taz(taz_file: Path) -> Dict[str, str]:
    taz_tree = ET.parse(taz_file)
    mapping = {}
    for taz in taz_tree.getroot().findall(".//taz"):
        taz_id = taz.get("id")
        for eid in _taz_edge_ids(taz):
            mapping[eid] = taz_id
    return mapping


# STEP 4.3b — Shortest path O'->O / D->D' using run_duarouter (BATCH)

MAX_EXTENSION_ROUNDS = 10 # retry rounds for different candidate, for non-resolved trips.
MAX_USES_PER_EDGE = 15 # cap for passages over a selected edge
MAX_EXTENSION_RADIUS_M = 500.0 # boundery radius (distance in m) from connector for which a residential edge is considerable as candidate for the extension.
K_NEAREST_CANDIDATES = 20 # among the candidates, how many can form the pool for random.choice selection.


def _dist(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    return math.hypot(p1[0] - p2[0], p1[1] - p2[1])


def _nearest_candidates(
    candidates: List[ResidentialCandidate],
    ref_point: Tuple[float, float],
    tried: set,
    exclude_edge: str,
    edge_use_count: Dict[str, int],
    k: int,
) -> List[str]:
    """
    Returns up to k edge_id ordered per growing distance from ref_point.
    It iterates over the non-analysed candidates, different from attach_edges, included in MAX_EXTENSION_RADIUS_M + non-saturated (MAX_USES_PER_EDGE).
    """
    scored = []
    for eid, x, y in candidates:
        if eid in tried or eid == exclude_edge:
            continue
        if edge_use_count.get(eid, 0) >= MAX_USES_PER_EDGE:
            continue
        d = _dist((x, y), ref_point)
        if d <= MAX_EXTENSION_RADIUS_M:
            scored.append((d, eid))
    scored.sort(key=lambda t: t[0])
    return [eid for _, eid in scored[:k]]


def _run_duarouter_batch(
    trip_specs: List["tuple[str, str, str]"], tmp_dir: Path, tag: str
) -> Dict[str, List[str]]:
    if not trip_specs:
        return {}

    tmp_dir.mkdir(parents=True, exist_ok=True)
    trips_file = tmp_dir / f"{tag}_trips.xml"
    routes_file = tmp_dir / f"{tag}_routes.xml"

    root = ET.Element("routes")
    for trip_id, from_edge, to_edge in trip_specs:
        ET.SubElement(
            root, "trip", id=trip_id, depart="0", **{"from": from_edge, "to": to_edge}
        )
    ET.ElementTree(root).write(trips_file)

    try:
        run_duarouter(
            net_file=cfg.NET_FILE,
            trips_input=trips_file,
            routes_output=routes_file,
            routing_alg="dijkstra",
            extra_args=["--ignore-errors", "true"],
        )
    except RuntimeError as e:
        print(f"[ERROR duarouter batch] tag={tag}: error={e}")

    results: Dict[str, List[str]] = {}
    if routes_file.exists():
        tree = ET.parse(routes_file)
        for veh in tree.getroot().findall("vehicle"):
            route_el = veh.find("route")
            if route_el is not None:
                results[veh.get("id")] = route_el.get("edges", "").split()

    trips_file.unlink(missing_ok=True)
    routes_file.unlink(missing_ok=True)
    return results


def _resolve_terminal_edges_batch(
    pending: Dict[str, "tuple[str, str]"],
    residential_by_taz: Dict[str, List[ResidentialCandidate]],
    direction: str,
    tmp_dir: Path,
    scenario_tag: str,
    edge_use_count: Dict[str, int],
    net: "sumolib.net.Net",
) -> Dict[str, List[str]]:
    resolved: Dict[str, List[str]] = {}
    tried: Dict[str, set] = {vid: set() for vid in pending}
    still_pending = dict(pending)

    for round_idx in range(MAX_EXTENSION_ROUNDS):
        if not still_pending:
            break

        batch_specs = []
        reserved_this_round: Dict[str, str] = {}

        for vid, (taz_id, attach_edge) in still_pending.items():
            edge_obj = net.getEdge(attach_edge)
            # prefix: the beginning of attach_edgei is the reference point (the route entry point)
            # suffix: the end of the route (exit point)
            ref_node = (
                edge_obj.getFromNode() if direction == "prefix" else edge_obj.getToNode()
            )
            ref_point = ref_node.getCoord()
            near = _nearest_candidates(
                residential_by_taz.get(taz_id, []),
                ref_point,
                tried[vid],
                attach_edge,
                edge_use_count,
                K_NEAREST_CANDIDATES,
            )
            if not near:
                continue
            candidate = random.choice(near)
            tried[vid].add(candidate)
            reserved_this_round[vid] = candidate
            edge_use_count[candidate] = edge_use_count.get(candidate, 0) + 1
            if direction == "prefix":
                batch_specs.append((vid, candidate, attach_edge))
            else:
                batch_specs.append((vid, attach_edge, candidate))

        batch_results = _run_duarouter_batch(
            batch_specs, tmp_dir, tag=f"{scenario_tag}_{direction}_r{round_idx}"
        )

        for vid, candidate in reserved_this_round.items():
            if vid in batch_results:
                resolved[vid] = batch_results[vid]
                still_pending.pop(vid, None)
            else:
                # unresolved duarouter
                edge_use_count[candidate] = max(0, edge_use_count.get(candidate, 1) - 1)

    return resolved
# STEP 4.3c - Extension of all selected batches

def extend_trips_batch(
    selected_vehicles,
    residential_by_taz: Dict[str, List[ResidentialCandidate]],
    tmp_dir: Path,
    scenario_tag: str,
    net: "sumolib.net.Net",
):
    pending_prefix = {}
    pending_suffix = {}
    for vid, info in selected_vehicles.items():
        edges = info["edges"]
        if len(edges) < 2:
            continue
        pending_prefix[vid] = (info["taz_o"], edges[0])
        pending_suffix[vid] = (info["taz_d"], edges[-1])

    edge_use_count = {}

    print(f"\tBatch O'->O for {len(pending_prefix)} trips...")
    prefixes = _resolve_terminal_edges_batch(
        pending_prefix, residential_by_taz, "prefix", tmp_dir, scenario_tag,
        edge_use_count, net,
    )

    print(f"\tBatch D->D' for {len(pending_suffix)} trips...")
    suffixes = _resolve_terminal_edges_batch(
        pending_suffix, residential_by_taz, "suffix", tmp_dir, scenario_tag,
        edge_use_count, net,
    )

    extended = {}
    n_both = n_prefix_only = n_suffix_only = 0

    for vid, info in selected_vehicles.items():
        has_prefix = vid in prefixes
        has_suffix = vid in suffixes

        if has_prefix and has_suffix:
            extended[vid] = prefixes[vid][:-1] + info["edges"] + suffixes[vid][1:]
            n_both += 1
        elif has_prefix:
            # extended origin, same destination
            extended[vid] = prefixes[vid][:-1] + info["edges"]
            n_prefix_only += 1
        elif has_suffix:
            # extended destination, same origin
            extended[vid] = info["edges"] + suffixes[vid][1:]
            n_suffix_only += 1

    print(
        f"      -> solved prefix: {len(prefixes)}/{len(pending_prefix)} | "
        f"solved suffix: {len(suffixes)}/{len(pending_suffix)} | "
        f"estesi completi: {n_both} | solo O': {n_prefix_only} | solo D': {n_suffix_only} | "
        f"totale estesi: {len(extended)}"
    )

    import json
    (tmp_dir.parent / "edge_use_count.json").write_text(json.dumps(edge_use_count))
    return extended

# STEP 4.3d — Extension for a fraction of trips per scenario, period

def _select_route_element(veh: ET.Element) -> Optional[ET.Element]:
    route_el = veh.find("route")
    if route_el is not None:
        return route_el

    route_dist = veh.find("routeDistribution")
    if route_dist is not None:
        routes = route_dist.findall("route")
        if not routes:
            return None
        if len(routes) == 1:
            return routes[0]

        def _route_key(r):
            prob = float(r.get("probability", 0) or 0)
            cost = float(r.get("cost", "inf") or "inf")
            return (-prob, cost)

        return sorted(routes, key=_route_key)[0]

    return None


def extend_subset_of_trips(
    scenario: str,
    period: str,
    routes_macro: Path,
    net: "sumolib.net.Net",
    edge_taz_map: Dict[str, str],
    residential_by_taz: Dict[str, List[ResidentialCandidate]],
) -> Path:
    """
    Apply O'/D' extension to a fraction of the total number of routes (FRACTION_TRIPS_TO_EXTEND).
    Only performed over last run after cycle convergence.
    """
    work_dir = cfg.WORKDIRS[scenario][period]
    tmp_dir = work_dir / "tmp_duarouter"

    tree = ET.parse(routes_macro)
    root = tree.getroot()

    top_level_routes = {
        r.get("id"): r.get("edges", "").split()
        for r in root.findall("route")
        if r.get("id") is not None
    }

    vehicles = root.findall("vehicle") + root.findall("trip")
    n_to_extend = int(len(vehicles) * FRACTION_TRIPS_TO_EXTEND)
    selected_ids = set(v.get("id") for v in random.sample(vehicles, n_to_extend))

    selected_vehicles = {}
    veh_elements = {}

    n_direct = n_distribution = n_referenced = n_no_route_found = n_no_taz = 0

    for veh in vehicles:
        vid = veh.get("id")
        if vid not in selected_ids:
            continue

        route_el = _select_route_element(veh)
        original_edges = None
        via_top_level_id = None

        if route_el is not None:
            original_edges = route_el.get("edges", "").split()
            if veh.find("route") is not None:
                n_direct += 1
            else:
                n_distribution += 1
        else:
            route_ref = veh.get("route")
            if route_ref is not None and route_ref in top_level_routes:
                original_edges = top_level_routes[route_ref]
                via_top_level_id = route_ref
                n_referenced += 1

        if not original_edges:
            n_no_route_found += 1
            continue

        taz_o = edge_taz_map.get(original_edges[0]) or veh.get("fromTaz")
        taz_d = edge_taz_map.get(original_edges[-1]) or veh.get("toTaz")
        if taz_o is None or taz_d is None:
            n_no_taz += 1
            continue

        selected_vehicles[vid] = {
            "edges": original_edges,
            "taz_o": taz_o,
            "taz_d": taz_d,
        }
        veh_elements[vid] = (veh, route_el, via_top_level_id)

    print(
        f"[4.3] Estensione O'/D' [{scenario}/{period}]: {n_to_extend}/{len(vehicles)} viaggi "
        f"selezionati ({FRACTION_TRIPS_TO_EXTEND:.0%}) | "
        f"route dirette: {n_direct} | da routeDistribution: {n_distribution} | "
        f"referenced-by-id: {n_referenced} | nessuna route trovata: {n_no_route_found} | "
        f"senza TAZ: {n_no_taz}"
    )

    scenario_tag = f"{scenario}_{period}"
    extended = extend_trips_batch(
        selected_vehicles, residential_by_taz, tmp_dir, scenario_tag, net
    )

    new_route_counter = 0
    for vid, new_edges in extended.items():
        veh, route_el, via_top_level_id = veh_elements[vid]
        if via_top_level_id is None:
            route_el.set("edges", " ".join(new_edges))
        else:
            new_route_counter += 1
            new_route_id = f"{via_top_level_id}_ext{new_route_counter}"
            ET.SubElement(root, "route", id=new_route_id, edges=" ".join(new_edges))
            veh.set("route", new_route_id)

    routes_final = work_dir / "marouter_output_extended.rou.xml"
    tree.write(routes_final)
    print(f"\t{len(extended)} extended trips; output: {routes_final}")
    return routes_final


# MAIN


def run_pipeline_for(
    scenario: str,
    period: str,
    net: "sumolib.net.Net",
    taz_file: Path,
    edge_taz_map: Dict[str, str],
    residential_by_taz: Dict[str, List[ResidentialCandidate]],
    n_rounds: int = 3,
    scouting_duration: Optional[int] = None,
):
    routes_macro_iterated = run_feedback_cycle(
        scenario, period, taz_file, net,
        n_rounds=n_rounds,
        scouting_duration=scouting_duration,
        edge_taz_map=edge_taz_map,
    )
    routes_final = extend_subset_of_trips(
        scenario, period, routes_macro_iterated, net, edge_taz_map, residential_by_taz
    )
    build_final_sumocfg(scenario, period, taz_file, routes_final)


def main(
    run_peaks: bool = True,
    run_day: bool = True,
    day_scale: float = DEFAULT_DAY_SCALE,
    n_rounds: int = 3,
    scouting_duration: Optional[int] = None,
    scenarios: Optional[List[str]] = None,
    periods: Optional[List[str]] = None,
):
    random.seed(42)

    scenarios = scenarios or SCENARIOS_MA
    periods = periods or PERIODS_TO_RUN

    taz_file = build_network_zones()
    net = sumolib.net.readNet(str(cfg.NET_FILE))
    edge_taz_map = build_edge_to_taz(taz_file)
    residential_by_taz = build_residential_edges_by_taz(net, taz_file)

    for scenario in scenarios:
        if run_peaks:
            for period in periods:
                run_pipeline_for(
                    scenario, period, net, taz_file, edge_taz_map, residential_by_taz,
                    n_rounds=n_rounds, scouting_duration=scouting_duration,
                )

        if run_day:
            routes_day = run_macroscopic_assignment_day(
                scenario, taz_file, scale=day_scale
            )
            routes_day_final = extend_subset_of_trips(
                scenario, "DAY", routes_day, net, edge_taz_map, residential_by_taz
            )
            build_sumocfg_day(scenario, taz_file, routes_day_final, scale=day_scale)

    print(
        f"Pipeline completed — peaks: {run_peaks} | day scenario (scale={day_scale}): {run_day}"
    )


if __name__ == "__main__":
    # How to:
    # uv run python -m scripts.src.operations.assignment
    # uv run python -m scripts.src.operations.assignment --peaks-only
    # uv run python -m scripts.src.operations.assignment --day-only
    # uv run python -m scripts.src.operations.assignment --day-only --scale 0.8
    # uv run python -m scripts.src.operations.assignment --rounds 2 --scout-duration 21600

    args = sys.argv[1:]
    run_peaks = "--day-only" not in args
    run_day = "--peaks-only" not in args
    scale = DEFAULT_DAY_SCALE
    if "--scale" in args:
        scale = float(args[args.index("--scale") + 1])

    n_rounds = 3
    if "--rounds" in args:
        n_rounds = int(args[args.index("--rounds") + 1])

    scouting_duration = None
    if "--scout-duration" in args:
        scouting_duration = int(args[args.index("--scout-duration") + 1])

    scenarios = None
    if "--scenario" in args:
        scenarios = [args[args.index("--scenario") + 1]]
    periods = None
    if "--period" in args:
        periods = [args[args.index("--period") + 1]]
        

    main(
        run_peaks=run_peaks, run_day=run_day, day_scale=scale,
        n_rounds=n_rounds, scouting_duration=scouting_duration,
        scenarios=scenarios, periods=periods,
    )