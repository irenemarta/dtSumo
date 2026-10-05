"""
Rapelli et al. - TuST
4.1  Road Graph + TAZ:
-> parse_edges() + read_revisioned_TAZ(): to produce a unice taz file.

builds the TAZ file from the VISUM shapefile and derives the per-TAZ lookups 
(edge->TAZ, residential/service candidate edges) used by the traffic assignment and O'/D' extension steps.
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple
import sumolib
from colorama import Fore, init

import scripts.src.inputs.config as cfg
from scripts.src.helpers import parse_edges
from scripts.src.inputs.taz_from_OD import read_TAZ
from scripts.src.modules.entities import AssignmentContext, ResidentialCandidate

init(autoreset=True)

RESIDENTIAL_TYPES = {
    "highway.residential",
    #"highway.unclassified",
    "highway.service",
}

USE_PRIORITY_FALLBACK = True
RESIDENTIAL_PRIORITY_THRESHOLD = 4

def _net_zones() -> Path:
    # see tazOD.py
    edges = parse_edges(cfg.EDG_PARSE_XML)
    taz_file = read_TAZ(
        edges,
        cfg.ZONES,
        cfg.CONNECTORS,
        output_path=cfg.OUTPUT_DIR_ADD,
    )
    return taz_file


# STEP 4.3a — TAZ -> find residential/service edges ("controviali")
def _taz_edge_ids(taz_el: ET.Element) -> List[str]:
    e_ids = list(taz_el.get("edges", "").split())
    for t in taz_el:
        if t.tag in ("tazSource", "tazSink") and t.get("id"):
            e_ids.append(t.get("id"))
    seen_edges = set()
    output = []
    for eid in e_ids:
        if eid not in seen_edges:
            seen_edges.add(eid)
            output.append(eid)
    return output


def _is_residential(edge) -> bool:
    etype = edge.getType()
    if etype in RESIDENTIAL_TYPES:
        return True
    if not USE_PRIORITY_FALLBACK:
        return False
    is_artery = False
    for kw in ["primary", "secondary", "tertiary"]:
        if kw in etype.lower():
            is_artery = True
            break
    eprior_ok = edge.getPriority() <= RESIDENTIAL_PRIORITY_THRESHOLD and not is_artery
    return eprior_ok


def residential_edge_taz(
    net: "sumolib.net.Net", taz_file: Path
) -> Dict[str, List[ResidentialCandidate]]:
    all_tazs = ET.parse(taz_file).getroot().findall(".//taz")

    taz_edges: Dict[str, List[ResidentialCandidate]] = {}
    n_total_edges = n_matched_edges = n_missing_in_net = 0

    for taz in all_tazs:
        edge_ids = _taz_edge_ids(taz)
        n_total_edges += len(edge_ids)

        candidates = []
        for eid in edge_ids:
            if not net.hasEdge(eid):
                n_missing_in_net += 1
                continue
            edge = net.getEdge(eid)
            if _is_residential(edge):
                shape = edge.getShape()
                mid = shape[len(shape) // 2]
                candidates.append((eid, mid[0], mid[1]))

        taz_edges[taz.get("id")] = candidates
        n_matched_edges += len(candidates)

    n_taz_with_candidates = 0
    for v in taz_edges.values():
        if v:
            n_taz_with_candidates += 1

    print(Fore.LIGHTBLACK_EX + f"Found {len(all_tazs)} TAZs")
    print(Fore.LIGHTBLUE_EX + f"\ttotal edges in TAZs{n_total_edges} | not found edges: {n_missing_in_net}" 
        f"\t residential/service edges: {n_matched_edges} ")
    print(Fore.LIGHTBLUE_EX + f"TAZ having at least one candidate: {n_taz_with_candidates}")

    return taz_edges


def _edge_taz_map(taz_file: Path) -> Dict[str, str]:
    taz_tree = ET.parse(taz_file)
    mapping = {}
    for taz in taz_tree.getroot().findall(".//taz"):
        taz_id = taz.get("id")
        for eid in _taz_edge_ids(taz):
            mapping[eid] = taz_id
    return mapping


def build_context() -> AssignmentContext:
    taz_file = _net_zones()
    net = sumolib.net.readNet(str(cfg.NET_FILE))
    return AssignmentContext(
        net=net,
        taz_file=taz_file,
        edge_taz_map=_edge_taz_map(taz_file),
        residential_by_taz=residential_edge_taz(net, taz_file),
    )