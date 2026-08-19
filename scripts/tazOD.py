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

    """
    sjoin (spatial join)

    geopandas.sjoin(left_df, right_df, how='inner', predicate='intersects', lsuffix='left', rsuffix='right', 
    distance=None, on_attribute=None, **kwargs)[source]
    
    Spatial join of two GeoDataFrames.

    left_df, right_df: GeoDataFrames
    how: string, default ‘inner’
    The type of join:
        ‘left’: use keys from left_df; retain only left_df geometry column

        ‘right’: use keys from right_df; retain only right_df geometry column

        ‘inner’: use intersection of keys from both dfs; retain only left_df geometry column
    
    predicate: string, default ‘intersects’ (other option: 'within')

    Binary predicate. Valid values are determined by the spatial index used.
    """

    # For internal zones - sjoin on zones
    joined_internal_z = gpd.sjoin(gdf_edges, zones, how="left", predicate="intersects")
    # print(joined_internal_z.columns)
    # For external zones - sjoin on connectors
    coords_list = connectors.geometry.apply(lambda line: line.coords)
    # print(coords_list)

    connectors_endpoint = gpd.GeoDataFrame(
        connectors.drop(columns="geometry"),
        geometry=gpd.points_from_xy(
            x=coords_list.apply(lambda coords: coords[0][0]),
            y=coords_list.apply(lambda coords: coords[0][1]),
        ),
        crs=gdf_edges.crs,
    )

    print(connectors_endpoint.to_csv())
    print(f"numero connettori {len(connectors_endpoint)}")
    # coord = [(lon1, lat1), (lon2, lat2)]
    #   coord[-1] = (lon_n, lat_n)
    #   coord[-1][0] = lon_n, coord[-1][1] = lat_n
    # print(joined_external_z.columns)
    """
    geopandas.sjoin_nearest
        geopandas.sjoin_nearest(left_df, right_df, how='inner', max_distance=None, lsuffix='left', rsuffix='right', distance_col=None, exclusive=False)[source]
        Spatial join of two GeoDataFrames based on the distance between their geometries.

        Results will include multiple output records for a single input record where there are multiple equidistant nearest or intersected neighbors.

        Distance is calculated in CRS units and can be returned using the distance_col parameter.
    """
    connectors_endpoint_proj = connectors_endpoint.to_crs(
        epsg=32632
    )  # metric --> GEOPANDAS SJOIN_NEAREST WORKS BEST WITH METRIC CRS

    gdf_edges_proj = gdf_edges.to_crs(epsg=32632)

    joined_external_z = gpd.sjoin_nearest(
        connectors_endpoint_proj, gdf_edges_proj, how="left", distance_col="distance"
    )

    # Create a new xml file to store TAZ information: TAZ.add.xml
    # its extension suggests that it will be used as additional file in SUMO
    root = ET.Element("additionals")
    taz_root = ET.SubElement(root, "tazs")

    # MAP OF ZONES -> key = ZONE_ID, value = EDGE_LIST
    taz_map = {}

    """
    The setdefault() method returns the value of the item with the specified key.
    SYNTAX: dictionary.setdefault(keyname, value)
    if the keyname exists, get its value, otherwise set it to be the specified value.
    """
    
    # assegna le TAZ ESTERNE per prime sequenzialmente
    # Ogni connettore esclude gli edge già "reclamati" dai connettori precedenti in modo da non creare overlap di zone
    claimed_edges = set()
    edge_geoms_full = gdf_edges_proj.geometry

    rows = []
    for idx, conn in connectors_endpoint_proj.iterrows():
        pool = edge_geoms_full[~gdf_edges_proj["edge_id"].isin(claimed_edges)]
        if pool.empty:
            pool = edge_geoms_full  # fallback estremo: no free edge left -> can pick any edge

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
    #print(best_external)

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
        # print(zone_rows)

        # for e_id, e_weight in weights.items():
        #     # print(zones[["ID_ZONA", "geometry"]].head())
        #     edge_geom = gdf_edges[gdf_edges["edge_id"] == e_id].geometry.values[0]
        #     # zone_geom = zones[zones["ID_ZONA"] == e_id].geometry.values[0]
        #     src_w, snk_w = _edge_direction_weight(edge_geom, zone_geom)
        
            # ET.SubElement(
            #     taz_element,
            #     "tazSource",
            #     id=str(e_id),
            #     weight=str(round(src_w * e_weight, 3)),
            # )
            # ET.SubElement(
            #     taz_element,
            #     "tazSink",
            #     id=str(e_id),
            #     weight=str(round(snk_w * e_weight, 3)),
            # )
            
            
        # invece di scrivere src_w*e_weight edge per edge dentro il loop,
        # prima raccogliete tutti i pesi combinati della TAZ:
        combined_weights = {}
        for e_id, e_weight_softened in weights.items():
            edge_geom = gdf_edges[gdf_edges["edge_id"] == e_id].geometry.values[0]
            src_w, snk_w = _edge_direction_weight(edge_geom, zone_geom)
            combined_weights[e_id] = (src_w * e_weight_softened, snk_w * e_weight_softened)

        # POI cappate il risultato finale, non i fattori singoli
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

    """
    for e_zone in sorted(external_zones):
        taz_element = ET.SubElement(root, "taz", id=str(e_zone))
        taz_element.set("edges", " ")

        ET.indent(root, space="  ", level=0) # to correctly indent XML
        tree = ET.ElementTree(root)
        tree.write(taz_revised, encoding='utf-8', xml_declaration=True)
    """

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


def main():

    # ROUTING_ALGS = ["dijkstra", "astar", "CH"]
    ITERATIONS = 70
    SAVE_STEP = 10

    # ROUTING_CONFIGS = [
    #     # Logit
    #     {
    #         "route_choice": "logit",
    #         "route_alg": "dijkstra",
    #         "logit_theta": 0.01,
    #         "tag": "logit_theta0.01",
    #     },  # default sumo
    #     {
    #         "route_choice": "logit",
    #         "route_alg": "dijkstra",
    #         "logit_theta": 0.05,
    #         "tag": "logit_theta0.05",
    #     },
    #     {
    #         "route_choice": "logit",
    #         "route_alg": "dijkstra",
    #         "logit_theta": 0.15,
    #         "tag": "logit_theta0.15",
    #     },
    #     {
    #         "route_choice": "logit",
    #         "route_alg": "dijkstra",
    #         "logit_theta": 0.5,
    #         "tag": "logit_theta0.5",
    #     },
    #     # Gawron
    #     {
    #         "route_choice": "gawron",
    #         "route_alg": "dijkstra",
    #         "gawron_a": 0.1,
    #         "gawron_beta": 0.5,
    #         "tag": "gawron_a0.1_b0.5",
    #     },
    #     {
    #         "route_choice": "gawron",
    #         "route_alg": "dijkstra",
    #         "gawron_a": 0.3,
    #         "gawron_beta": 0.9,
    #         "tag": "gawron_a0.3_b0.9",
    #     },  # default sumo
    #     {
    #         "route_choice": "gawron",
    #         "route_alg": "dijkstra",
    #         "gawron_a": 0.5,
    #         "gawron_beta": 0.9,
    #         "tag": "gawron_a0.5_b0.9",
    #     },
    # ]

    ROUTING_CONFIGS = [
        {
            "route_choice": "logit",
            "route_alg": "dijkstra",
            "logit_theta": 0.05,
            "tag": "logit_theta0.15",
        },
        {
            "route_choice": "gawron",
            "route_alg": "dijkstra",
            "gawron_a": 0.3,
            "gawron_beta": 0.9,
            "tag": "gawron_a0.3_b0.9",
        },
    ]

    # Paths definition for input files

    net = cfg.NET_FILE
    edges_path = cfg.EDG_PARSE_XML
    output_cfg = cfg.CFG_DIR
    zones_path = cfg.ZONES
    connectors_path = cfg.CONNECTORS

    od_matrix_am = cfg.OD_MATRICES["AM"]
    od_matrix_pm = cfg.OD_MATRICES["PM"]

    # TAZ and TAZREL
    output_taz = cfg.OUTPUT_DIR_ADD
    tazrel_am = cfg.TAZREL["AM"]
    tazrel_pm = cfg.TAZREL["PM"]

    # User Equilbrium with Duarouter
    output_am = cfg.OD_ROUTES["AM"].parent
    output_pm = cfg.OD_ROUTES["PM"].parent

    cfg.create_project_structure()

    for det_dir in [
        cfg.DET_OUT["LOGIT"]["AM"],
        cfg.DET_OUT["LOGIT"]["PM"],
        cfg.DET_OUT["GAWRON"]["AM"],
        cfg.DET_OUT["GAWRON"]["PM"],
    ]:
        Path(det_dir).mkdir(parents=True, exist_ok=True)

    # Create edge dictionary using (helpers.py) function
    edges = parse_edges(edges_path)
    # Write a revisioned TAZ file
    taz_file = read_revisioned_TAZ(
        edges, zones_path, connectors_path, output_path=output_taz
    )

    taz_rel_am = write_taz_relations(
        od_matrix_am, output_add=tazrel_am.parent, begin=8 * 3600, end=9 * 3600
    )
    taz_rel_pm = write_taz_relations(
        od_matrix_pm, output_add=tazrel_pm.parent, begin=18 * 3600, end=19 * 3600
    )

    # # DUAROUTER - single User Equilibrium (UE)

    # # Create trips and routes from O/D matrices - with duarouter
    # for p in ["AM", "PM"]:
    #     for s in ["base_no_TLS", "base_with_TLS"]:
    #         print(
    #             Fore.GREEN
    #             + f"Scenario {' '.join(part.strip() for part in s.split('_'))} - {p}"
    #         )
    #         trips, dict_routes_duarouter = trips_routes_from_od(
    #             net_file=net,
    #             taz_file=taz_file,
    #             od_matrix=cfg.OD_MATRICES[p],
    #             local_priority_threshold=LOCAL_PRIORITY_THRESHOLD,
    #             out_dir=cfg.OD_ROUTES[p].parent,
    #         )
    #         dict_cleaned = {}
    #         for alg, route_file in dict_routes_duarouter.items():
    #             cleaned = filter_short_flows(
    #                 input_xml=Path(route_file),
    #                 output_new=Path(route_file).parent / f"routes_{alg}_clean.rou.xml",
    #             )
    #             dict_cleaned[alg] = cleaned

    #             CfgAttributes(
    #                 net=net,
    #                 routes=dict_cleaned["dijkstra"],
    #                 output_cfg=output_cfg,
    #                 output_sumo=cfg.SIM_OUT[s][p],
    #                 config_name=f"francia_peschiera_peak_{s[5:]}_{p}.sumocfg",
    #                 meso=False,
    #                 setting=cfg.VIEW
    #             ).build(
    #                 method="base",
    #                 taz=taz_file,
    #                 begin=28800 if p == "AM" else 64800,
    #                 end=36000 if p == "AM" else 72000,
    #                 detectors=cfg.DETECTORS[s][p],
    #             )

    #         print(Fore.LIGHTBLACK_EX + "\tDone.\n")

    # LOGIT AND GAWRON with DUAITERATE
    # DUAITERATE - User Equilibrium (UE) iterated
    # print(f"\nSTARTING DUAITERATE - PEAK")
    # for p in ["AM", "PM"]:
    #     for s in ["DUA_no_TLS", "DUA_with_TLS"]:
    #         print(
    #             Fore.GREEN
    #             + f"Scenario {' '.join(part.strip() for part in s.split('_'))} - {p}"
    #         )
    #         for rc in ROUTING_CONFIGS:
    #             tag = rc["tag"]
    #             route_choice = rc["route_choice"]
    #             router = rc.get("route_alg", "dijkstra")

    #             # Seleziona i detector giusti in base all'algoritmo
    #             det_am = cfg.DETECTORS["LOGIT"]["AM"] if route_choice == "logit" else cfg.DETECTORS["GAWRON"]["AM"]
    #             det_pm = cfg.DETECTORS["LOGIT"]["PM"] if route_choice == "logit" else cfg.DETECTORS["GAWRON"]["PM"]

    #             out_path = Path(cfg.WORKDIRS["LOGIT"][p] if route_choice == "logit" else cfg.WORKDIRS["GAWRON"][p]) / tag
    #             out_path.mkdir(parents=True, exist_ok=True)

    #             trips, dict_routes_duarouter = trips_routes_from_od(
    #                     net, taz_file, cfg.OD_MATRICES[p], cfg.OD_ROUTES[p].parent
    #             )

    #             run_duaIterate(
    #                 net_file=net,
    #                 trips_file=trips,
    #                 out_dir=out_path,
    #                 #additional_files=det_am,
    #                 iterations=ITERATIONS,
    #                 save_every=SAVE_STEP,
    #                 route_choice=route_choice,
    #                 routing_alg=router,
    #                 logit_theta=rc.get("logit_theta", 0.01),
    #                 gawron_a=rc.get("gawron_a", 0.05),
    #                 gawron_beta=rc.get("gawron_beta", 0.3),
    #                 begin=28800 if p == "AM" else 64800,
    #                 end=36000 if p == "AM" else 72000,
    #             )

    #             print(Fore.LIGHTBLACK_EX + "\tDone.\n")

    #             # cleaned_dua_pm = filter_short_flows(
    #             #     input_xml=result_pm,
    #             #     output_new=result_am.parent / "routes_clean.rou.xml"
    #             # )

    # MAROUTER - Macroscophic routing
    print(f"\nSTARTING MAROUTER - PEAK")
    for p in ["AM", "PM"]:
        for s in ["MA_no_TLS", "MA_with_TLS"]:
            print(
                Fore.GREEN
                + f"Scenario {' '.join(part.strip() for part in s.split('_'))} - {p}"
            )

            routes_marouter = run_marouter(
                net_file=Path(net),
                od_matrices=cfg.OD_MATRICES[p],
                netload_output=Path(cfg.WORKDIRS[s][p], "netload_ouput.xml"),
                # ♥all_pair_output=Path(cfg.WORKDIRS[s][p], "allpair_ouput.xml"),
                trips_output=os.path.join(
                    cfg.WORKDIRS[s][p], "od_trips_file.odtrips.xml"
                ),
                logit_theta=0.1,
                logit_beta=0.15,
                route_choice="logit",
                taz_file=Path(taz_file),
                # taz_rel=Path(taz_rel_am),
                additional_files=[cfg.DETECTORS[s][p], cfg.VTYPE],
                out_dir=cfg.WORKDIRS[s][p],
                # scale=0.7,
                method="SUE",
                paths=15,
                path_penalty=25.0,
                weights_priority=0.5,
                max_alternatives=10,
                max_iterations=200,
                tolerance=0.5,
                # path_penalty=6.0,
                begin=28800 if p == "AM" else 64800,
                end=36000 if p == "AM" else 72000,
            )

            routes_ma_clean = filter_short_flows(
                routes_marouter,
                output_new=Path(cfg.WORKDIRS[s][p]) / "marouter_output_clean.rou.xml",
            )

            routes_final = filter_zero_prob(routes_ma_clean)
            
            # routes_extended, stats = extend_route_endpoints_coverage(
            #     routes_xml_path=routes_final,
            #     edges=edges,
            #     taz_file=taz_file,
            #     net_file=net,
            #     output_path=Path(cfg.WORKDIRS[s][p]) / "marouter_output_extended.rou.xml",
            #     service_priority_threshold=5,
            #     max_uses_per_route=2,
            #     max_connector_length=1500.0,
            # )
            
            print(stats)

            CfgAttributes(
                net=net,
                routes=routes_extended,
                output_cfg=output_cfg,
                output_sumo=cfg.SIM_OUT[s][p],
                config_name=f"francia_peschiera_MAROUTER_{s[3:]}_{p}.sumocfg",
                meso=False,
                setting=cfg.VIEW
            ).build(
                method="marouter",
                taz=taz_file,
                begin=28800 if p == "AM" else 64800,
                end=36000 if p == "AM" else 72000,
                detectors=cfg.DETECTORS[s][p],
            )

            print(Fore.LIGHTBLACK_EX + "\tDone.\n")


if __name__ == "__main__":
    main()
