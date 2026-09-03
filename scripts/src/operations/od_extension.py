"""
Rapelli et al. - TuST
4.3  Extension O'/D' using duarouter, applied ONCE after the feedback cycle
    has converged (not on every round, to keep the O'/D' random sampling
    from adding noise to the round-to-round comparison).
    
Selection of the residential terminal is weighted by geographic proximity
to the attach point (K-nearest within a max radius), instead of uniform
random choice over the whole TAZ candidate pool.
"""

import math
import random
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import sumolib
from colorama import Fore, init

import scripts.src.inputs.config as cfg
from scripts.src.operations.cmd import run_duarouter
from scripts.src.operations.taz_zones import AssignmentContext, ResidentialCandidate


init(autoreset=True)

FRACTION_TRIPS_TO_EXTEND = 1.0  # fraction to extend to O'/D'

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

    print(Fore.BLUE + f"\tBatch O'->O for {len(pending_prefix)} trips...")
    prefixes = _resolve_terminal_edges_batch(
        pending_prefix, residential_by_taz, "prefix", tmp_dir, scenario_tag,
        edge_use_count, net,
    )

    print(Fore.BLUE + f"\tBatch D->D' for {len(pending_suffix)} trips...")
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
        Fore.LIGHTBLACK_EX + 
        f" \t-> solved prefix: {len(prefixes)}/{len(pending_prefix)} | "
        f"solved suffix: {len(suffixes)}/{len(pending_suffix)} | "
        f"\nEXTENDED:"
        f"\tComplete{n_both} | O' only: {n_prefix_only} | D' only: {n_suffix_only} | total: {len(extended)}"
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
    ctx: AssignmentContext,
) -> Path:
    """
    Apply O'/D' extension to a fraction of the total number of routes (FRACTION_TRIPS_TO_EXTEND).
    Only performed over last run after cycle convergence.
    """
    net = ctx.net
    edge_taz_map = ctx.edge_taz_map
    residential_by_taz = ctx.residential_by_taz

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
        Fore.BLUE +
        f"[4.3] O'/D' Extension [{scenario}/{period}]: {n_to_extend}/{len(vehicles)} trips. "
        f"\nfranction = {FRACTION_TRIPS_TO_EXTEND:.0%} | "
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
    print(Fore.GREEN + f"\t{len(extended)} extended trips; output: {routes_final}")
    return routes_final