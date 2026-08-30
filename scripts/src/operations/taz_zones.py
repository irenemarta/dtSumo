"""
Rapelli et al. - TuST
4.1  Road Graph + TAZ:
-> parse_edges() + read_revisioned_TAZ(): to produce a unice taz file.

builds the TAZ file from the VISUM shapefile
and derives the per-TAZ lookups (edge->TAZ, residential/service candidate
edges) used later by the traffic assignment and O'/D' extension steps.
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple
import sumolib
from colorama import Fore, init

import scripts.src.inputs.config as cfg
from scripts.src.helpers import parse_edges
from scripts.src.inputs.tazOD import read_revisioned_TAZ

init(autoreset=True)

RESIDENTIAL_TYPES = {
    "highway.residential",
    #"highway.unclassified",
    "highway.service",
}

USE_PRIORITY_FALLBACK = True
RESIDENTIAL_PRIORITY_THRESHOLD = 4

# Each candidate is stored as (edge_id, mid_x, mid_y) so the extension step
# can rank candidates by geographic proximity to the trip's attach point,
# instead of picking uniformly at random over the whole TAZ.
ResidentialCandidate = Tuple[str, float, float] 

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
        Fore.LIGHTBLACK_EX +
        f"Found {len(all_tazs)} TAZs: total edges in TAZs{n_total_edges} | not found edges: {n_missing_in_net}" 
        f"\t residential/service edges: {n_matched_edges} "
        f"\tTAZ having at least one candidate: {n_taz_with_candidates}"
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


@dataclass
class AssignmentContext:
    """
    Bundles the network artifacts that are computed once per pipeline run
    (main()) and then reused, unchanged, across every (scenario, period)
    combination: the loaded SUMO net, the TAZ file, the edge->TAZ lookup
    and the per-TAZ residential/service candidate edges.
    """
    net: "sumolib.net.Net"
    taz_file: Path
    edge_taz_map: Dict[str, str]
    residential_by_taz: Dict[str, List[ResidentialCandidate]]

    @classmethod
    def build(cls) -> "AssignmentContext":
        taz_file = build_network_zones()
        net = sumolib.net.readNet(str(cfg.NET_FILE))
        edge_taz_map = build_edge_to_taz(taz_file)
        residential_by_taz = build_residential_edges_by_taz(net, taz_file)
        return cls(
            net=net,
            taz_file=taz_file,
            edge_taz_map=edge_taz_map,
            residential_by_taz=residential_by_taz,
        )