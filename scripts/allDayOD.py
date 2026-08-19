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

# from scripts.sumoResults import (
#     parse_sumo_summary,
#     summary_analysis,
# )

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

    # run_duarouter(net_file, ",".join(trips_list), routes_output, _OD_DUAROUTER_ARGS)

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


### GENERATE CONFIGURATION
def _generate_daily_configuration(
    net_file: Path,
    taz_file: str,
    trips_list: List[Path],
    output_dir: str,
    output_sumo: str,
    config_name: str,
    # taz_rel: Path = None,
    routes_output: Path = cfg.OD_ROUTES["DAY"],
    detectors: Path = None,
) -> tuple[str, str]:

    routes = run_duarouter(
        net_file=net_file,
        trips_input=",".join(trips_list),
        routes_output=routes_output,
        extra_args=_OD_DUAROUTER_ARGS,
    )

    # Configuration
    config_file = CfgAttributes(
        net=net_file,
        routes=routes_output,
        output_cfg=output_dir,
        output_sumo=output_sumo,
        config_name=config_name,
        teleport="60",
        setting=cfg.VIEW
    ).build(
        method="duarouter",
        taz=taz_file,
        begin=0,
        end=86400,
        detectors=detectors,
    )

    return config_file, routes


# TODO: sistemare sumo_results e richiamare funzione qua
# ### PLOT PROFILE
# def plot_profile_day(sumo_summary_path: Path, output_dir: Path):
#     summary = parse_sumo_summary(sumo_summary_path)
#     summary_analysis(summary, output_dir)


def main():

    SCALE_FACTOR = 0.8

    net = cfg.NET_FILE
    taz = cfg.TAZ
    tazrel_day = cfg.TAZREL["DAY"]
    output_dir_cfg = cfg.CFG_DIR

    od_morning = cfg.OD_MATRICES["AM"]
    od_evening = cfg.OD_MATRICES["PM"]

    output_trips = cfg.OD_TRIPS_DIR["DAY"]
    data_dir = cfg.OD_MATRICES["DAY"] / f"scaled_{SCALE_FACTOR}"
    output_day_simul = cfg.OUTPUT_DASHBOARDS / "out-DataExtr-OD-allDay"

    working_dir_ue_day = cfg.WORKDIRS["DUA"]["DAY"]
    working_dir_sue_day_logit = cfg.WORKDIRS["LOGIT"]["DAY"]
    working_dir_sue_day_gawron = cfg.WORKDIRS["GAWRON"]["DAY"]
    workdir_ma_no_tls = cfg.WORKDIRS["MA_no_TLS"]["DAY"]
    workdir_ma_with_tls = cfg.WORKDIRS["MA_with_TLS"]["DAY"]

    with cProfile.Profile() as pr:

        cfg.create_project_structure()
        for d in [
            data_dir,
            output_trips,
            output_day_simul,
            working_dir_ue_day,
            working_dir_sue_day_logit,
            working_dir_sue_day_gawron,
            workdir_ma_no_tls,
            workdir_ma_with_tls,
        ]:
            Path(d).mkdir(parents=True, exist_ok=True)

        generate_hour_matrices(od_morning, od_evening, data_dir, SCALE_FACTOR)

        trips_list = _day_scenario_trips(
            taz_file=taz, od_folder=data_dir, out_dir=output_trips
        )

        # for s in ["base_no_TLS", "base_with_TLS"]:
        #     _generate_daily_configuration(
        #         net_file=net,
        #         taz_file=taz,
        #         trips_list=trips_list,
        #         output_dir=output_dir_cfg,
        #         output_sumo=cfg.SIM_OUT[s]["DAY"],
        #         config_name=f"francia_peschiera_{s}_scaled{SCALE_FACTOR}_DAY.sumocfg",
        #         detectors=cfg.DETECTORS[s]["DAY"],
        #     )

        list_day_matrices = sorted(data_dir.glob("h*.mtx"))
        od_matrices_str = ",".join(str(file) for file in list_day_matrices)
        # taz_rel_day = write_taz_relations(list_day_matrices, tazrel_day.parent)

        # ONE FOR 24 H
        od_matrices_24h = {
            h: list_day_matrices[h] for h in range(len(list_day_matrices))
        }
        taz_rel_24h = write_taz_relations_24h(od_matrices_24h, tazrel_day.parent)

        # MAROUTER - Macroscophic routing
        print("\nSTARTING MAROUTER - DAY SCENARIO")
        for s in ["MA_no_TLS", "MA_with_TLS"]:
            print(Fore.GREEN + f"\tScenario {" ".join(s[3:].split("_"))}")
            routes_day_marouter = run_marouter(
                net_file=Path(net),
                od_matrices=od_matrices_str,
                taz_file=Path(taz),
                trips_output=os.path.join(
                    cfg.WORKDIRS[s]["DAY"], "od_trips_file.odtrips.xml"
                ),
                logit_theta=0.01,
                logit_beta=0.3,
                route_choice="logit",
                # taz_rel=Path(taz_rel_24h),
                data_dir=data_dir,
                additional_files=[cfg.DETECTORS[s]["DAY"], cfg.VTYPE],
                out_dir=cfg.WORKDIRS[s]["DAY"],
                netload_output=Path(cfg.WORKDIRS[s]["DAY"], "netload_ouput.xml"),
                paths=15,
                path_penalty=25.0,
                weights_priority=0.4,
                max_alternatives=10,
                max_iterations=200,
                tolerance=0.5,
                method="SUE",
                begin=0,
                end=24 * 3600,
            )

            routes_day_ma_clean = filter_short_flows(
                routes_day_marouter,
                output_new=Path(cfg.WORKDIRS[s]["DAY"])
                / "marouter_output_clean.rou.xml",
            )
            
            routes_final = filter_zero_prob(routes_day_ma_clean)

            CfgAttributes(
                net=net,
                routes=routes_final,
                output_cfg=output_dir_cfg,
                output_sumo=cfg.SIM_OUT[s]["DAY"],
                config_name=f"francia_peschiera_MAROUTER_{s[3:]}_DAY.sumocfg",
                teleport="60",
                meso=False,
                setting=cfg.VIEW
            ).build(
                method="marouter",
                taz=taz,
                begin=0,
                end=86400,
                detectors=cfg.DETECTORS[s]["DAY"],
            )

            print("Done\n")

    results = pstats.Stats(pr)
    results.sort_stats(pstats.SortKey.TIME)
    # results.print_stats()


if __name__ == "__main__":
    main()