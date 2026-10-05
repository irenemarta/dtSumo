import xml.etree.ElementTree as ET
from pathlib import Path
from tqdm import tqdm
from colorama import init, Fore

init(autoreset=True)

#### FILTERING
def _get_edges(element: ET.Element) -> list[str] | None:
    route = element.find("route")
    if route is not None:
        return route.attrib.get("edges", "").split()
    rd = element.find("routeDistribution")
    if rd is not None:
        routes = rd.findall("route")
        if routes:
            best = max(routes, key=lambda r: float(r.attrib.get("probability", 0)))
            return best.attrib.get("edges", "").split()
    return None

def _count_edges(element: ET.Element) -> int:
    edges = _get_edges(element)
    if edges:
        return len(edges)
    else:
        return 0

def filter_short_flows(input_xml: Path, output_new: Path, edge_taz_map: dict, min_edges: int = 2) -> Path:
    """
    Overwrites XML to eliminate trips which run over a single edge and create artificial bottlenecks in the system.
    """
    tree = ET.parse(input_xml)
    root = tree.getroot()
    short_ids = []
    total_veh_removed = 0

    for tag in ["vehicle", "flow"]:
        for el in root.findall(tag):
            n_edges = _count_edges(el)
            edges = _get_edges(el)
            if not edges:
                continue
            from_taz = int(edge_taz_map.get(edges[0]))
            to_taz = int(edge_taz_map.get(edges[-1]))
            is_external = (
                from_taz >= 10000 or to_taz >= 10000
            )  # convention for external zones offsets

            if n_edges < min_edges and is_external:
                short_ids.append(el.get("id"))
                if tag == "flow":
                    n_veh_removed = int(el.attrib.get("number", 0))
                    total_veh_removed += n_veh_removed
                else:
                    total_veh_removed += 1

    removed_count = 0
    for tag in ["vehicle", "flow"]:
        elements = root.findall(tag)
        for el in tqdm(elements, desc=f"Procesing {tag}s"):
            if el.get("id") in short_ids:
                root.remove(el)
                removed_count += 1
    if removed_count == 0:
        print(Fore.MAGENTA + f"ROUTE CLEANING: no short flows found or removed.")

    ET.indent(tree, space="    ")
    output_new.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_new, encoding="utf-8", xml_declaration=True)
    print(
        Fore.MAGENTA + f"ROUTE CLEANING: removed {len(short_ids)} flows "
        f"({total_veh_removed} vehicles) from {input_xml.name}"
    )

    return output_new

def filter_zero_flows(trips_xml: Path) -> Path: # to be used with _preprocess_multiple_ods
    tree = ET.parse(trips_xml)
    root = tree.getroot()

    removed = 0
    for flow in root.findall(".//flow"):
        number = int(flow.get("number", 0))
        if number == 0:
            root.remove(flow)
            removed += 1
    print(Fore.MAGENTA + f"Removed {removed} zero-flow entries")
    tree.write(trips_xml, encoding="utf-8", xml_declaration=True)

    return trips_xml


def filter_zero_prob(marouter_output: Path) -> Path:
    """Post-processing of routes to prevent SUE crash running marouter"""

    tree = ET.parse(marouter_output)
    root = tree.getroot()
    to_remove = []
    
    for tag in ["vehicle", "flow"]:
        for vehicle in root.findall(tag):
            rd = vehicle.find("routeDistribution")
            if rd is None:
                continue
            routes = rd.findall("route")
            if not routes:
                to_remove.append(vehicle)
                continue
            # unique route with 0 prob or total route prob is 0
            tot_prob = sum(float(r.get("probability", 0)) for r in routes)
            if tot_prob == 0.0:
                to_remove.append(vehicle)
    print(Fore.MAGENTA + f"ZERO-PROB: Removing {len(to_remove)} zero-probability vehicles/flows")
    
    for v in tqdm(to_remove):
        root.remove(v)
    ET.indent(tree, space="    ")
    marouter_output.parent.mkdir(parents=True, exist_ok=True)
    tree.write(marouter_output, encoding="utf-8", xml_declaration=True)

    return marouter_output