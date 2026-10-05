"""
This script is used to implement an ad hoc alorithm to produce realistic ODs matrices for a daily SUMO simulation on the map.
"""

import os
from dotenv import load_dotenv
from typing import Dict
from pathlib import Path
from colorama import Fore, init

import numpy as np
import pandas as pd
from scripts.src.operations.connections import _get_pasta_data

init(autoreset=True)
AM_PEAK = 8
PM_PEAK = 18  # slightly less than the morning peak
# total_am = df_morning['Flow'].sum() = 27975.968
# total_pm = df_evening['Flow'].sum() = 23196.106

def _load_ods(flows_path: str) -> pd.DataFrame:
    flows = pd.read_csv(flows_path, sep=r"\s+", header=None, comment="*", names=["From", "To", "Flow"])
    flows = flows.iloc[3:].reset_index(drop=True)
    flows["Flow"] = pd.to_numeric(flows["Flow"], errors="coerce") # force numeric datatype on flows
    flows[["From", "To"]] = flows[["From", "To"]].astype(int)
    return flows

def __alpha(hour: float) -> float:
    # alpha(8) = 1, alpha(17) = 0
    t = (hour - AM_PEAK) / (PM_PEAK - AM_PEAK)
    if t < 1:
        a = 1-abs(t)
    else:
        a = t-1
    return a

def __beta(hour: float) -> float:
    return 1 - __alpha(hour)


def _scale_factor(data: Path = None) -> tuple[Dict[int, float], float]:
    """
    Scale factor [0,1]: how much traffic at this hour relative to peak.
    """
    if data is not None:
        flows = pd.read_csv(data)
    else:
        load_dotenv()
        connection_strings = {
            "ista": os.getenv("ISTA_URL"),
            "istc": os.getenv("ISTC_URL"),
        }
        if not connection_strings["ista"]:
            raise ValueError(Fore.RED + "ISTA_URL not in .env file")
        if not connection_strings["istc"]:
            raise ValueError(Fore.RED + "ISTC_URL not in .env file")
        
        _, flows = _get_pasta_data(connection_strings["ista"], connection_strings["istc"])
        
    day_hour_flows = flows.groupby(["hour", "daytime"])["count_all"].sum()
    hourly_mean_counts = day_hour_flows.groupby("hour").mean()
    peak = hourly_mean_counts.max()
    k_perc = round((hourly_mean_counts / peak), 3).to_dict()

    return k_perc, float(peak)


def _write_od_files(hour_matrices, output_dir_data):
    for hour, matrix in hour_matrices.items():
        output_dir_data.mkdir(parents=True, exist_ok=True)
        with open(Path(output_dir_data, f"h0{hour}.mtx" if hour < 10 else f"h{hour}.mtx"), "w", encoding="utf-8",) as file:
            file.write(
                f"$O;D3\n* From-time To-time\n{hour}.00 {hour+1}.00\n* Factor\n1.00\n*\n* 5T srl Gruppo GTT Torino\n* 03/04/26\n"
            )
            for _, row in matrix.iterrows():
                file.write(
                    f"{int(row['From'])}\t{int(row['To'])}\t{round(row['Flow'], 2)}\n"
                )


def generate_hour_matrices(
    od_morning: str, od_evening: str, input_data: Path, output_dir_data: Path, demand_scale: float = 1.0
) -> Dict[int, pd.DataFrame]:

    df_morning = _load_ods(od_morning)
    df_evening = _load_ods(od_evening)
    df_combined = pd.merge(df_morning, df_evening, on=["From", "To"], how="outer", suffixes=("_AM", "_PM"))

    df_combined["Flow_AM"] = df_combined["Flow_AM"].fillna(0.0)
    df_combined["Flow_PM"] = df_combined["Flow_PM"].fillna(0.0)

    k_perc, _ = _scale_factor(input_data)
    # print(f"Peaks: AM = {df_combined["Flow_AM"].sum()}, PM = {df_combined["Flow_PM"].sum()}")

    hour_matrices = {}

    for hour, perc in k_perc.items():
        a = __alpha(hour)
        b = __beta(hour)
        shape_factor = perc / (a * k_perc[AM_PEAK] + b * k_perc[PM_PEAK])
        interpol_mat = df_combined[["From", "To"]].copy()
        interpol_mat["Flow"] = shape_factor * (
            a * df_combined["Flow_AM"] + b * df_combined["Flow_PM"]
        )
        interpol_mat["Flow"] *= demand_scale
        hour_matrices[hour] = interpol_mat

    accumulator = []
    for hour, matrix in hour_matrices.items():
        sum_hour = matrix['Flow'].sum()
        print(f"hour {hour}: flow {sum_hour:.0f}")
        accumulator.append(sum_hour)
    
    print(f"Total vehicles: {sum(accumulator)}")
    
    _write_od_files(hour_matrices, output_dir_data)
    return hour_matrices