"""
Network building functions for what-if scenarios. 
Closed lanes keep existing and only get disallow="all": same edges, lanes and detectors, connections and traffic lights untouched,
so that one can avoid passing the net through netconvert/netedit for every scenario.
"""

import sumolib, math
import xml.etree.ElementTree as ET
from pathlib import Path
from colorama import init, Fore
from typing import Dict, Optional, Set
from scripts.src.whatif.scenario import ScenarioError, which_lanes_to_close

init(autoreset=True)


def build_scenario_net(base_net: Path, net: "sumolib.net.Net", lanes_closed: Dict[str, int],
                    out_dir: Path) -> Path:
    """scenario.net.xml = base_net with disallow="all" on the closed lanes (lanes_closed: {edge: n lanes})."""
    closed_lanes = set()
    for eid, k in lanes_closed.items():
        # leftmost lanes first, skipping the last lane of a turn (see scenario.lanes_for_closure)
        for lane in which_lanes_to_close(net.getEdge(eid), k):
            closed_lanes.add(lane.getID())
    closed_lanes = set()
    connections = []
    for eid, k in lanes_closed.items():
        lanes = net.getEdge(eid).getLanes()
        for lane in lanes[len(lanes) - k:]:  # highest indices, lane 0 is the rightmost (source: https://sumo.dlr.de/docs/Networks/SUMO_Road_Networks.html#lanes)
            closed_lanes.add(lane.getID())

    tree = ET.parse(base_net)
    root = tree.getroot()
    # also close the internal lanes of the movements from or to a closed lane.
    for conn_el in root.iter("connection"):
        if conn_el.get("via"):
            connections.append(conn_el)
    # a left turn can go through two internal lanes (internal junction)
    # from lane = lane index -> from + fromLane = edgeid_laneid
    for _ in range(2):
        for conn in connections:
            from_lane = f"{conn.get('from')}_{conn.get('fromLane')}"
            to_lane = f"{conn.get('to')}_{conn.get('toLane')}"
            if from_lane in closed_lanes or to_lane in closed_lanes:
                closed_lanes.add(conn.get("via"))

    for lane in root.iter("lane"):
        if lane.get("id") in closed_lanes:
            lane.attrib.pop("allow", None)
            lane.set("disallow", "all")
    scenario_net = out_dir / "scenario.net.xml"
    tree.write(scenario_net, encoding="UTF-8", xml_declaration=True)
    return scenario_net


def scenario_taz(taz_file: Path, closed_edges: Set[str], out_path: Optional[Path] = None) -> Optional[Path]:
    """
    TAZ without the fully closed edges.
    Without out_path it only checks that every zone keeps an origin and a destination.
    """
    tree = ET.parse(taz_file)
    lost_endpoints = []
    for t_zone in tree.getroot().iter("taz"):
        list_edges = t_zone.get("edges", "").split()
        if list_edges:
            t_zone.set("edges", " ".join(e for e in list_edges if e not in closed_edges))
        for tag in ("tazSource", "tazSink"):
            src_snk = t_zone.findall(tag)
            kept = [el for el in src_snk if el.get("id") not in closed_edges]
            if src_snk and not kept:
                label = 'origins' if tag == 'tazSource' else 'destinations'
                lost_endpoints.append(f"{t_zone.get('id')} ({label})")
            for el in src_snk:
                if el not in kept:
                    t_zone.remove(el)
    if lost_endpoints:
        raise ScenarioError(Fore.YELLOW +
            "closing these edges leaves zones without access, their demand could not be served: "
            + ", ".join(lost_endpoints) + ". Keep at least one access edge open or use 'partial_closure'.")
    if out_path:
        tree.write(out_path, encoding="UTF-8", xml_declaration=True)
    return out_path
