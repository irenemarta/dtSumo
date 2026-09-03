"""
@file       tazOD.py
@author     Irene Marta
@date       2026

DA RIVEDERE


The script is designed to create TAZ (Traffic Assignment Zones) file starting from the shapefile and the edge file of the network.
The TAZ file is generated as an additional file (taz.add.xml) in order to be used in netedit.
Finally, a O/D matrix will eventually be used to generate trips and routes leveraging SUMO tools (od2Trips and duarouter).

"""

# TODO: UPDATE!!!!!!!!!

import os
import pandas as pd
from typing import Dict, Tuple

import xml.etree.ElementTree as ET

import geopandas as gpd
from shapely import LineString, Point, Polygon

from pathlib import Path
from colorama import Fore, init
import matplotlib.pyplot as plt

from scripts.src.modules.entities import CfgAttributes
from scripts.src.operations.cmd import (
    trips_routes_from_od,
    run_duaIterate,
    run_marouter,
)

from scripts.src.helpers import parse_edges, write_taz_relations
from scripts.src.operations.filtering import filter_short_flows, filter_zero_prob
import scripts.src.inputs.config as cfg

init(autoreset=True)
MAX_PRIORITY_SOURCE = 10
BORDER_MAIN_STREETS = [
    "1305446515#0",
    "310476526",
    "111859680#0",
    "134155167#0",
    "134155167#1",
    "1306068768#0",
    "310476526",
]
BORDER_RESIDENTIALS = [
    "1311410663#0",
    "824542217#0",
    "134460789#0",
    "134460789#1",
    "134460789#2",
    "37679200#0",
    "37679200#1",
    "37679200#2",
    "37679200#3",
    "37679200#4",
    "37679200#5",
    "37679200#6",
    "37679200#7",
]

LOCAL_PRIORITY_THRESHOLD = 8
CONNECTOR_BUFFER = 400.0  # metri


def _classify_edges_in_zone(gdf_edges_proj, geom, edges_info, buffer=CONNECTOR_BUFFER):
    within = gdf_edges_proj[gdf_edges_proj.distance(geom) <= buffer]
    return [
        row["edge_id"]
        for _, row in within.iterrows()
        if int(edges_info.get(row["edge_id"], {}).get("priority", 20))
        < LOCAL_PRIORITY_THRESHOLD
    ]

def _cap_weight_ratio(weight_pairs: dict, max_ratio=5.0):
    """weight_pairs: {edge_id: (src_weight, snk_weight)}.
    Impone il cap SEPARATAMENTE su src e su snk (sono usati per campionamenti
    indipendenti: uno per l'origine, uno per la destinazione)."""
    if not weight_pairs:
        return weight_pairs

    max_src = max(p[0] for p in weight_pairs.values())
    max_snk = max(p[1] for p in weight_pairs.values())
    floor_src = max_src / max_ratio
    floor_snk = max_snk / max_ratio

    return {
        e: (max(src, floor_src), max(snk, floor_snk))
        for e, (src, snk) in weight_pairs.items()
    }


# Comrpess ratio among weights (to soften the maximal delta)
def _soften_weight(w, k=3):
    return w ** (1.0 / k)


def _edge_weight(
    edge_id: str, priority: int, is_mandatory_entry: bool = False
) -> float:
    if is_mandatory_entry:
        return 0.15
    if edge_id in BORDER_MAIN_STREETS:
        return 0.1
    if edge_id in BORDER_RESIDENTIALS:
        return 0.8
    if priority >= 11:
        return 0.05
    elif priority >= 5:
        return 0.8
    else:
        return 1.0
    

def _edge_direction_weight(
    edge_geom: LineString, zone_geom: Polygon
) -> Tuple[float, float]:
    """Returns tazSource and tazSink probability based on the direction of the edge relative to the zone geometry.
    If start point inside and finish outside = tazSource 1.0 and tazSink 0.0;
    If start point outside and finish inside = viceversa
    if both inside = symmetric weights."""

    # print(zone_geom)
    start = Point(edge_geom.coords[0])
    end = Point(edge_geom.coords[-1])

    #  Use the .contains() or .within() methods to check if a Point or
    # a smaller geometric shape is completely enclosed within a larger polygon or object.
    start_inside = zone_geom.contains(start)
    end_inside = zone_geom.contains(end)

    # return: tazSource, tazSink
    if start_inside and end_inside:
        return 1.0, 1.0
    elif not start_inside and end_inside:
        return 0.1, 1.0  # can't be zero
    elif start_inside and not end_inside:
        return 1.0, 0.1
    else:  # both external -> for external zones
        return 0.3, 0.3


# Read edge file and shapefile, leverage geopandas to spatially join them and assign edges to zones
def read_revisioned_TAZ(
    edges: Dict, path_zones: Path, path_connectors: Path, output_path=".", N_BEST: int = 8,
):

    zones = gpd.read_file(path_zones)
    connectors = gpd.read_file(path_connectors)

    # Define output
    if not os.path.exists(output_path):
        os.makedirs(output_path, exist_ok=True)
    taz_revised = os.path.join(output_path, "francia_peschiera_TAZ.taz.xml")

    # Geopandas dataframe for edges
    edge_data = []

    for eid, data in edges.items():
        coords = data.get("shape")
        # edge_type = data.get("type")
        # priority = data.get("priority")
        # weight = _edge_weight(priority)

        if coords and len(coords) >= 2:
            # Create edge line as object LineString between coordinates
            line = LineString(coords)

            edge_data.append(
                {
                    "edge_id": eid,
                    "geometry": line,
                    # "type": edge_type,
                    # "priority": priority,
                    # "weight": weight,
                }
            )

    gdf_edges = gpd.GeoDataFrame(
        edge_data, crs=zones.crs
    )  # same coordinates reference system of shape file
    zones["shape"] = (
        zones.geometry
    )  # add a column to copy the geometry and mantain it even after the spatial join as 'shape'

    # For internal zones - sjoin on zones
    joined_internal_z = gpd.sjoin(gdf_edges, zones, how="left", predicate="intersects")

    # For external zones - sjoin on connectors
    coords_list = connectors.geometry.apply(lambda line: line.coords)

    connectors_endpoint = gpd.GeoDataFrame(
        connectors.drop(columns="geometry"),
        geometry=gpd.points_from_xy(
            x=coords_list.apply(lambda coords: coords[0][0]),
            y=coords_list.apply(lambda coords: coords[0][1]),
        ),
        crs=gdf_edges.crs,
    )
    
    connectors_endpoint_proj = connectors_endpoint.to_crs(
        epsg=32632
    )  # metric --> GEOPANDAS SJOIN_NEAREST WORKS BEST WITH METRIC CRS

    gdf_edges_proj = gdf_edges.to_crs(epsg=32632)

    # Create a new xml file to store TAZ information: TAZ.add.xml
    # its extension suggests that it will be used as additional file in SUMO
    root = ET.Element("additionals")
    taz_root = ET.SubElement(root, "tazs")

    # MAP OF ZONES -> key = ZONE_ID, value = EDGE_LIST
    taz_map = {}
    
    # frist assign EXTERNAL TAZs  + no overlap if an edge was already claimed as connector.
    claimed_edges = set()
    edge_geoms_full = gdf_edges_proj.geometry

    rows = []
    for idx, conn in connectors_endpoint_proj.iterrows():
        pool = edge_geoms_full[~gdf_edges_proj["edge_id"].isin(claimed_edges)]
        if pool.empty:
            pool = edge_geoms_full  # fallback: no free edge left -> can pick any edge

        distances = pool.distance(conn.geometry) # returns list of geometries
        nearest = distances.nsmallest(min(N_BEST, len(distances)))
        found = [gdf_edges_proj.loc[pos, "edge_id"] for pos in nearest.index]
        print(f"Connector idx={idx}, point={conn.geometry}, found edges: {found}")

        for pos, dist in nearest.items():
            eid = gdf_edges_proj.loc[pos, "edge_id"]
            rows.append({"ZONENO": conn["ZONENO"], "edge_id": eid, "distance": dist})
            claimed_edges.add(eid)  # next connectors avoid the already selected edge

    best_external = pd.DataFrame(
        rows, columns=["ZONENO", "edge_id", "distance"]
    ).dropna(subset=["ZONENO", "edge_id"])

    # first, prioritise external zone by mapping those
    for _, row in best_external.iterrows():
        z_id = str(int(row["ZONENO"]))
        taz_map.setdefault(z_id, set()).add(row["edge_id"])

    # map internal excluding claimed edges
    for _, row in joined_internal_z.dropna(subset=["ID_ZONA"]).iterrows():
        if row["edge_id"] in claimed_edges:
            continue  # already assigned to an external -> avoid to overlap
        z_id = str(int(row["ID_ZONA"]))
        taz_map.setdefault(z_id, set()).add(row["edge_id"])

    connector_geom_map = {}  # map zone -> connectors for external zones

    for _, row in connectors.iterrows():
        z_id = str(int(row["ZONENO"]))
        connector_geom_map[z_id] = row.geometry  # type Polygon

    # Iterate on edge edge cointained in a zone, for each zone
    for zone_id, edge_set in sorted(taz_map.items()):
        if not edge_set:
            print(
                Fore.YELLOW
                + f"[WARN] Zone {zone_id} has no associations to an edge set."
            )
            continue
        zone_rows = zones[zones["ID_ZONA"] == int(float(zone_id))]
        # internal zone

        if not zone_rows.empty:
            zone_geom = zone_rows.geometry.values[0]
        else:
            zone_geom = connector_geom_map.get(zone_id, None)  # external zones

        weights = {}
        for e_id in edge_set:
            priority = int(
                edges[e_id]["priority"] if e_id in edges else 20
            )  # anything more than max priority (highway.primary)
            raw_weights = _edge_weight(
                e_id, priority
            )  # define origin and destination probabilities for TAZ edges
            weights[e_id] = _soften_weight(raw_weights)
            
        # Fallback: force uniform weight if all weights are 0 in the TAZ
        if all(w == 0.0 for w in weights.values()):
            weights = {e_id: 1.0 for e_id in edge_set}

        taz_element = ET.SubElement(taz_root, "taz", id=str(int(zone_id)))

        combined_weights = {}
        for e_id, e_weight_softened in weights.items():
            edge_geom = gdf_edges[gdf_edges["edge_id"] == e_id].geometry.values[0]
            src_w, snk_w = _edge_direction_weight(edge_geom, zone_geom)
            combined_weights[e_id] = (src_w * e_weight_softened, snk_w * e_weight_softened)

        combined_weights = _cap_weight_ratio(combined_weights, max_ratio=5.0)

        for e_id, (src_final, snk_final) in combined_weights.items():
            ET.SubElement(taz_element, "tazSource", id=str(e_id), weight=str(round(src_final, 3)))
            ET.SubElement(taz_element, "tazSink", id=str(e_id), weight=str(round(snk_final, 3)))
            

        taz_element.set("edges", " ".join(list(edge_set)))

    try:
        ET.indent(root, space="  ", level=0)  # to correctly indent XML
        tree = ET.ElementTree(root)
        tree.write(taz_revised, encoding="utf-8", xml_declaration=True)
        print(f"\nTAZ files successfully saved as: {taz_revised}")

    except Exception as e:
        raise RuntimeError(f"Failed to write TAZ file")

    return taz_revised


def plot_taz(zones, centroids):
    out_path = cfg.IMAGES
    """Plots created TAZs from SV zones"""
    import contextily as cx

    cmap = plt.get_cmap("tab20", len(zones))
    fig = zones.plot(color="yellow", edgecolor="red", cmap=cmap, legend=True, alpha=0.3)
    fig.set_title("TAZ distribution")
    fig.grid(True, alpha=0.3)
    fig.set_axis_off()
    centroids.plot(ax=fig, color="black")
    # connectors.plot(ax=fig, color='black')
    cx.add_basemap(
        ax=fig, source=cx.providers.OpenStreetMap.Mapnik, crs=zones.crs.to_string()
    )
    label = zones["ID_ZONA"]
    try:
        for idx, row in zones.iterrows():
            fig.annotate(
                text=row["ID_ZONA"],
                xy=(row.geometry.centroid.x, row.geometry.centroid.y),
                ha="center",
                fontsize=8,
                color="black",
            )
        print(f"TAZ plot saved in {out_path}")
    except Exception as e:
        print(f"ERROR while plotting TAZs: {e}")

    return plt.savefig(out_path, dpi=500)