"""
@file       allDayOD.py
@author     Irene Marta
@date       2026

This script is used to implement an ad hoc alorithm to produce realistic ODs matrices
for a daily SUMO simulation on the map.

#### descrizione parametri

Firstly, the algorithm is performed and finally the simulation is ran.
"""

import os
from dotenv import load_dotenv
from typing import Dict, List
from pathlib import Path

import cProfile
import pstats
from colorama import Fore, init

import numpy as np
import pandas as pd

import scripts.src.inputs.config as cfg
from scripts.src.operations.cmd import _ensure_sumo_home, _od2trips
from scripts.src.operations.connections import _get_pasta_data
from scripts.src.modules.entities import CfgAttributes
from scripts.src.helpers import write_taz_relations_24h
from scripts.src.operations.filtering import filter_zero_prob


from scripts.src.operations.cmd import (
    #_day_scenario_trips,
    # run_simulation,
    run_duarouter,
    run_marouter,
    _OD_DUAROUTER_ARGS,
)

from scripts.src.operations.filtering import filter_short_flows

init(autoreset=True)
AM_PEAK = 8
PM_PEAK = 18  # slightly less than the morning peak
# total_am = df_morning['Flow'].sum() = 27975.968
# total_pm = df_evening['Flow'].sum() = 23196.106


def __alpha(hour: float) -> float:
    # alpha(8) = 1, alpha(17) = 0
    return float(
        np.clip(
            (
                1 - abs((hour - AM_PEAK) / (PM_PEAK - AM_PEAK))
                if hour - PM_PEAK < 0
                else abs((hour - PM_PEAK) / (PM_PEAK - AM_PEAK))
            ),
            0,
            1,
        )
    )  # np.clip(value, min, max)


def __beta(hour: float) -> float:
    return 1 - __alpha(hour)


def _scale_factor(data: Path = None) -> tuple[Dict[int, float], float]:
    """
    Scale factor [0,1]: how much traffic at this hour relative to peak.
    Based on PASTA data

    """
    if data is not None:
        flows = pd.read_csv(data)
    else:
        load_dotenv()  # reads variables from a .env file and sets them in os.environ
        connection_strings = {
            "ista": os.getenv("ISTA_URL"),
            "istc": os.getenv("ISTC_URL"),
        }
        if not ista_url or not istc_url:
            raise ValueError("ERROR: ISTA_URL or ISTC_URL not in .env file")
        
        _, flows = _get_pasta_data(connection_strings["ista"], connection_strings["istc"])
        # real_data = pasta_db_merge(anagraphics, flows)
        
    day_hour_flows = flows.groupby(["hour", "daytime"])["count_all"].sum()
    hourly_mean_counts = day_hour_flows.groupby("hour").mean()
    peak = hourly_mean_counts.max()
    k_perc = round((hourly_mean_counts / peak), 3).to_dict()

    return k_perc, float(peak)


def _load_ods(path: str) -> pd.DataFrame:
    df = pd.read_csv(
        path, sep=r"\s+", header=None, comment="*", names=["From", "To", "Flow"]
    )  # for one or more whitespaces: sep='\s+'
    df = df.iloc[3:].reset_index(drop=True)
    print(df.head)

    # Force numeric datatype on flows (in case of strings)
    df["Flow"] = pd.to_numeric(df["Flow"], errors="coerce")
    df[["From", "To"]] = df[["From", "To"]].astype(int)

    return df


def _day_scenario_trips(
    taz_file: Path,
    od_folder: Path,
    out_dir: Path,
    output_suffix="_daily",
) -> List[Path]:
    _ensure_sumo_home()

    trips_list = []

    for od_file in sorted(
        (f for f in od_folder.iterdir() if f.suffix == ".mtx"),
        key=lambda f: int(f.stem[1:3])
    ):
        hour = od_file.name[1:3]
        start = int(hour) * 3600
        trips_output = os.path.join(
            out_dir, f"od_trips_h{hour}{output_suffix}.odtrips.xml"
        )

        _od2trips(
            taz_file,
            od_file,
            trips_output,
            extra_args=[
                "-b",
                str(start),
                "-e",
                str(start + 3600),
                "--prefix",
                f"h{hour}_",
            ],
        )
        trips_list.append(str(trips_output))

    return trips_list


def generate_hour_matrices(
    od_morning: str, od_evening: str, input_data: Path, output_dir_data: Path, demand_scale: float = 1.0
) -> Dict[int, pd.DataFrame]:

    df_morning = _load_ods(od_morning)
    df_evening = _load_ods(od_evening)

    df_combined = pd.merge(
        df_morning, df_evening, on=["From", "To"], how="outer", suffixes=("_AM", "_PM")
    )

    df_combined["Flow_AM"] = df_combined["Flow_AM"].fillna(0.0)
    df_combined["Flow_PM"] = df_combined["Flow_PM"].fillna(0.0)

    k_perc, _ = _scale_factor(input_data)
    print(
        f"Peaks: AM = {df_combined["Flow_AM"].sum()}, PM = {df_combined["Flow_PM"].sum()}"
    )

    k_perc_am = k_perc[AM_PEAK]
    k_perc_pm = k_perc[PM_PEAK]

    hour_matrices = {}

    for hour, perc in k_perc.items():
        a = __alpha(hour)
        b = __beta(hour)
        """
        NB: 
        I VOLUMI DI PASTA E DELLE OD PER I PICCI SONO MOLTO DIVERSI
        -> tengo in considerazione l'ANDAMENTO % di PASTA e i VALORI delle OD
        """
        shape_factor = perc / (a * k_perc_am + b * k_perc_pm)
        interpol_mat = df_combined[["From", "To"]].copy()
        interpol_mat["Flow"] = shape_factor * (
            a * df_combined["Flow_AM"] + b * df_combined["Flow_PM"]
        )
        interpol_mat["Flow"] *= demand_scale
        hour_matrices[hour] = interpol_mat

    accumulator = []
    for hour, matrix in hour_matrices.items():
        sum_hour = matrix['Flow'].sum()
        print(f"ora {hour}: {sum_hour:.0f}")
        accumulator.append(sum_hour)
    
    print(f"Total vehicles: {sum(accumulator)}")

    for hour, matrix in hour_matrices.items():
        output_dir_data.mkdir(parents=True, exist_ok=True)
        with open(
            Path(output_dir_data, f"h0{hour}.mtx" if hour < 10 else f"h{hour}.mtx"),
            "w",
            encoding="utf-8",
        ) as file:
            file.write(
                f"$O;D3\n* From-time To-time\n{hour}.00 {hour+1}.00\n* Factor\n1.00\n*\n* 5T srl Gruppo GTT Torino\n* 03/04/26\n"
            )
            for _, row in matrix.iterrows():
                file.write(
                    f"{int(row['From'])}\t{int(row['To'])}\t{round(row['Flow'], 2)}\n"
                )