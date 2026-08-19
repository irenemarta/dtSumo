import xml.etree.ElementTree as ET
from pathlib import Path
import random
from colorama import init, Fore
from collections import defaultdict

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

def _count_edges(element: ET.Element) -> int | None:
    edges = _get_edges(element)
    return len(edges) if edges is not None else None


def filter_short_flows(input_xml: Path, output_new: Path, edge_taz_map: dict, min_edges: int = 2) -> Path:
    """
    Overwrites XML to eliminate really short trips that create artificial bottlenecks in the system.
    """
    import tqdm

    tree = ET.parse(input_xml)
    root = tree.getroot()

    short_ids = []
    total_cars_removed = 0

    for tag in ["vehicle", "flow"]:
        for el in root.findall(tag):
            n_edges = _count_edges(el)
            edges = _get_edges(el)
            
            from_taz_raw = edge_taz_map.get(edges[0])
            to_taz_raw = edge_taz_map.get(edges[-1])
            try:
                from_taz = int(from_taz_raw)
                to_taz = int(to_taz_raw)
            except ValueError as e:
                print("Value Error, from/to TAZ in wrong format")
                raise
            
            is_external = (
                from_taz >= 10000 or to_taz >= 10000
            )  # convention for external zones offsets

            if n_edges < min_edges and is_external:
                short_ids.append(el.get("id"))
                if tag == "flow":
                    total_cars_removed += int(el.attrib.get("number", 0))
                else:
                    total_cars_removed += 1

    # Remove useless routes
    removed_count = 0

    for tag in ["vehicle", "flow"]:
        elements = root.findall(tag)
        for el in tqdm.tqdm(elements, desc=f"Procesing {tag}s"):
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
        f"({total_cars_removed} vehicles) from {input_xml.name}"
    )

    return output_new


# to be used with _preprocess_multiple_ods
def filter_zero_flows(trips_xml: Path) -> Path:
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
    from tqdm import tqdm

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

    print(
        Fore.MAGENTA
        + f"ZERO-PROB: Removing {len(to_remove)} zero-probability vehicles/flows"
    )
    for v in tqdm(to_remove):
        root.remove(v)

    ET.indent(tree, space="    ")
    marouter_output.parent.mkdir(parents=True, exist_ok=True)
    tree.write(marouter_output, encoding="utf-8", xml_declaration=True)

    return marouter_output


def _spread_depart_trips(od2trips_file: Path, interval: float = 3600.0) -> Path:
    """
    To spread trips depart times over an interval
    to avoid that too many vehicle enter the map simultaneously.
    """
    
    tree = ET.parse(od2trips_file)
    root = tree.getroot()

    groups_interval = defaultdict(list)
    for trip in root.findall("trip"):
        depart = float(trip.get("depart"))
        interval_start = (depart // interval) * interval
        groups_interval[interval_start].append(trip)
    # Separate uniformly through an interval, basing on the number of vehicles
    for interval, group in groups_interval.items():
        step = interval / len(group)
        for idx_list, trip in enumerate(group):
            new_depart = interval_start + idx_list * step
            trip.set("depart", f"{new_depart:.2f}")

    try:
        trips_sorted = sorted(
            root.findall("trip"), key=lambda t: float(t.get("depart"))
        )
        for t in root.findall("trip"):
            root.remove(t)
        for t in trips_sorted:
            root.append(t)

        output_path = Path(od2trips_file.parent, "od_trips_spread.odtrips.xml")
        tree.write(output_path, encoding="utf-8", xml_declaration=True)
        print(Fore.MAGENTA + f"SPREAD: trips spread correctly.")
    except ValueError as e:
        print(Fore.RED + f"ERROR occued while spreading trips: {e}")
        raise

    return output_path
