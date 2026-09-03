"""
@file       database.py
@author     Irene Marta
@date       2026

The script aims at creating a database of edges related to the area of interest.
Starting from default values, the database gets enriched and updated iteration by iteration, following a precise matching mechanism.
The matching logic leverages the Shapely library and fixes an acceptance threshold.

WORKFLOW:
1. Define how the matches have to be found - including both complete and partial edges.
2. Create a new database, containing all SUMO values and some default values to be updated
3. Perform database update
4. Fallback for unmatched edges: update default values (Flow 0 velocities and Zones) using average values of the same area
5. Save results in the output directory
"""

import os
import pandas as pd
import matplotlib.pyplot as plt
from shapely.geometry import LineString
from colorama import init, Fore
from pathlib import Path

import scripts.src.inputs.config as cfg
from scripts.src.helpers import (
    parse_nodes,
    parse_edges,
    _filter_by_bbox,
    _get_opposite_direction,
)

init(autoreset=True)


### 1. SPECIFIC HELPER FUNCTIONS
# Matching basing on minimal distances among edges
def find_best_match(
    csv_segment: list[tuple[float, float]], edges, distance_threshold: float = 6.5e-5
):  # 6.5e-5 = 7,22m
    """
    Find the best match between a segment of the CSV database and net SUMO's edges, basing on the smallest possible distance
    """
    csv_line = LineString(csv_segment)  # csv row as input
    best_match = None
    best_score = float("inf")

    for edge in edges.values():
        shape = edge.get("shape")
        if not shape or len(shape) < 2:  # avoid edges with invalid shape
            continue

        # Create a variable for each edge in the edg.xml file and compute the distance from each CSV edge
        edge_line = LineString(shape)
        distance = csv_line.distance(
            edge_line
        )  # from Shapely: object.distance() Returns the minimum distance (float) to the other geometric object.
        # Update the matching scores
        if distance < best_score:
            best_score = distance
            best_match = edge

    # Apply threshold for acceptance criteria
    if best_match is not None and best_score < distance_threshold:
        return best_match, best_score
    else:
        return None, best_score


### 2. BUILD DATABASE AND UPDATE MISSING VALUES


def build_database(
    filtered_db: pd.DataFrame,
    coord_to_node: dict,
    edges: dict,
    threshold: float = 6.5e-5,
    output_dir: Path = ".",
) -> pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame:
    """
    Build a new database with SUMO data, perform map-matching and finally update dataframe entries.
    """

    # Map ID node - data ->  {"id": ..., "type": ..., "coords": (lat, lon)}
    id_to_node_data = {}
    # coord_to_node: {"(lat, lon)": id, type}
    for coords, data in coord_to_node.items():
        node_id = data["id"]
        id_to_node_data[node_id] = {
            "coords": coords,  # (lat, lon)
            "type": data.get("type"),
        }

    # Base list with all the edges and their data
    sumo_base_data = []
    # BUILD DEFAULT DATABASE
    for edge_id, edge_data in edges.items():
        type_edge = edge_data["type"]
        street_length = round(float(edge_data.get("length", 0)), 2)
        opposite_dir = _get_opposite_direction(edge_id, edge_data, edges)
        oneway = 1 if opposite_dir is None else 0
        # Set SUMO edges IDs
        from_sumo_id = edge_data.get("from", "N/A")
        to_sumo_id = edge_data.get("to", "N/A")
        # Set default coordinates and types (either SUMO nodes or None values)
        from_node_data = id_to_node_data.get(
            from_sumo_id, {"coords": (None, None), "type": None}
        )
        to_node_data = id_to_node_data.get(
            to_sumo_id, {"coords": (None, None), "type": None}
        )
        from_lat, from_lon = from_node_data["coords"]
        to_lat, to_lon = to_node_data["coords"]
        # Extract default intersection type
        from_inter_type = from_node_data["type"]
        to_inter_type = to_node_data["type"]

        base_row = {
            "Edge SUMO ID": edge_id,
            "Street Name": edge_data.get("name", ""),
            "Root SUMO ID": edge_id.split("#")[0] if "#" in edge_id else edge_id,
            "From SUMO ID": from_sumo_id,
            "To SUMO ID": to_sumo_id,
            "From Latitude": round(from_lat, 6),  # to be updated
            "From Longitude": round(from_lon, 6),  # to be updated
            "To Latitude": round(to_lat, 6),  # to be updated
            "To Longitude": round(to_lon, 6),  # to be updated
            "From Intersection Type": from_inter_type,  # to be updated
            "To Intersection Type": to_inter_type,  # to be updated
            "Street Type": type_edge,
            "Street Priority": edge_data.get("priority", ""),
            "Number Lanes": edge_data.get("numLanes", ""),
            "Directions": "One-way" if oneway == 1 else "Two-way",
            "Opposite Direction": opposite_dir if oneway == 0 else "NaN",
            "Random Speed [m/s]": float(edge_data.get("speed", 0)),
            "Average Velocity Flow 0 [m/s]": 0.0,  # to be filled
            "Travel Time Flow 0 [s]": 0.0,  # to be filled
            "Street Length [m]": street_length,
            "Zone": None,  # to be filled
            "has Parking": True if type_edge == "highway.service" else False,
            "Match Found": False,  # to track matches
        }
        sumo_base_data.append(base_row)

    print(f"len sumo_base_data:{len(sumo_base_data)}")
    full_db = pd.DataFrame(sumo_base_data).set_index("Edge SUMO ID")

    matched_csv_indices = set()
    matches_found = 0
    no_matches_csv = []

    # SUMO edges data update
    for idx, row in filtered_db.iterrows():
        if idx in matched_csv_indices:
            continue
        # Get coordinates from CSV
        from_lat_csv, from_lon_csv = float(row["From Latitude"]), float(
            row["From Longitude"]
        )
        to_lat_csv, to_lon_csv = float(row["To Latitude"]), float(row["To Longitude"])
        csv_segment = [(from_lon_csv, from_lat_csv), (to_lon_csv, to_lat_csv)]

        # MATCHING LOGIC
        best_edge, dist = find_best_match(
            csv_segment, edges, distance_threshold=threshold
        )
        
        if best_edge is not None:
            edge_id = best_edge["id"]
            if edge_id in full_db.index:
                full_db.loc[edge_id, "Match Found"] = True
                full_db.loc[edge_id, "From Latitude"] = from_lat_csv
                full_db.loc[edge_id, "From Longitude"] = from_lon_csv
                full_db.loc[edge_id, "To Latitude"] = to_lat_csv
                full_db.loc[edge_id, "To Longitude"] = to_lon_csv
                full_db.loc[edge_id, "From Intersection Type"] = row[
                    "From Intersection Type"
                ]
                full_db.loc[edge_id, "To Intersection Type"] = row[
                    "To Intersection Type"
                ]
                full_db.loc[edge_id, "Average Velocity Flow 0 [m/s]"] = row.get(
                    "Average Velocity Flow 0 [m/s]", 0
                )
                full_db.loc[edge_id, "Travel Time Flow 0 [s]"] = row.get(
                    "Travel Time Flow 0 [s]", 0
                )
                full_db.loc[edge_id, "Zone"] = row["Zone"]

                matches_found += 1
                matched_csv_indices.add(idx)

        else:
            # Check for non-matched segments
            no_matches_csv.append(
                {
                    "Street Name CSV": row.get("Street Name", ""),
                    "From Latitude": from_lat,
                    "To Latitude": to_lat,
                    "From Longitude": to_lat,
                    "To Longitude": to_lon,
                    "CSV From/To Latitude": (row["From Latitude"], row["To Latitude"]),
                    "CSV From/To Longitude": (
                        row["From Longitude"],
                        row["To Longitude"],
                    ),
                    "Reason": f"No geometric match (min_dist={dist:.6f})",
                }
            )

    matched_df = full_db[full_db["Match Found"]].copy().reset_index()
    missing_sumo_df = (
        full_db[~full_db["Match Found"]].copy().reset_index()
    )  # ~ (from pd) per inversione valori (prende i "Match Found" == False)

    matched_path = os.path.join(output_dir, "matched_enriched.csv")
    unmatched_csv_path = os.path.join(output_dir, "csv_unmatched.csv")
    full_db_path = os.path.join(output_dir, "full_sumo_db.csv")

    full_db.reset_index().to_csv(full_db_path, sep=";", index=False)
    matched_df.to_csv(matched_path, sep=";", index=False)
    pd.DataFrame(no_matches_csv).to_csv(unmatched_csv_path, sep=";", index=False)

    # Debug and threshold diagnostics
    print(f"Complete db in: {full_db_path}")
    print(f"Matches saved in: {matched_path}")
    # print(f"No match: {unmatched_csv_path}")
    print(f"\nNumber of matches: {matches_found}")
    print(f"Number of non-matched: {len(no_matches_csv)}")

    return full_db, matched_df, pd.DataFrame(no_matches_csv), missing_sumo_df


def update_missing_values(full_db:pd.DataFrame, edges:dict) -> pd.DataFrame:
    """
    Find missing values for unmatched edges by taking the average flow 0 values in the matched neighbourhood as a reference.
    (Fallback: average value by street type). Zone updated by nearest matched edge.
    """
    # (fallback) Compute average velocity by street type among matches
    matched_data = full_db[full_db["Match Found"] == True]
    # Group by street type, compute the mean and create a dictionary (street type - mean value)
    avg_flow0_by_type = (
        matched_data.groupby("Street Type")["Average Velocity Flow 0 [m/s]"]
        .mean()
        .to_dict()
    )

    # map for ditances: Edge SUMO ID -> LineString Object
    matched_edge_lines = {}
    for edge_id, row in matched_data.iterrows():  # iterate over df
        sumo_edge = edges.get(edge_id)
        if sumo_edge and sumo_edge.get("shape"):
            matched_edge_lines[edge_id] = LineString(sumo_edge["shape"])

    # Edges reporting missing data
    missing_edges_ids = full_db[full_db["Match Found"] == False].index.tolist()
    missing_count = 0
    for edge_id in missing_edges_ids:
        edge_data = full_db.loc[edge_id]
        street_type = edge_data["Street Type"]
        sumo_speed = edge_data["Random Speed [m/s]"]
        length = edge_data["Street Length [m]"]

        # Update data based on neighbouring (matched) CSV values
        avg_velocity = avg_flow0_by_type.get(street_type, None)

        if avg_velocity is not None:
            avg_velocity = round(avg_velocity, 2)

        if pd.isna(avg_velocity) or avg_velocity == 0:
            # Fallback to random SUMO velocity
            avg_velocity = (
                sumo_speed if sumo_speed > 0 else 5.56
            )  # Default for service roads

        full_db.loc[edge_id, "Average Velocity Flow 0 [m/s]"] = avg_velocity

        # Update Time Flow 0
        if avg_velocity:
            travel_time = round((length / avg_velocity), 2)
        else:
            travel_time = 0.0

        full_db.loc[edge_id, "Travel Time Flow 0 [s]"] = travel_time
        missing_count += 1

        # Zone assignment
        unmatched_sumo_edge = edges.get(edge_id)
        best_zone = None
        # Get shapes of unmatched
        if unmatched_sumo_edge and unmatched_sumo_edge.get("shape"):
            unmatched_line = LineString(unmatched_sumo_edge["shape"])
            min_dist = float("inf")
            # Search by geometric distance
            for matched_id, matched_line in matched_edge_lines.items():
                try:
                    distance = unmatched_line.distance(matched_line)
                    if distance < min_dist:
                        min_dist = distance
                        best_zone = full_db.loc[matched_id, "Zone"]
                except Exception:
                    continue

        if best_zone:
            zone = best_zone
        else:
            # Fallback
            zone = "NaN"

        full_db.loc[edge_id, "Zone"] = zone

    print(f"\n{missing_count} edges have been updated.")
    return full_db


### 3. PLOTTING FUNCTIONS


def plot_matches(
    coord_to_node:dict,
    edges:dict,
    matched_df,
    unmatched_df=None,
    missing_sumo_df=None,
    output_path=None,
):

    plt.figure(figsize=(10, 10))
    plt.title(
        "Matching edge SUMO -  CSV segment (green=match, red=not matched, yellow=missing SUMO)"
    )
    plt.xlabel("Longitudine")
    plt.ylabel("Latitudine")
    plt.grid(True, alpha=0.3)

    """coord_to_node = (lat, lon) : {"id":..., "type":...}"""
    id_to_coord = {}
    for key, value in coord_to_node.items():
        nid = value.get("id")
        if nid:
            id_to_coord[value["id"]] = key

    for edge_id, edge_data in edges.items():
        if edge_data and edge_data.get("shape"):
            # shape = [(lon, lat), ...]
            lon_coords = [p[0] for p in edge_data["shape"]]
            lat_coords = [p[1] for p in edge_data["shape"]]

            plt.plot(
                lon_coords, lat_coords, color="gray", linewidth=0.4, alpha=0.3, zorder=1
            )

    matched_sumo_ids = set(matched_df["Edge SUMO ID"].tolist())

    # Iterating over the original edges
    for edge_id, edge_data in edges.items():
        # Look for original SUMO shape
        if edge_data and edge_data.get("shape"):
            lon_coords = [p[0] for p in edge_data["shape"]]
            lat_coords = [p[1] for p in edge_data["shape"]]
            # if match = green
            if edge_id in matched_sumo_ids:
                plt.plot(
                    lon_coords,
                    lat_coords,
                    color="green",
                    linewidth=1.8,
                    alpha=0.9,
                    zorder=4,
                )
            # otherwise, look for missing ids to draw them in orange
            elif edge_id in missing_sumo_df["Edge SUMO ID"].tolist():
                plt.plot(
                    lon_coords,
                    lat_coords,
                    color="orange",
                    linewidth=1.5,
                    alpha=0.8,
                    zorder=3,
                )

    plt.plot([], [], color="orange", linewidth=1.5, label="Edge SUMO (Missing in CSV)")
    plt.plot([], [], color="green", linewidth=1.8, label="Matchati")

    # Draw unmatched in red
    if unmatched_df is not None and not unmatched_df.empty:
        for idx, row in unmatched_df.iterrows():
            plt.plot(
                [row["From Longitude"], row["To Longitude"]],
                [row["From Latitude"], row["To Latitude"]],
                color="red",
                linewidth=1.3,
                alpha=0.9,
                zorder=5,
            )
        plt.scatter(
            unmatched_df["From Longitude"],
            unmatched_df["From Latitude"],
            color="red",
            s=10,
            label="Non matchati CSV",
            alpha=0.9,
            zorder=6,
        )

    plt.plot([], [], color="gray", linewidth=0.4, label="Rete SUMO")
    plt.legend(loc="upper left")
    if output_path:
        plt.savefig(output_path, dpi=300)
        print(f"\n Plot salvato in: {output_path}")
    else:
        plt.show()


### 5. MAIN


def main():
    edg_path = cfg.EDG_PARSE_XML
    nod_path = cfg.NOD_PARSE_XML
    csv_input = cfg.DATA_PATH / "Torino_RoadTopografy_v5.csv"
    output_dir = cfg.OUTPUT_BASE / "database"

    os.makedirs(output_dir, exist_ok=True)
    new_db_path = os.path.join(output_dir, "DB_RoadTopography.csv")

    db = pd.read_csv(csv_input, sep=";")
    coord_to_node = parse_nodes(nod_path)
    edges = parse_edges(edg_path)

    # Filter by bounding box boundaries (see helpers.py) - to restrict the domain and diminish the computational cost
    filtered_db = _filter_by_bbox(db)

    # Matching phase and dataframe enrichment
    full_db, matched_df, unmatched_csv_df, missing_sumo_df = build_database(
        filtered_db, coord_to_node, edges, threshold=6.5e-5, output_dir=output_dir
    )

    node_to_edge_map = {}
    for edge_id, edge_data in edges.items():
        from_node = edge_data.get("from")
        to_node = edge_data.get("to")
        # Check if nodes exist and are not undefined
        if from_node and from_node != "N/A":
            node_to_edge_map.setdefault(from_node, []).append(edge_id)
        if to_node and to_node != "N/A":
            node_to_edge_map.setdefault(to_node, []).append(edge_id)

    # Update missing values
    full_db_updated = update_missing_values(full_db.copy(), edges)

    plot_matches(
        coord_to_node,
        edges,
        matched_df=matched_df,
        unmatched_df=unmatched_csv_df,
        missing_sumo_df=missing_sumo_df,
        output_path=os.path.join(output_dir, "matched_combined_plot.png"),
    )

    full_db_updated.reset_index().to_csv(new_db_path, sep=";", index=False)
    print(Fore.GREEN + f"Database saved in: {new_db_path}")


if __name__ == "__main__":
    main()