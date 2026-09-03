"""
@file       helpers.py
@author     Irene Marta
@date       2026

A compilation of helper functions that are frequently used in this project.
"""


import os
from typing import Dict, List
from pathlib import Path

import pandas as pd
import xml.etree.ElementTree as ET

from geopy.distance import distance
from geopy.geocoders import Nominatim
from geopy.point import Point


def bbox():
    """
    Computes the coordinates in lat/long format at a due distance from to a specific point od interest.
    It leverages geopy for geolocation and coordinate coding.
    Input: radius (in meters) 
    Returns bounding box coordinates.

    The coordinates will be given as input to JOSM in a second step of the analysis.
    """
    # Find geo-coordinates
    geolocator = Nominatim(user_agent="sumo_map_extractor")
    location = geolocator.geocode(
        "Corso Francia, Parella, Circoscrizione 4, Torino, Piemonte, 10146, Italia"
    )

    # Define map radius
    radius = 1000

    print(f"\nCenter coordinates are: {location.latitude}, {location.longitude}")
    center = Point(location.latitude, location.longitude)

    # Calculate cardinal points at radius-distance from the centre - max distance from the centre in the bbox
    north = distance(meters=radius).destination(center, bearing=0)
    south = distance(meters=radius).destination(center, bearing=180)
    east = distance(meters=radius).destination(center, bearing=90)
    west = distance(meters=radius).destination(center, bearing=270)

    # Bounding-box coordinates
    lat_min, lat_max = south.latitude, north.latitude
    lon_min, lon_max = west.longitude, east.longitude

    print(f"Bounding box: {lat_min}, {lon_min}, {lat_max}, {lon_max}\n")

    return lat_min, lon_min, lat_max, lon_max


def _filter_by_bbox(df):
    """Mask a df to extract data related to a restricted bbox."""
    lat1, lon1, lat2, lon2 = bbox()
    mask = (
        df["From Latitude"].between(lat1, lat2, inclusive="both")
        & df["From Longitude"].between(lon1, lon2, inclusive="both")
    ) | (
        df["To Latitude"].between(lat1, lat2, inclusive="both")
        & df["To Longitude"].between(lon1, lon2, inclusive="both")
    )
    filtered_db = df[mask]
    print(f"Lines in the filtered CSV: {len(filtered_db)}")
    return filtered_db

# Function to save the xml files in the output directory
def format_xml(xml, out_dir):
    os.makedirs(out_dir, exist_ok=True)

    for filename, root in xml.items():
        tree = ET.ElementTree(root)
        ET.indent(tree, space="  ", level=0)
        tree.write(
            os.path.join(out_dir, filename), encoding="utf-8", xml_declaration=True
        )
        print(f"{filename} successfully downloaded in {out_dir}")


def parse_nodes(nod_file: Path):
    tree = ET.parse(nod_file)
    root = tree.getroot()
    coord_to_node = {}
    for node in root.findall("node"):
        node_id = node.attrib.get("id")
        try:
            lat = float(node.attrib.get("y", ""))
            lon = float(node.attrib.get("x", ""))
        except ValueError:
            continue
        node_type = node.attrib.get("type", "")
        # {(lat, lon) : {"id":.., "type":..}}
        coord_to_node[(lat, lon)] = {"id": node_id, "type": node_type}
    print(f"Nodes in .nod.xml: {len(coord_to_node)}")
    return coord_to_node


def parse_edges(edg_file: Path) -> Dict:
    tree = ET.parse(edg_file)
    root = tree.getroot()
    edges = {}
    highway_service_count = 0

    for edge in root.findall("edge"):
        # Ignore internal edges
        if edge.attrib.get("function") == "internal":
            continue

        eid = edge.attrib.get("id", "")
        from_id = edge.attrib.get("from")
        to_id = edge.attrib.get("to")
        type_edge = edge.attrib.get("type", "")

        if from_id is None or to_id is None:
            continue
        if type_edge == "highway.service":
            highway_service_count += 1

        # Collect data for each edge
        edge_data = {
            "id": eid,
            "from": from_id,
            "to": to_id,
            "type": type_edge,
            "priority": edge.attrib.get("priority", ""),
            "name": edge.attrib.get("name", ""),
            "numLanes": edge.attrib.get("numLanes", ""),
            "speed": edge.attrib.get("speed", ""),
            "length": edge.attrib.get("length", ""),
            "shape": None,
        }

        # Collect lane data for each edge
        lanes = edge.findall("lane")
        if lanes:
            lane_speeds = []
            lane_lengths = []
            for lane in lanes:
                s = lane.attrib.get("speed")
                l = lane.attrib.get("length")
                """Devo usare la shape delle lane ognuna rappresenta un record diverso nel db"""
                if s:
                    try:
                        lane_speeds.append(float(s))
                    except ValueError:
                        pass
                if l:
                    try:
                        lane_lengths.append(float(l))
                    except ValueError:
                        pass

                # Add the obtained shape
                shape_str = lane.attrib.get("shape")
                if shape_str:
                    """NOTA: serve conversione da "lon1,lat1 lon2,lat2 ..." a [(lon1,lat1), (lon2,lat2), ...]"""
                    try:
                        coords = []
                        for point in shape_str.strip().split():
                            # The map() function executes a specified function for each item in an iterable.
                            #   The item is sent to the function as a parameter.
                            # SYNTAX: map(function, iterables)
                            lon, lat = map(float, point.split(sep=","))
                            coords.append((lon, lat))
                        edge_data["shape"] = coords
                    except Exception as e:
                        print(f"Parsing error for edge {eid}: {e}")

            # If the edge has no defined speed, get the average velocity on all its lanes
            if not edge_data["speed"] and lane_speeds:
                edge_data["speed"] = str(sum(lane_speeds) / len(lane_speeds))
            # Same goes for lengths
            if not edge_data["length"] and lane_lengths:
                edge_data["length"] = str(sum(lane_lengths) / len(lane_lengths))

        # key = id, value = data
        edges[eid] = edge_data

    print(f"Edges in .edg.xml: {len(edges)}")
    print(f"Type service edges: {highway_service_count} over {len(edges)}")
    return edges


def parse_tll(xml_file: Path):
    tree = ET.parse(xml_file)
    root = tree.getroot()
    
    tlLogic = {}

    for tl in root.findall("tlLogic"):
        tl_id = tl.attrib.get("id")
        tll_data = {
            "id": tl_id,
            "programID": tl.attrib.get("programID", ""),
            "phases": [],
            "connections": {} 
        }
        for p in tl.findall('phase'):
            tll_data["phases"].append({
                "duration": p.attrib.get('duration', ''),
                "state": p.attrib.get('state', '')
            })
        
        tlLogic[tl_id] = tll_data

    for conn in root.findall("connection"):
        tl_id = conn.attrib.get("tl")
        # For connection managed by traffic ligth logic
        if tl_id and tl_id in tlLogic:
            link_idx = conn.attrib.get("linkIndex")

            conn_info = {
                "from": conn.attrib.get("from"),
                "to": conn.attrib.get("to"),
                "fromLane": conn.attrib.get("fromLane"),
                "toLane": conn.attrib.get("toLane"),
                "linkIndex": int(link_idx)
            }
            
            # NB: one linkIndex can manage more than one connection
            if link_idx not in tlLogic[tl_id]["connections"]:
                tlLogic[tl_id]["connections"][link_idx] = []
            
            tlLogic[tl_id]["connections"][link_idx].append(conn_info)

    return tlLogic


def _get_opposite_direction(edge_id, edge_data, edges):
    """ Extract opposite direction for a two-way type edge. """
    from_node = edge_data.get("from")
    to_node = edge_data.get("to")

    # First logic: check for "-"
    if (
        edge_id[0] == "-"
    ):
        opposite_id = edge_id.lstrip("-")
        if opposite_id in edges:
            return opposite_id

    # Second logic: from/to swap
    if from_node and to_node:
        for other_edge_id, other_edge_data in edges.items():
            # Skip current edge
            if other_edge_id == edge_id:
                continue

            other_from = other_edge_data.get("from")
            other_to = other_edge_data.get("to")

            if other_from == to_node and other_to == from_node:
                return other_edge_id

    return None  # oneway cases



# ODS
def _process_multiple_ods(od_path: Path | list | str, out_path: Path, min_flow: float = 0.5) -> Dict[Path, pd.DataFrame]:
    ods_cleaned = []
    # paths = [od_path] if isinstance(od_path, Path) else od_path
    if isinstance(od_path, str):
        paths = [Path(p.strip()) for p in od_path.split(",")]
    elif isinstance(od_path, Path) and od_path.is_dir():
        paths = sorted(od_path.glob("*.mtx"))
    elif isinstance(od_path, Path):
        paths = [od_path]
    else:
        paths = [Path(p) for p in od_path]
    
    for p in paths:
        flow_cleaned = []
        with open(p, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            
        header = []
        data = []
        for l in lines:
            if l in lines[0:7]:
                header.append(l)
            else:
                data.append(l)
        for line in data:
            stripped = line.strip()
            if not stripped: # only whitespaces lines
                continue
            info = line.strip().split()
            if len(info) < 3:
                print(f"[WARNING] wrong line on file {p.name}: '{stripped}'")
                continue
            # print(len(info))
            flow_str = info[2].lower()
            if "nan" in flow_str or info[0].lower() == "nan" or info[1].lower() == "nan":
                print(f"[ERROR] 'NaN' value in file file {p.name} at line: '{stripped}'")
                flow = min_flow # force to min_flow to prevent errors
            else:
                flow = float(info[2])
            
            if flow < 0:
                continue
            if flow < min_flow:
                continue
            
            flow = max(flow, min_flow)
            flow_cleaned.append(f"{info[0]}\t{info[1]}\t{flow:.2f}\n")

        final_file_path = out_path / p.name
        os.makedirs(out_path, exist_ok=True)
        
        with open(final_file_path, "w") as new:
            new.writelines(header)
            new.writelines(flow_cleaned)
        ods_cleaned.append(final_file_path)

    return ods_cleaned


def _read_multiple_ods(od_path: Path | list) -> Dict[Path, pd.DataFrame]:
    od_flows = {}

    paths = [od_path] if isinstance(od_path, Path) else od_path

    for p in paths:
        df = pd.read_csv(
            p,
            sep="\s+",
            comment="*",
            names=["From", "To", "Flow"],
            skiprows=[0, 7],
            engine="python",
        )
        df_clean = df.drop(df.index[:2]).reset_index(drop=True)

        od_flows[p] = df_clean

    return od_flows

def write_taz_relations(path_od: Path | List[Path], output_add: Path, begin: int= 0, end: int = 86400):
    od_flows = _read_multiple_ods(path_od)
    # od-flows = {path_od : df}, df.columns = [From, To, Flow]

    taz_relations = os.path.join(output_add, "francia_peschiera.TAZREL.add.xml")
    root = ET.Element("additional")
    interval = ET.SubElement(root, "interval", {
        "begin": str(begin),
        "end": str(end)
    })
    
    for df in od_flows.values():
        for _, row in df.iterrows():
            ET.SubElement(interval, "tazRelation", {
                "from": str(int(row['From'])),
                "to": str(int(row['To'])),
                "count": str(round(row['Flow'], 2))
            })

    tree = ET.ElementTree(root)
    ET.indent(tree, space="    ")
    tree.write(taz_relations, encoding="utf-8", xml_declaration=True)

    return taz_relations


def write_taz_relations_24h(
    dict_od_paths: Dict[int, Path],
    output_path: Path,
    partitions: int = 4,
):
    output_path.mkdir(parents=True, exist_ok=True)
    taz_relations_file = output_path / "full_day.TAZREL.add.xml"
    
    hour_flows = {
        hour: next(iter(_read_multiple_ods(path).values()))
        for hour, path in dict_od_paths.items()
    }
    root = ET.Element("data")

    for interval_idx in range(24 * partitions):
        hour_now = interval_idx // partitions
        quarter_idx = interval_idx % partitions

        if hour_now not in hour_flows:
            continue

        begin = interval_idx * 3600 / partitions
        end = (interval_idx + 1) * 3600 / partitions
        interval_el = ET.SubElement(root, "interval", begin=str(begin), end=str(end))

        df_now = hour_flows[hour_now]
        df_next = hour_flows.get(hour_now + 1, df_now)

        merged = df_now.merge(
            df_next[["From", "To", "Flow"]],
            on=["From", "To"],
            how="left",
            suffixes=("", "_next"),
        )
        merged["Flow_next"] = merged["Flow_next"].fillna(merged["Flow"])

        alpha = (quarter_idx + 0.5) / 4.0

        for _, row in merged.iterrows():
            vehicles = (row["Flow"] * (1 - alpha) + row["Flow_next"] * alpha) / 4.0
            if vehicles <= 0:
                continue
            ET.SubElement(
                interval_el,
                "tazRelation",
                {"from": str(int(row["From"])), "to": str(int(row["To"])), "count": str(round(vehicles, 3))},
            )

    tree = ET.ElementTree(root)
    ET.indent(tree, space="    ")
    tree.write(str(taz_relations_file), encoding="utf-8", xml_declaration=True)
    return taz_relations_file