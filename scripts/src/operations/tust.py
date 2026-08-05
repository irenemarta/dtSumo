"""
TuST section 4 routing logic applied by this script:

4.1  Road Graph + TAZ:
-> parse_edges() + read_revisioned_TAZ(): to produce a unice taz file

4.2  Traffic Assignment macroscopico
-> run_marouter(), one run for each combination of (scenario, period) or just one run for the entire day
-> filter_short_flows() removes trips that are not long enough

File created for each (scenario, period) tuple, in order:
marouter_output.rou.xml -> marouter_output_clean.rou.xml -> marouter_output_extended.rou.xml

4.3  Extension O'/D' using duarouter
-> run_duarouter() (in batch) to cover O'->O e D->D', over residential edges
   Selection of the residential terminal is now weighted by geographic
   proximity to the attach point (K-nearest within a max radius), instead
   of uniform random choice over the whole TAZ candidate pool. This avoids
   extending trips to residential edges that are geometrically far from
   where the trip actually attaches to the macroscopic network, which was
   producing source/sink points scattered across the whole study zone
   regardless of the trip's real attach point.
"""

import math
import random
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
    "highway.unclassified",
    "highway.service",
}

USE_PRIORITY_FALLBACK = True
RESIDENTIAL_PRIORITY_THRESHOLD = 4

TLS_PENALTY_BY_VARIANT = {
    "no_TLS": 0.0,
    "with_TLS": 5.0,
}

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


# STEP 4.2 — Traffic Assignment with marouter (AM / PM)


def _tls_variant_of(scenario: str) -> str:
    # "MA_no_TLS" -> "no_TLS" ; "MA_with_TLS" -> "with_TLS"
    return scenario.replace("MA_", "", 1)


def run_macroscopic_assignment(scenario: str, period: str, taz_file: Path) -> Path:
    """
    MAROUTER on the period matrix, for the due TLS scenario
    """
    work_dir = cfg.WORKDIRS[scenario][period]
    work_dir.mkdir(parents=True, exist_ok=True)
    trips_output = work_dir / "od_trips.odtrips.xml"

    tls_variant = _tls_variant_of(scenario)
    tls_penalty = TLS_PENALTY_BY_VARIANT[tls_variant]

    detectors_file = cfg.DETECTORS[scenario][period]

    print(
        f"[4.2] MAROUTER — scenario={scenario} period={period}, (tls_penalty={tls_penalty})..."
    )
    METHOD="incremental"
    routes_macro = run_marouter(
        net_file=cfg.NET_FILE,
        od_matrices=cfg.OD_MATRICES[period],
        taz_file=taz_file,
        out_dir=work_dir,
        trips_output=trips_output,
        additional_files=[detectors_file, cfg.VTYPE],
        netload_output=Path(work_dir, "netload_ouput.xml"),
        method=METHOD,
        route_choice="logit",
        logit_theta=0.3,
        logit_beta=0.2,
        paths=15,
        path_penalty=10.0,
        weights_priority=0.0,
        max_alternatives=10,
        max_iterations=300,
        max_inner_iterations=20,
        #tolerance=0.5,
        weights_tls=tls_penalty,
        begin=cfg.PERIODS[period]["start"],
        end=cfg.PERIODS[period]["end"],
        extra_args=[
            "-l", str(work_dir / f"marouter_{METHOD}.log"), 
            #"-v", "true"
                    ],
        weight_files=f"/home/marta/tesi-5t/DTBaldissera/vm-file/project/scripts/output/marouter-workdir/{scenario}_{period}/edge_output.xml",
    )

    print(f"\t Macroscopic assignment saved here: {routes_macro}")

    routes_clean = filter_short_flows(
        routes_macro,
        output_new=work_dir / "marouter_output_clean.rou.xml",
        min_edges=2,
    )

    n_before_filter = len(ET.parse(routes_clean).getroot().findall("vehicle")) + len(
        ET.parse(routes_clean).getroot().findall("flow")
    )

    routes_filtered = filter_zero_prob(routes_clean)

    n_after_filter = len(ET.parse(routes_filtered).getroot().findall("vehicle")) + len(
        ET.parse(routes_filtered).getroot().findall("flow")
    )

    print(f"\t\troute cleaned (short-flows + zero-prob): {routes_filtered}")
    if n_before_filter and n_after_filter == 0:
        raise RuntimeError(
            "filter_zero_prob removed all vehicles: check marouter output"
        )

    return routes_filtered


def build_sumocfg(
    scenario: str, period: str, taz_file: Path, routes_final: Path
) -> None:
    """
    Costruisce il .sumocfg PER AM/PM, puntando esplicitamente al file di
    route DOPO l'estensione O'/D' (routes_final = marouter_output_extended
    .rou.xml). Va chiamata dopo extend_subset_of_trips, non prima: se
    chiamata sul file pre-estensione (come accadeva in precedenza, quando
    questa build era dentro run_macroscopic_assignment) la simulazione
    finisce per usare le route SENZA l'estensione locale, vanificando
    silenziosamente tutto il lavoro fatto in step 4.3.
    """
    detectors_file = cfg.DETECTORS[scenario][period]
    CfgAttributes(
        net=cfg.NET_FILE,
        routes=routes_final,
        output_cfg=cfg.CFG_DIR,
        output_sumo=cfg.SIM_OUT[scenario][period],
        config_name=f"francia_peschiera_MAROUTER_{scenario[3:]}_{period}.sumocfg",
        meso=False,
        setting=cfg.VIEW
    ).build(
        method="marouter",
        taz=taz_file,
        begin=28800 if period == "AM" else 64800,
        end=36000 if period == "AM" else 72000,
        detectors=detectors_file,
        edgedata=Path(f"/home/marta/tesi-5t/DTBaldissera/vm-file/project/scripts/output/marouter-workdir/{scenario}_{period}/edgeData.xml")
    )
    print(f"\t\t.sumocfg [{scenario}/{period}] scritto puntando a: {routes_final}")


# STEP 4.2-DAY — Traffic Assignment for the entire day (24 matrici orarie)


def run_macroscopic_assignment_day(
    scenario: str, taz_file: Path, scale: float = DEFAULT_DAY_SCALE
) -> Path:
    """
    Executes marouter for the entire day, only once (begin=0, end=86400),
    using the folder of hour matrices as input (h00.mtx..h23.mtx)
    """

    from scripts.allDayOD import generate_hour_matrices

    generate_hour_matrices(
        cfg.OD_MATRICES["AM"],
        cfg.OD_MATRICES["PM"],
        cfg.OD_MATRICES["DAY"] / f"scaled_{scale}",
        scale,
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
        od_matrices=day_matrices,  # list of matrix files
        taz_file=taz_file,
        out_dir=work_dir,
        trips_output=trips_output,
        additional_files=[detectors_file, cfg.VTYPE],
        netload_output=work_dir / "netload_ouput.xml",
        method="incremental",
        route_choice="logit",
        logit_theta=0.3,
        logit_beta=0.2,
        paths=15,
        path_penalty=10.0,
        weights_priority=0.0,
        max_alternatives=10,
        max_iterations=300,
        tolerance=0.5,
        weights_tls=tls_penalty,
        begin=0,
        end=24 * 3600,
    )
    print(f"\t\tDay macroscopic assignment run in {routes_macro}")

    routes_clean = filter_short_flows(
        routes_macro,
        output_new=work_dir / "marouter_output_clean.rou.xml",
        min_edges=2,
    )
    routes_filtered = filter_zero_prob(routes_clean)

    return routes_filtered


def build_sumocfg_day(
    scenario: str, taz_file: Path, routes_final: Path, scale: float = DEFAULT_DAY_SCALE
) -> None:
    """
    Equivalente di build_sumocfg() ma per lo scenario DAY: va chiamata dopo
    extend_subset_of_trips(..., "DAY", ...), puntando al file esteso
    invece che a routes_filtered pre-estensione.
    """
    detectors_file = cfg.DETECTORS[scenario]["DAY"]
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

MAX_EXTENSION_ROUNDS = (
    10  # round di retry con candidati diversi per i viaggi ancora irrisolti
)
MAX_USES_PER_EDGE = 15  # cap sul SOLO terminale scelto (non sugli edge di passaggio):
# un cap troppo stretto fa fallire più viaggi (candidati esauriti),
# che tornano sulla route originale (arterie principali).
# STORIA DEL TUNING: valore iniziale 30. Con K_NEAREST_CANDIDATES alzato
# da 12 a 20 la copertura dell'estensione non è migliorata in modo
# apprezzabile (prefix 13229->13249, suffix 9273->9357 su ~24800 viaggi
# selezionabili), segno che per i cluster di attach point più densi non
# esistono altri edge residenziali entro MAX_EXTENSION_RADIUS_M oltre a
# quelli già raggiunti con K=12: il vincolo reale è la scarsità locale di
# edge, non la dimensione del pool. Alzato quindi il cap a 50 per
# permettere agli stessi edge vicini di assorbire più viaggi ciascuno,
# unica leva rimasta per recuperare copertura senza allontanare
# geograficamente le estensioni. Da validare: se la % di edge saturi resta
# comunque alta e la copertura non risale a sufficienza, il limite è
# probabilmente strutturale (rete residenziale reale troppo rada in quelle
# zone) più che di tuning — vedi diagnose_extension_distances.py.

MAX_EXTENSION_RADIUS_M = 500.0  # raggio massimo (metri, coordinate di rete) dal
# punto di aggancio entro cui un edge residenziale è considerato un candidato
# valido per l'estensione O'/D'. Evita estensioni geograficamente insensate
# su TAZ molto estese (es. 604-608, 635), dove il pool di candidati può
# coprire chilometri di rete. Zona di studio ~1 km di raggio: 500 m tiene
# l'estensione genuinamente locale senza far crollare troppo la quota di
# viaggi risolti. Se dopo il primo run la dispersione residua nelle mappe
# source/sink resta eccessiva, stringere a 400; se invece troppi viaggi
# restano non estesi (pending esauriti), allargare leggermente o alzare
# K_NEAREST_CANDIDATES.
K_NEAREST_CANDIDATES = 20  # tra i candidati entro il raggio, quanti dei più
# vicini formano il pool su cui pescare random.choice. Un pool piccolo ma
# non singleton evita sia la dispersione geografica (troppi candidati)
# sia la saturazione immediata di un unico edge (troppo pochi candidati).
# NOTA: con MAX_USES_PER_EDGE=30, la capacità nominale di un cluster locale
# è K_NEAREST_CANDIDATES x MAX_USES_PER_EDGE. Con K=12 (valore iniziale) e
# il cap ora correttamente rispettato (fix su edge_use_count riservato
# subito, non a fine round), la capacità si è rivelata insufficiente nelle
# zone a domanda densa: la quota di viaggi estesi è crollata dal 78%/66%
# (prefix/suffix, con cap non rispettato) al 53%/37% (cap rispettato, K=12).
# K=20 alza la capacità nominale a 20x30=600 slot per cluster locale,
# restando comunque entro il raggio di 500 m (le distanze osservate anche
# ai percentili alti, p90~400-430m con K=12, lasciano margine).


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
    Ritorna fino a k edge_id, ordinati per distanza crescente da ref_point,
    tra i candidati non ancora tentati, diversi dall'attach_edge, entro
    MAX_EXTENSION_RADIUS_M e non ancora saturi (MAX_USES_PER_EDGE).
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
        # vid -> candidato riservato in QUESTO round, per poter liberare la
        # riserva se duarouter non risolve quel viaggio (edge non davvero
        # usato) e per sapere quale terminale confermare se invece risolve.
        reserved_this_round: Dict[str, str] = {}

        for vid, (taz_id, attach_edge) in still_pending.items():
            edge_obj = net.getEdge(attach_edge)
            # prefix: il punto di riferimento è l'inizio dell'attach_edge
            # (dove il viaggio "entra" nella rete macroscopica);
            # suffix: la fine (dove il viaggio "esce").
            ref_node = (
                edge_obj.getFromNode() if direction == "prefix" else edge_obj.getToNode()
            )
            ref_point = ref_node.getCoord()

            # edge_use_count viene letto e aggiornato QUI, dentro il loop
            # sui viaggi dello stesso round (non a fine round): così se due
            # viaggi con attach point vicini condividono lo stesso pool
            # ristretto di K_NEAREST_CANDIDATES, il secondo vede già la
            # riserva del primo e il cap è rispettato anche intra-round.
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
                # riserva confermata, nessun aggiustamento necessario
            else:
                # duarouter non ha risolto: l'edge non è stato davvero
                # usato, libera la riserva così altri viaggi (o questo
                # stesso viaggio in un round successivo) possano usarlo
                edge_use_count[candidate] = max(0, edge_use_count.get(candidate, 1) - 1)

    return resolved


# STEP 4.3c — Extension of all selected batches


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
            # solo origine estesa, destinazione resta quella originale
            extended[vid] = prefixes[vid][:-1] + info["edges"]
            n_prefix_only += 1
        elif has_suffix:
            # solo destinazione estesa, origine resta quella originale
            extended[vid] = info["edges"] + suffixes[vid][1:]
            n_suffix_only += 1

    print(f"      -> solved prefix: {len(prefixes)}/{len(pending_prefix)} | "
        f"solved suffix: {len(suffixes)}/{len(pending_suffix)} | "
        f"estesi completi: {n_both} | solo O': {n_prefix_only} | solo D': {n_suffix_only} | "
        f"totale estesi: {len(extended)}")

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
    Applica l'estensione O'/D' a una frazione dei viaggi, indipendentemente
    dal periodo: funziona identica per AM, PM e DAY, perché opera solo sui
    tag <vehicle>/<trip> del file routes_macro, senza assumere nulla su
    come è stato generato (singola matrice oraria o giornata intera).
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

        taz_o = veh.get("fromTaz") or edge_taz_map.get(original_edges[0])
        taz_d = veh.get("toTaz") or edge_taz_map.get(original_edges[-1])
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
):
    routes_macro = run_macroscopic_assignment(scenario, period, taz_file)
    routes_final = extend_subset_of_trips(
        scenario, period, routes_macro, net, edge_taz_map, residential_by_taz
    )
    build_sumocfg(scenario, period, taz_file, routes_final)


def main(
    run_peaks: bool = True, run_day: bool = True, day_scale: float = DEFAULT_DAY_SCALE
):
    random.seed(42)

    taz_file = build_network_zones()
    net = sumolib.net.readNet(str(cfg.NET_FILE))
    edge_taz_map = build_edge_to_taz(taz_file)
    residential_by_taz = build_residential_edges_by_taz(net, taz_file)

    for scenario in SCENARIOS_MA:
        if run_peaks:
            for period in PERIODS_TO_RUN:
                run_pipeline_for(
                    scenario, period, net, taz_file, edge_taz_map, residential_by_taz
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
    # uv run python -m scripts.src.operations.tust
    # uv run python -m scripts.src.operations.tust --peaks-only
    # uv run python -m scripts.src.operations.tust --day-only
    # uv run python -m scripts.src.operations.tust --day-only --scale 0.8

    args = sys.argv[1:]
    run_peaks = "--day-only" not in args
    run_day = "--peaks-only" not in args
    scale = DEFAULT_DAY_SCALE
    if "--scale" in args:
        scale = float(args[args.index("--scale") + 1])

    main(run_peaks=run_peaks, run_day=run_day, day_scale=scale)