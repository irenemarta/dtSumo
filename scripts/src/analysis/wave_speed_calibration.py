"""
Calibration of the congested-branch wave speed of the theoretical FD (see
fundamental_diagram.py) from real sensor data, instead of the literature
default DEFAULT_WAVE_SPEED_KMH: free-flow speed and jam density can be derived 
from geometry (edge speed limit, vehicle packing), but the wave speed genuinely
depends on driver behavior in congestion and needs to be measured, not guessed.

Keep only streets where speed suggests congestion, derive density = count/speed for
each, and fit a line through them: the slope's magnitude is the wave speed estimate. 

NB: it does NOT replicate Li & Zhang's fluctuation-based 
state separation and MILP piecewise fit. 
"""

from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from scripts.src.inputs.sensors import DATE_FORMAT, pasta_db_merge, standardize_datetime
from scripts.src.modules.df_schemas import DataFrameSchemaPasta

# Bounds used to reject a calibration result that is clearly nonsense
# (e.g. fit on 5 noisy points) rather than silently trusting it.
MIN_WAVE_SPEED_KMH = 1.0
MAX_WAVE_SPEED_KMH = 60.0


def prepare_pasta_data(anagraphics: pd.DataFrame, flows: pd.DataFrame) -> pd.DataFrame:
    df = pasta_db_merge(anagraphics, flows)

    already_iso = df["daytime"].astype(str).str.match(r"^\d{4}-\d{2}-\d{2}$").all()
    if not already_iso:
        df = standardize_datetime(df, "daytime", DATE_FORMAT)

    if "count_all" in df.columns and "PASTA_count" not in df.columns:
        df = df.rename(columns={"count_all": "PASTA_count"})

    schema_columns = list(DataFrameSchemaPasta.to_schema().columns)
    missing = [c for c in schema_columns if c not in df.columns]
    if missing:
        raise ValueError(
            f"Missing columns after pasta_db_merge(): {missing}. "
            f"Available columns: {sorted(df.columns)}"
        )
    df = df[schema_columns]

    return DataFrameSchemaPasta.validate(df)


def validate_sensor_df(df: pd.DataFrame) -> pd.DataFrame:
    return DataFrameSchemaPasta.validate(df)


def load_validated_sensor_data(path: Path) -> pd.DataFrame:
    return validate_sensor_df(pd.read_csv(path))


def estimate_wave_speed_kmh(
    sensor_df: pd.DataFrame,
    free_flow_speed_kmh: float,
    congestion_speed_fraction: float = 0.5,
    min_observations: int = 5,
) -> Optional[float]:
    """
    Input:
    sensor_df: one row per (daytime, hour) observation for a single
    sensor, with at least 'PASTA_count' (veh/h) and 'AVG_speed' (km/h) columns.

    Returns:
    estimated wave speed magnitude (km/h), or None if there aren't 
    enough congested observations to fit a reliable line 
    (fallback to literature default: Li & Zhang 2011
    "Fundamental Diagram of Traffic Flow: New Identification Scheme and
    Further Evidence from Empirical Data", TRR 2260)
    """
    sensor_df = validate_sensor_df(sensor_df)

    df = sensor_df.dropna(subset=["PASTA_count", "AVG_speed"])
    df = df[df["AVG_speed"] > 0]
    congested = df[df["AVG_speed"] < congestion_speed_fraction * free_flow_speed_kmh]
    if len(congested) < min_observations:
        return None

    density = congested["PASTA_count"] / congested["AVG_speed"]  # veh/km
    flow = congested["PASTA_count"]  # veh/h

    if density.nunique() < 2:
        return None

    slope, _intercept = np.polyfit(density, flow, 1)
    wave_speed = abs(float(slope))

    if not (MIN_WAVE_SPEED_KMH <= wave_speed <= MAX_WAVE_SPEED_KMH):
        return None
    return wave_speed


def wave_speed_for_edge(
    edge_id: str,
    sensor_df: pd.DataFrame,
    lookup_path: Path,
    free_flow_speed_kmh: float,
    **kwargs,
) -> Optional[float]:
    """
    Maps edge_id -> Cod_sens via the same sens_lanes_match.csv lookup
    already used by meso_validation.load_sensor_edge_ids(), then calls
    estimate_wave_speed_kmh() on that sensor's real observations. An edge
    with no matching sensor (or one that never shows real congestion)
    returns None — not every edge can be calibrated this way.
    """
    lookup = pd.read_csv(lookup_path)
    sensor_ids = lookup.loc[
        lookup["id_edge"].astype(str) == str(edge_id), "Cod_sens"
    ].unique()
    if len(sensor_ids) == 0:
        return None

    sub = sensor_df[sensor_df["Cod_sens"].isin(sensor_ids)]
    return estimate_wave_speed_kmh(sub, free_flow_speed_kmh, **kwargs)