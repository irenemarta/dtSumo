"""
The script is designed to create TAZ (Traffic Assignment Zones) file starting from the shapefile and the edge file of the network.
The TAZ file (.taz.xml) has a tazSource/tazSink weight for each edge, based on road priority and on the edge direction relative to the zone.
It is then used by marouter for the traffic assignment.

"""

import os
import pandas as pd
import contextily as cx
from typing import Dict, Tuple

import xml.etree.ElementTree as ET

import geopandas as gpd
from shapely import LineString, Point, Polygon

from pathlib import Path
from colorama import Fore, init
import matplotlib.pyplot as plt
import scripts.src.inputs.config as cfg

init(autoreset=True)
BORDER_MAIN_STREETS = [
    "1305446515#0",
    "310476526",
    "111859680#0",
    "134155167#0",
    "134155167#1",
    "1306068768#0",
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


def _cap_wratio(weight_pairs: dict[str, tuple[float, float]], max_ratio: float=5.0):
    if not weight_pairs: # {edge_id: (src_weight, snk_weight)}
        return weight_pairs

    max_src = max(p[0] for p in weight_pairs.values())
    max_snk = max(p[1] for p in weight_pairs.values())
    floor_src = max_src / max_ratio
    floor_snk = max_snk / max_ratio

    return {
        e: (max(src, floor_src), max(snk, floor_snk))
        for e, (src, snk) in weight_pairs.items()
    }

def _soften_wratio(w, k=3):
    return w ** (1.0 / k)

def _edge_w(edge_id: str, priority: int) -> float:
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

    start = Point(edge_geom.coords[0])
    end = Point(edge_geom.coords[-1])
    start_inside = zone_geom.contains(start)
    end_inside = zone_geom.contains(end)

    # return: tazSource, tazSink
    if start_inside and end_inside:
        return 1.0, 1.0
    elif not start_inside and end_inside:
        return 0.1, 1.0
    elif start_inside and not end_inside:
        return 1.0, 0.1
    else:  # both external -> for external zones
        return 0.3, 0.3

def _create_gdf(edges: Dict, crs) -> gpd.GeoDataFrame:
    edge_data = []
    
    for eid, data in edges.items():
        coords = data.get("shape")

        if coords and len(coords) >= 2:
            line = LineString(coords)

            edge_data.append(
                {"edge_id": eid, "geometry": line,}
            )
    gdf_edges = gpd.GeoDataFrame(edge_data, crs=crs)

    return gdf_edges

def _conn_end(connectors: gpd.GeoDataFrame, crs):
    coords_list = connectors.geometry.apply(lambda line: line.coords)
    connectors_end = gpd.GeoDataFrame(
        connectors.drop(columns="geometry"),
        geometry=gpd.points_from_xy(
            x=coords_list.apply(lambda coords: coords[0][0]),
            y=coords_list.apply(lambda coords: coords[0][1]),
        ),
        crs=crs,
    )
    
    proj_conn_end = connectors_end.to_crs(
        epsg=32632
    )  # metric --> GEOPANDAS SJOIN_NEAREST WORKS BEST WITH METRIC CRS
    
    return proj_conn_end


def _build_taz_map(best_external:pd.DataFrame, joined_internal_z: gpd.GeoDataFrame, claimed_edg: set) -> dict:
    taz_map = {} # MAP OF ZONES -> key = ZONE_ID, value = EDGE_LIST
    # prioritise external zones
    for _, row in best_external.iterrows():
        z_id = str(int(row["ZONENO"]))
        taz_map.setdefault(z_id, set()).add(row["edge_id"])

    # map internal excluding claimed edges
    for _, row in joined_internal_z.dropna(subset=["ID_ZONA"]).iterrows():
        if row["edge_id"] in claimed_edg:
            continue  # already assigned to an external -> avoid to overlap
        z_id = str(int(row["ID_ZONA"]))
        taz_map.setdefault(z_id, set()).add(row["edge_id"])
    
    return taz_map

def _compute_taz_w(edges_set: set, edges: dict, gdf_edges: gpd.GeoDataFrame, zone_geom) -> dict:
    combined_weights = {}
    for e_id in sorted(edges_set):  # sorted -> same xml order on every run
        pr = int(edges[e_id]["priority"] if e_id in edges else 20)  # anything more than max priority (highway.primary)
        e_weight_softened = _soften_wratio(_edge_w(e_id, pr))  # define origin and destination probabilities for TAZ edges

        edge_geom = gdf_edges[gdf_edges["edge_id"] == e_id].geometry.values[0]
        src_w, snk_w = _edge_direction_weight(edge_geom, zone_geom)
        combined_weights[e_id] = (src_w * e_weight_softened, snk_w * e_weight_softened)

    combined_weights = _cap_wratio(combined_weights, max_ratio=5.0)
            
    return combined_weights

def _assign_external(proj_conn_end, gdf_edges_proj, N_BEST):
    claimed_edg = set() # no overlap if an edge was already claimed as connector
    rows = []
    for idx, conn in proj_conn_end.iterrows():
        pool = gdf_edges_proj[~gdf_edges_proj["edge_id"].isin(claimed_edg)]
        if pool.empty:
            pool = gdf_edges_proj  # no free edge left = can pick any

        nearest = pool.distance(conn.geometry).nsmallest(N_BEST)
        found = pool.loc[nearest.index, "edge_id"].tolist()
        print(f"Connector idx={idx}, point={conn.geometry}, found edges: {found}")

        for eid, dist in zip(found, nearest):
            rows.append({"ZONENO": conn["ZONENO"], "edge_id": eid, "distance": dist})
            claimed_edg.add(eid)  # next connectors avoid the already selected edge

    best_external = pd.DataFrame(
        rows, columns=["ZONENO", "edge_id", "distance"]
    ).dropna(subset=["ZONENO", "edge_id"])
    return best_external, claimed_edg


def read_TAZ(
    edges: Dict, path_zones: Path, path_connectors: Path, output_path=".", N_BEST: int = 8,
):
    os.makedirs(output_path, exist_ok=True)
    taz_revised = os.path.join(output_path, "francia_peschiera_TAZ.taz.xml")

    zones = gpd.read_file(path_zones)
    connectors = gpd.read_file(path_connectors)
    if connectors.crs is not None and connectors.crs != zones.crs:
        connectors = connectors.to_crs(zones.crs)  # connectors must be in the same crs of zones and edges

    gdf_edges = _create_gdf(edges, zones.crs)
    joined_internal_z = gpd.sjoin(gdf_edges, zones, how="left", predicate="intersects") # on zones for internal
    proj_conn_end = _conn_end(connectors, gdf_edges.crs) # on connectors for external

    best_external, claimed_edg = _assign_external(proj_conn_end, gdf_edges.to_crs(epsg=32632), N_BEST)
    taz_map = _build_taz_map(best_external, joined_internal_z, claimed_edg)

    # polygon for internal zones, connector line for external ones
    zone_geom_map = {str(int(z)): g for z, g in zip(zones["ID_ZONA"], zones.geometry) if pd.notna(z)}
    conn_geom_map = {str(int(z)): g for z, g in zip(connectors["ZONENO"], connectors.geometry)}

    root = ET.Element("additionals")
    taz_root = ET.SubElement(root, "tazs")
    for zone_id, edge_set in sorted(taz_map.items()):
        zone_geom = zone_geom_map.get(zone_id, conn_geom_map.get(zone_id))
        combined_weights = _compute_taz_w(edge_set, edges, gdf_edges, zone_geom)

        taz_element = ET.SubElement(taz_root, "taz", id=zone_id, edges=" ".join(sorted(edge_set)))
        for e_id, (src_final, snk_final) in combined_weights.items():
            ET.SubElement(taz_element, "tazSource", id=e_id, weight=str(round(src_final, 3)))
            ET.SubElement(taz_element, "tazSink", id=e_id, weight=str(round(snk_final, 3)))

    ET.indent(root, space="  ", level=0)  # to correctly indent XML
    ET.ElementTree(root).write(taz_revised, encoding="utf-8", xml_declaration=True)
    print(Fore.GREEN + f"\nTAZ files successfully saved as: {taz_revised}")

    return taz_revised

def plot_taz(zones, centroids, out_path=cfg.IMAGES):
    fig = zones.plot(color="yellow", edgecolor="red", alpha=0.3)
    fig.set_title("TAZ distribution")
    fig.grid(True, alpha=0.3)
    fig.set_axis_off()
    centroids.plot(ax=fig, color="black")
    # connectors.plot(ax=fig, color='black')
    cx.add_basemap(
        ax=fig, source=cx.providers.OpenStreetMap.Mapnik, crs=zones.crs.to_string()
    )
    for _, row in zones.iterrows():
        fig.annotate(
            text=row["ID_ZONA"],
            xy=(row.geometry.centroid.x, row.geometry.centroid.y),
            ha="center",
            fontsize=8,
            color="black",
        )
    plt.savefig(out_path, dpi=500)
    plt.close()
    print(f"TAZ plot saved in {out_path}")

    return out_path