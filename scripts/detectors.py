"""
@file       detectors.py
@author     Irene Marta
@date       2026
"""

import os
import pandas as pd
import geopandas as gpd

import xml.etree.ElementTree as ET
from shapely import Point, LineString
from pathlib import Path

from typing import Dict, Tuple
import scripts.src.inputs.config as cfg
import matplotlib.pyplot as plt


_lanes_cache = {}

def _create_geopandas_df(sens_anagraphics: pd.DataFrame) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        sens_anagraphics,
        geometry=gpd.points_from_xy(sens_anagraphics.lon, sens_anagraphics.lat),
        crs="EPSG:4326",
    ).to_crs(epsg=3857)
    

def sensors_positions(gdf_sensors) -> Dict[Tuple, Tuple]:
    sensors_pos_dict = {
        (row["Cod_sens"], row["name"]): (row["lat"], row["lon"])
        for _, row in gdf_sensors.iterrows()
    }
    return sensors_pos_dict


### NET PARSING - each lane is stored as an edge and the edge ID info is kept in the dataframe
def edges_as_lanes(edg_path: Path) -> pd.DataFrame:
    tree = ET.parse(edg_path)
    root = tree.getroot()
    lanes_as_edges = {}

    for edge in root.findall("edge"):
        # Ignore internal edges
        if edge.attrib.get("function") == "internal":
            continue

        eid = edge.attrib.get("id", "")
        from_id = edge.attrib.get("from")
        to_id = edge.attrib.get("to")

        if from_id is None or to_id is None:
            continue

        # each lane becomes an edge
        for lane in edge.findall("lane"):
            lane_id = lane.attrib.get("id")  # "edgeId_0", "edgeId_1"
            lane_index = lane.attrib.get("index")
            length = lane.attrib.get("length")
            speed = lane.attrib.get("speed")
            l_shape = lane.attrib.get("shape")

            lane_data = {
                "id_edge": eid,
                "index": lane_index,
                "from": from_id,
                "to": to_id,
                "name": edge.attrib.get("name", ""),
                "numLanes": edge.attrib.get("numLanes", ""),
                "speed": speed,
                "length": length,
                "shape": l_shape,
            }

            # key = lane_id, value = lane_data
            lanes_as_edges[lane_id] = lane_data

    df_lanes = pd.DataFrame.from_dict(lanes_as_edges, orient="index")

    return df_lanes


### GET DATA on which computation will be performed
def _get_lanes_this_edge(
    sens_coord: Tuple[float, float], df_lanes: pd.DataFrame
) -> pd.DataFrame:
    
    """Given a sensor position (lat, lon), returns all lanes of the nearest edge.
    Results are cached by coordinate.
    """
    
    if sens_coord in _lanes_cache:
        return _lanes_cache[sens_coord]

    sensor_pt = gpd.GeoSeries(
        [Point(sens_coord[1], sens_coord[0])], crs="EPSG:4326"
    )
    sensor_pt_projected = sensor_pt.to_crs(
        epsg=32618
    )  # gpd.distance() works best in metric coordinates
    
    # Initialization
    best_lane_id = None
    min_dist = float("inf")
    # Look for positional matches
    for l_id, row in df_lanes.iterrows():
        # shape string ["x1,y1 x2,y2"] to list of tuples [(x1,y1), (x2,y2)]
        line_coords = [
            tuple(map(float, coord.split(","))) for coord in row["shape"].split(" ")
        ]
        line = gpd.GeoSeries([LineString(line_coords)], crs="EPSG:4326")
        line_projected = line.to_crs(epsg=32618)
        # Compute distances between sensors and lanes
        dist = min(sensor_pt_projected.distance(line_projected.union_all()))
        """.uninion_all() performes better when comparing one point against many lines."""
        # Find best edge
        if dist < min_dist:
            min_dist = dist
            best_lane_id = l_id

    # Edge IDs of the best lane (from df_lanes column) to collect all its lanes
    target_edge_id = df_lanes.loc[best_lane_id, "id_edge"]
    lanes_of_this_edge = df_lanes[df_lanes["id_edge"] == target_edge_id]

    _lanes_cache[sens_coord] = lanes_of_this_edge

    return lanes_of_this_edge


### MATCH DETECTOR - SENSOR
def _build_sensor_lane_lookup(gdf_sens: gpd.GeoDataFrame, df_lanes: pd.DataFrame) -> pd.DataFrame:
    """Create a table matching sensors data and lanes data basing on coordinates"""
    records = []
    for _, sensor in gdf_sens.iterrows():
        coord = (sensor['lat'], sensor['lon'])
        lanes_this_edge = _get_lanes_this_edge(sens_coord=coord, df_lanes=df_lanes)
        
        for lane_id in lanes_this_edge.index:
            records.append({
                "Cod_sens": sensor['Cod_sens'],
                "lane_id": lane_id,
                "id_edge": lanes_this_edge.loc[lane_id, "id_edge"]
            })
    
    return pd.DataFrame(records)


### DETECTORS GENERATION: Generate one detector per existent lane
def generate_all_detectors(
    # sensors_pos_dict: Dict[Tuple, Tuple],
    df_lanes: pd.DataFrame,
    frequence: int,
    add_path: Path,
    output_path: Path = None,
):
    
    """output_path = None for Logit, Gawron, Duaiterate (relative path), otherwhise absolute path (base and marouter)"""

    with open(add_path, "w") as f:
        f.write("<additional>\n")
        for lane_id, row in df_lanes.iterrows():
            edge_id = row['id_edge']
            pos_middle = round(
                float(row["length"]) / 2, 2
            )  # center based on lane lenght
            detector_id = f"e1_{lane_id}"
            file_value = f"{output_path}/det_{edge_id}.xml" if output_path else f"det_{edge_id}.xml"

            f.write(
                f'  <e1Detector id="{detector_id}" lane="{lane_id}" '
                f'pos="{pos_middle}" freq="{frequence}" '
                f'file="{file_value}"/>\n'
            )

        f.write("</additional>\n")

    return print(f"Detectors created - output in {output_path}\n")



## Simulation output generation
def _get_det_output_data(file):
    tree = ET.parse(file)
    root = tree.getroot()

    data = []
    for interval in root.findall("interval"):
        data.append(interval.attrib)

    df_det_data = (
        pd.DataFrame(data)
        .apply(pd.to_numeric, errors="ignore")
        .assign(
            time_dt=lambda x: abs(x["begin"] - x["end"]),
            cum_time=lambda x: x["time_dt"].cumsum(),
            cum_vehic=lambda x: x["nVehEntered"].cumsum(),
            cum_flow=lambda x: x['flow'].cumsum(),
            throughput=lambda x: x['flow'] / x['time_dt'],
            cum_throughput=lambda x: x['cum_vehic'] / x['cum_time'],
        )
    )

    return df_det_data



def load_det_output_sensors(det_out_dir: Path, sensors_lane_lookup: pd.DataFrame) -> Dict[int, pd.DataFrame]:
    """Load and aggregate detector output files for the lanes associated with real sensors."""

    results = {}
    
    for cod_sens, group in sensors_lane_lookup.groupby("Cod_sens"):
        dfs = []
        for lane_id in group["lane_id"]:
            det_file = det_out_dir / f"det_{lane_id}.xml"
            
            if det_file.exists():
                dfs.append(_get_det_output_data(det_file))
            else:
                print(f"WARNING: missing detectors file: {det_file}")
                
        if dfs: 
            # sum flows across lanes of the same edge
            df_combined = (pd.concat(dfs).groupby("begin", as_index=False).sum())
            results[cod_sens] = df_combined
            
    return results



def compute_real_throughput(flows: pd.DataFrame, start: int, end:int) -> pd.DataFrame:
    # TODO: PANDERA CHECK PER INPUT FLOWS
    df_pasta_counts = pd.DataFrame(flows[flows['hour'].isin([start, end])].groupby(by=['Cod_sens', 'daytime', 'hour'])['count_all'].sum())
    th_real = round(df_pasta_counts.groupby('Cod_sens').mean(), 2)
    th_real = th_real.reset_index().rename(columns={'count_all':'TH_real'})
    th_real['Cod_sens'] = th_real['Cod_sens'].astype(int)
    
    return th_real



def throughput_analysis(th_real: pd.DataFrame, gdf_sens: gpd.GeoDataFrame, df_dict_sim: Dict[str, pd.DataFrame]):
    """Compare simulated vs real throughput for each sensor."""
    
    # sensor positions essential data
    gdf_sens_essential = gdf_sens[['Cod_sens', 'name', 'strada', 'lat', 'lon']]
    
    data = []

    for name, df in df_dict_sim.items():
        cod_sens = int(name.strip('.xml')[4:])
        th_tot = (df['cum_flow'].iloc[-1] / df['cum_time'].iloc[-1]) * 3600  # veh/h
        data.append({'Cod_sens': cod_sens, 'TH_sim': th_tot})

    th_df_sim = pd.DataFrame(data).sort_values('Cod_sens')
    
    df_th_joined = pd.merge(th_real, th_df_sim, on='Cod_sens')
    df_th_joined_complete = pd.merge(gdf_sens_essential, df_th_joined, on='Cod_sens').set_index('Cod_sens')
    
    return df_th_joined_complete


def plot_detector_trend(df, name, output_folder, metric="speed"):

    plt.figure(figsize=(10, 5))
    plt.plot(df["begin"] / 3600, df[metric], label=f"{metric.capitalize()}")

    plt.xlabel("Time (h)")
    plt.ylabel(metric)
    plt.ylim(top=30)  # 30 m/s = 108
    plt.title(f"{metric.capitalize()} on {name}")
    plt.grid(True)
    plt.legend()
    plt.tight_layout()
    
    return plt.savefig(Path(output_folder) / f"{name}_{metric}.png", dpi=500)



def main():
    ### INPUT
    edge_path = cfg.EDG_PARSE_XML
    freq = 60 * 5  # step code PASTA = 5 minutes

    
    # 1. parse network to get lane data
    lanes_data = edges_as_lanes(edge_path)

    # 2. Generate all detectors
    for scenario in cfg.SCENARIOS:
        for period in cfg.PERIODS:
            add_path = cfg.DETECTORS[scenario][period]
            output_path = cfg.DET_OUT[scenario][period]

            add_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.mkdir(parents=True, exist_ok=True)

            generate_all_detectors(lanes_data, freq, add_path, output_path)

    # 3. Build sensor→lane lookup (run once, then optionally cache to CSV)
        lookup_path = cfg.SENS_LANE_LOOKUP   # DATA_PATH /sens_lane_MATCH.csv
        if lookup_path.exists():
            sensor_lane_lookup = pd.read_csv(lookup_path)
            print("Sensor–lane lookup loaded from cache")
        else:
            from scripts.src.operations.connections import _get_pasta_data
            from dotenv import load_dotenv
            load_dotenv()
            connection_strings = {
                "ista": os.getenv("ISTA_URL"),
                "istc": os.getenv("ISTC_URL"),
            }
            anagrafica, _ = _get_pasta_data(
                connection_strings["ista"], connection_strings["istc"]
            )
            gdf_sens = _create_geopandas_df(anagrafica)
            sensor_lane_lookup = _build_sensor_lane_lookup(gdf_sens, lanes_data)
            lookup_path.parent.mkdir(parents=True, exist_ok=True)
            sensor_lane_lookup.to_csv(lookup_path, index=False)
            print(f"Sensor–lane lookup saved to {lookup_path}")
    
        # 4. After simulation: load filtered detector outputs and analyse
        #    (example for one scenario/period — loop as needed)
        # scenario, period = "base", "AM"
        # det_outputs = load_detector_outputs_for_sensors(
        #     cfg.DET_OUT[scenario][period], sensor_lane_lookup
        # )
        # th_real = compute_real_throughput(flows, start=7, end=9)
        # results  = throughput_analysis(th_real, gdf_sens, det_outputs)

if __name__ == "__main__":
    main()
