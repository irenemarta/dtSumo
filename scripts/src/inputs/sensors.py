"""
@file       sensors.py
@author     Irene Marta
@date       2026

This script aims at analyzing data collected both on PASTA and BRIDGE databases, as two different comparable sources of traffic data, and at comparing them to
simulation results (SUMO summary.xml) to evaluate the quality of the simulation and the real-scenarios representativeness of the simulated model.

The script is structured as follows:
1. BRIDGE API interface data extraction
2. PASTA database data extraction
3. Data aggregation
4. Definition of functions to plot and compare data
5. SUMO output (summary.xml) analysis and dashboard creation.

Data is organised following the DataFrameSchemaPasta/DataFrameSchemaMerge.
Take hourly (count, speed) observations for one sensor across many days.
"""

import os
from dotenv import load_dotenv
from pathlib import Path

from numpy import float64
from typing import List, Tuple
import matplotlib.pyplot as plt
import seaborn as sns
import geopandas as gpd
import contextily as cx
import matplotlib.pyplot as plt

import pandas as pd
import pandera.pandas as pa
from pandera.typing import Series

from scripts.src.operations.connections import _get_pasta_data
import scripts.src.inputs.config as cfg

sns.set_theme(style="whitegrid")
DATE_FORMAT = "%Y-%m-%d"
HOUR_FORMAT = "{:02d}:00"

"""
The Supervisor (SV) uses fixed convertion factors to convert all types of vehicles data in Equivalent Vehicles (EQ)
-> convert vehicle types to EQ using fixed factors
"""
VEHICLE_CONVERSION_FACTORS = {
    "Furgone": 2,
    "Camion": 3,
    "Autobus": 3,
    # "Moto": 0.5  # Commented out as per original
}

PALETTES = {"primary": "viridis", "secondary": "coolwarm", "speeds": "colorblind"}

### HELPERS


# STANDARDIZE DATAFRAMES WITH SENSOR DATA
def standardize_datetime(df: pd.DataFrame, date_col: str, DATE_FORMAT) -> pd.DataFrame:
    """Standardize datetime in the format YYYY-MM-DD."""
    df = df.copy()
    df[date_col] = (
        pd.to_datetime(df[date_col], dayfirst=True)
        .dt.strftime(DATE_FORMAT)
        .astype(object)
    )
    return df


def format_hour_column(df: pd.DataFrame, hour_col: str = "hour") -> pd.DataFrame:
    """Standard hour format 'HH:00'."""
    df = df.copy()
    df[hour_col] = df[hour_col].astype(str).str.zfill(2) + ":00"
    return df


# Database integrity
class DataFrameSchemaBridge(pa.DataFrameModel):
    # Common columns definitions and constraints
    sezione: Series[int] = pa.Field(gt=0)
    name: Series[str]
    hour: Series[object] = pa.Field(str_matches=r"^\d{2}:00$")  # HH:00 format
    daytime: Series[str] = pa.Field(
        str_matches=r"^\d{4}-\d{2}-\d{2}$"
    )  # YYYY-MM-DD format
    # Column definitions and constraints for BRDIGE
    BRIDGE_count: Series[int] = pa.Field(ge=0)


class DataFrameSchemaPasta(pa.DataFrameModel):
    # Common columns definitions and constraints
    sezione: Series[int] = pa.Field(gt=0)
    name: Series[str]
    hour: Series[object] = pa.Field(str_matches=r"^\d{2}:00$")  # HH:00 format
    daytime: Series[str] = pa.Field(
        str_matches=r"^\d{4}-\d{2}-\d{2}$"
    )  # YYYY-MM-DD format
    # Column defintiions and constraints for PASTA
    Cod_sens: Series[int] = pa.Field(gt=0)
    strada: Series[str]
    direction: Series[str]
    lat: Series[float64]
    lon: Series[float64]
    disponibile: Series[bool] = pa.Field(isin=[0, 1])
    PASTA_count: Series[int] = pa.Field(ge=0)
    AVG_accuracy: Series[float64] = pa.Field(ge=0)
    AVG_speed: Series[float64] = pa.Field(ge=0)

    class Config:
        strict = True  # if True, error for extra columns not defined
        coerce = True  # convertes automatically if wrong type


class DataFrameSchemaMerge(pa.DataFrameModel):
    # Common columns definitions and constraints
    sezione: Series[int] = pa.Field(gt=0)
    name: Series[str]
    hour: Series[object] = pa.Field(str_matches=r"^\d{2}:00$")  # HH:00 format
    daytime: Series[str] = pa.Field(
        str_matches=r"^\d{4}-\d{2}-\d{2}$"
    )  # YYYY-MM-DD format
    # Column definitions and constraints for merged dataframe
    BRIDGE_count: Series[int] = pa.Field(ge=0)
    PASTA_count: Series[int] = pa.Field(ge=0)
    Cod_sens: Series[int] = pa.Field(gt=0)
    strada: Series[str]
    direction: Series[str]
    lat: Series[float64]
    lon: Series[float64]
    disponibile: Series[bool] = pa.Field(isin=[0, 1])
    AVG_accuracy: Series[float64] = pa.Field(ge=0)
    AVG_speed: Series[float64] = pa.Field(ge=0)

    class Config:
        strict = True  # if True, error for extra columns not defined
        coerce = True  # convertes automatically if wrong type


#### 1.BRIDGE API interface (MOBILITY 129)


def bridge_db(file_bridge: Path) -> Tuple[pd.DataFrame, pd.DataFrame]:
    print("\tCreating BRIDGE Database")
    # The analysis considers a typical week of data - 03.11.2025-06.11.2025
    df_bridge = pd.read_csv(file_bridge, sep=";")

    df_bridge.columns = df_bridge.columns.str.strip().str.capitalize()
    # columns: (['Camera', 'Camera id', 'Ip camera', 'Linea', 'Linea.1', 'Tipo veicolo','Numero passaggi', 'Data di inizio', 'Ora di inizio','Giorno di inizio']
    # Drop unnecessary columns
    df_bridge = df_bridge.drop(columns={"Ip camera", "Linea", "Linea.1"})
    df_bridge["Data di inizio"] = pd.to_datetime(
        df_bridge["Data di inizio"], dayfirst=True
    )

    # Standard PASTA format to establish an homogeneus nomenclature
    df_bridge = df_bridge.rename(
        columns={
            "Camera id": "sezione",
            "Data di inizio": "daytime",
            "Camera": "name",
            "Numero passaggi": "count_all",
            "Ora di inizio": "hour",
        }
    )

    for vehicle_type, factor in VEHICLE_CONVERSION_FACTORS.items():
        df_bridge.loc[df_bridge["Tipo veicolo"] == vehicle_type, "count_all"] *= factor

    # Aggregation step
    dfp_bridge = (
        df_bridge.sort_values(by=["daytime", "hour"])
        .groupby(["daytime", "hour", "name", "sezione"])["count_all"]
        .sum()
        .reset_index()
    )

    return df_bridge, dfp_bridge


### 2. PASTA database interface


def plot_sens_position(gdf: gpd.GeoDataFrame, output_dir: Path) -> None:
    # The analysis refers to ten sensors in the intersection area between Corso Pechiera and Corso Francia, in the city of Turin
    """
    To plot sensors distribution on an OSM map

    REFERENCES:
        geopandas
        https://geopandas.org/en/stable/gallery/plotting_basemap_background.html
        cx libreria
        https://contextily.readthedocs.io/en/latest/reference.html
        cx providers (parametro source mappa background)
        http://contextily.readthedocs.io/en/latest/providers_deepdive.html
    """
    print("\tCreating plot for sensors distribution")
    # Sensors plot + OSM background
    fig, ax = plt.subplots(figsize=(12, 10))
    fig.suptitle("Sensors distribution", fontsize=14, fontweight="bold")
    # plt.xlabel('Longitude')
    # plt.ylabel('Latitude')
    gdf.plot(ax=ax, marker="o", color="blue", markersize=60)

    ax.set_axis_off()
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['bottom'].set_visible(False)
    ax.spines['left'].set_visible(False)
    
    cx.add_basemap(ax, source=cx.providers.OpenStreetMap.Mapnik)
    # point labels
    for x, y, label in zip(
        gdf.geometry.x, gdf.geometry.y, zip(gdf["Cod_sens"], gdf["name"])
    ):
        ax.annotate(
            label,
            xy=(x, y),
            xytext=(3, 3),
            textcoords="offset points",
            fontsize=9,
            fontweight="bold",
        )

    plt.savefig(output_dir / "FP_sensors_distr.png", bbox_inches="tight", dpi=300)
    plt.close()


def pasta_db_merge(anagraphics: pd.DataFrame, flows: pd.DataFrame) -> pd.DataFrame:
    print("\tCreating PASTA Database")
    # Join to correlate all the data
    df_pasta = pd.DataFrame.merge(anagraphics, flows, how="inner")
    df_pasta = format_hour_column(df_pasta)
    return df_pasta


### 3. DATA AGGREGATION AND COMPARISON


def merge_data(df_bridge: pd.DataFrame, df_pasta: pd.DataFrame) -> pd.DataFrame:
    """Merge all available registered data on commun keys."""
    print("\tCollecting all data to create the complete dataframe")

    # data preparation to let merge happen smoothly
    # - BRIDGE
    df_bridge = df_bridge.rename(columns={"count_all": "BRIDGE_count"}).drop(
        columns=["Tipo veicolo", "Giorno di inizio"]
    )
    df_bridge = (
        (df_bridge.sort_values(by="daytime").sort_values(by="hour"))
        .groupby(["daytime", "hour", "name", "sezione"])["BRIDGE_count"]
        .sum()
        .reset_index()
        .sort_values(["sezione", "name", "daytime", "hour"])
    )
    print(df_bridge.columns)
    df_bridge = standardize_datetime(df_bridge, "daytime", DATE_FORMAT)
    df_bridge = standardize_datetime(df_bridge, "daytime", DATE_FORMAT)
    df_bridge["hour"] = df_bridge["hour"].astype(object)
    df_bridge["name"] = df_bridge["name"].astype(object)

    # - PASTA
    df_pasta = df_pasta.rename(columns={"count_all": "PASTA_count"}).drop(
        columns=[
            "sensor_description",
            "AVG_count_light",
            "AVG_count_heavy",
            "AVG_speed_light",
            "AVG_speed_heavy",
        ]
    )
    print(df_pasta.columns)
    df_pasta = standardize_datetime(df_pasta, "daytime", DATE_FORMAT)
    df_pasta = df_pasta.sort_values(["sezione", "name", "daytime", "hour"]).reset_index(
        drop=True
    )

    # Check data integrity before merging
    @pa.check_types
    def check_integrity(df: pd.DataFrame, schema: pa.DataFrameModel) -> pd.DataFrame:
        return schema.validate(df)

    df_pasta = check_integrity(df_pasta, DataFrameSchemaPasta)
    df_bridge = check_integrity(df_bridge, DataFrameSchemaBridge)

    # merge data
    data_tot = pd.DataFrame.merge(
        df_pasta, df_bridge, how="inner", on=["sezione", "name", "daytime", "hour"]
    )
    print(data_tot.columns)
    # data_tot = check_integrity(data_tot, DataFrameSchemaMerge)

    return data_tot


### 4. PLOTS


def lineplot_data(
    dfp_list: List[pd.DataFrame],
    output_dir: Path,
    data_source: List[str] = ["PASTA", "BRIDGE"],
) -> None:
    print("\tCreating lineplots")
    """Create line plot showing hourly traffic flows"""
    for df, source in enumerate(data_source):
        ax = sns.lineplot(
            data=dfp_list[df],
            x="hour",
            y="count_all",
            hue="name",
            palette=PALETTES["primary"],
        )
        plt.xticks(rotation=45)
        plt.title(
            f"{source} data for hourly traffic flows", fontsize=14, fontweight="bold"
        )
        plt.xlabel("Time (h)")
        plt.ylabel("Vehicle Count")
        plt.grid(True, alpha=0.4)
        sns.move_legend(ax, "upper left", bbox_to_anchor=(1, 1))
        plt.savefig(
            output_dir / f"FP_{source}_lineplot_sensors.png",
            bbox_inches="tight",
            dpi=300,
        )
        plt.close()


def histplot_data(
    dfp_list: List[pd.DataFrame],
    output_dir: Path,
    data_source: List[str] = ["PASTA", "BRIDGE"],
) -> None:
    """Create histograms showing hourly traffic flow by camera."""
    print("\tCreating histograms")
    for df, source in enumerate(data_source):
        dfp = dfp_list[df]
        cameras = sorted(dfp["name"].unique())

        fig, axs = plt.subplots(
            len(cameras), 1, figsize=(18, 26), constrained_layout=True
        )
        fig.suptitle(
            f"{source} Data - Traffic Flow by Camera", fontsize=14, fontweight="bold"
        )

        # Single camera case
        if len(cameras) == 1:
            axs = [axs]
        # For multiple cameras
        for n, cam in enumerate(cameras):
            data_camera = dfp[dfp["name"] == cam]

            sns.barplot(data=data_camera, x="hour", y="count_all", ax=axs[n])

            axs[n].set_title(f"Flow per hour - Camera: {cam}")
            axs[n].set_xlabel("Time (h)")
            axs[n].set_ylabel("Vehicle Count")
            axs[n].grid(True, alpha=0.4)

        plt.savefig(
            output_dir / f"FP_{source}_histogram_sensors.png",
            bbox_inches="tight",
            dpi=300,
        )
        plt.close()


def plot_comparison(data_tot: pd.DataFrame, output_dir: Path) -> None:
    """Visual comparison among data from PASTA and BRDIGE databases."""
    print("\tCreating plots to compare data")
    # Lineplot
    fig, axs = plt.subplots(1, 2, figsize=(14, 7), sharey=True, constrained_layout=True)
    fig.suptitle(
        "Lineplot comparison - PASTA vs BRIDGE", fontsize=16, fontweight="bold"
    )

    sns.lineplot(
        data=data_tot,
        x="hour",
        y="PASTA_count",
        hue=data_tot["name"].astype(str),
        ax=axs[0],
        palette=PALETTES["primary"],
    )
    axs[0].set_title("PASTA Count by Hour", fontsize=12)
    axs[0].set_xlabel("Time (h)")
    axs[0].set_ylabel("Vehicle Count")
    axs[0].grid(True, alpha=0.4)
    axs[0].tick_params(axis="x", rotation=45)

    sns.lineplot(
        data=data_tot,
        x="hour",
        y="BRIDGE_count",
        hue=data_tot["name"].astype(str),
        ax=axs[1],
        palette=PALETTES["secondary"],
    )
    axs[1].set_title("BRIDGE Count by Hour", fontsize=12)
    axs[0].set_xlabel("Time (h)")
    axs[0].set_ylabel("Vehicle Count")
    axs[1].grid(True, alpha=0.4)
    axs[1].tick_params(axis="x", rotation=45)

    plt.savefig(
        output_dir / f"FP_ALL_lineplot_sensors.png", bbox_inches="tight", dpi=300
    )
    plt.close()

    dfp = pd.melt(
        data_tot,
        id_vars=["Cod_sens", "hour"],
        value_vars=["BRIDGE_count", "PASTA_count"],
        var_name="source",
        value_name="count",
    )

    fig, axs = plt.subplots(
        len(dfp["Cod_sens"].unique()),
        figsize=(18, 24),
        constrained_layout=True,
        sharey=True,
    )

    # Check the unique sensor case
    if len(sorted(dfp["Cod_sens"].unique())) == 1:
        axs = [axs]

    fig.suptitle(
        "PASTA VS BRIDGE - hourly traffic comparison", fontsize=16, fontweight="bold"
    )

    for n, cam in enumerate(dfp["Cod_sens"].unique()):
        data_camera = dfp[dfp["Cod_sens"] == cam]

        sns.barplot(data=data_camera, x="hour", y="count", hue="source", ax=axs[n])

        axs[n].set_title(f"Sensor {cam}", fontsize=12)
        axs[n].set_xlabel("Time (h)")
        axs[n].set_ylabel("Vehicle Count")
        axs[n].grid(True, alpha=0.4)
        axs[n].legend(title="Data source")

    plt.savefig(
        output_dir / f"FP_ALL_histogram_sensors.png", bbox_inches="tight", dpi=300
    )
    plt.close()


def pasta_speeds(df_pasta: pd.DataFrame, output_dir: Path) -> None:
    """Plot average registered velocities from PASTA, for each sensor."""
    img = sns.lineplot(
        df_pasta,
        x="hour",
        y="AVG_speed",
        hue=df_pasta["name"].astype(str),
        palette=PALETTES["speeds"],
    )
    plt.title("Average speed (from PASTA)", fontsize=14, fontweight="bold")
    plt.xlabel("Time (h)")
    plt.xticks(rotation=45)
    plt.ylabel("Average speed (m/s)")
    plt.grid(True, alpha=0.4)
    sns.move_legend(img, loc="best", bbox_to_anchor=(1, 1))

    plt.savefig(output_dir / "FP_pasta_avg_speed.png", bbox_inches="tight", dpi=300)
    plt.close()


def main():
    print("\nANALYSIS OF TRAFFIC DATA FROM PASTA DATABASE AND BRIDGE INTERFACE")
    file_bridge = cfg.BRIDGE_CSV
    output_dir = cfg.OUTPUT_DIR_AM_SENS_DUA
    sensor_folder = cfg.SENS_DATA_FOLDER
    output_dir.mkdir(parents=True, exist_ok=True)

    bridge_data, dfp_bridge = bridge_db(file_bridge)
    if sensor_folder is not None:
        anagraphics = pd.read_csv(Path(data_folder / "anagraphics_fp.csv"))
        flows = pd.read_csv(Path(data_folder / "flows_fp.csv")) 
    else:
        load_dotenv()  # reads variables from a .env file and sets them in os.environ
        connection_strings = {
            "ista": os.getenv("ISTA_URL"),
            "istc": os.getenv("ISTC_URL"),
        }
        # "server:driver://username:psw@host"
            df_anagraph, df_flows = _get_pasta_data(
                connection_strings["ista"], connection_strings["istc"]
            )
    
    df_pasta = pasta_db_merge(df_anagraph, df_flows)

    gdf_sensors = gpd.GeoDataFrame(
        df_anagraph,
        geometry=gpd.points_from_xy(df_anagraph.lon, df_anagraph.lat),
        crs="EPSG:4326",
    ).to_crs(epsg=3857)

    plot_sens_position(gdf_sensors, output_dir)
    lineplot_data([df_pasta, dfp_bridge], output_dir)
    histplot_data([df_pasta, dfp_bridge], output_dir)
    pasta_speeds(df_pasta, output_dir)

    # Merge data from both sources for comparison
    data_merged = merge_data(bridge_data, df_pasta)
    if len(data_merged) > 0:
        plot_comparison(data_merged, output_dir)
    else:
        print("WARNING: No common records found - skipping data comparison")


if __name__ == "__main__":
    main()
