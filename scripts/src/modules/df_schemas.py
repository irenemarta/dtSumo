# Check data integrity
import pandera.pandas as pa
from pandera.typing import Series

from numpy import float64



# SENSORS.PY DATAFRAME SCHEMAS

class DataFrameSchemaBridge(pa.DataFrameModel):
    # Common columns definitions and constraints
    sezione: Series[int] = pa.Field(gt=0)
    name: Series[str]
    hour: Series[object] = pa.Field(str_matches=r"^\d{2}:00$") # HH:00 format
    daytime: Series[str] = pa.Field(str_matches=r"^\d{4}-\d{2}-\d{2}$") # YYYY-MM-DD format
    # Column definitions and constraints for BRDIGE
    BRIDGE_count: Series[int] = pa.Field(ge=0)


class DataFrameSchemaPasta(pa.DataFrameModel):
    # Common columns definitions and constraints
    sezione: Series[int] = pa.Field(gt=0)
    name: Series[str]
    hour: Series[object] = pa.Field(str_matches=r"^\d{2}:00$") # HH:00 format
    daytime: Series[str] = pa.Field(str_matches=r"^\d{4}-\d{2}-\d{2}$") # YYYY-MM-DD format
    # Column defintiions and constraints for PASTA
    Cod_sens: Series[int] = pa.Field(gt=0)
    strada: Series[str]
    direction: Series[str]
    lat: Series[float64]
    lon: Series[float64]
    disponibile: Series[bool] = pa.Field(isin=[0,1])
    PASTA_count: Series[int] = pa.Field(ge=0)
    AVG_accuracy: Series[float64] = pa.Field(ge=0)
    AVG_speed: Series[float64] = pa.Field(ge=0)

    class Config:
        strict = True # if True, error for extra columns not defined
        coerce = True  # convertes automatically if wrong type


class DataFrameSchemaMerge(pa.DataFrameModel):
    # Common columns definitions and constraints
    sezione: Series[int] = pa.Field(gt=0)
    name: Series[str]
    hour: Series[object] = pa.Field(str_matches=r"^\d{2}:00$") # HH:00 format
    daytime: Series[str] = pa.Field(str_matches=r"^\d{4}-\d{2}-\d{2}$") # YYYY-MM-DD format
    # Column definitions and constraints for merged dataframe
    BRIDGE_count: Series[int] = pa.Field(ge=0)
    PASTA_count: Series[int] = pa.Field(ge=0)
    Cod_sens: Series[int] = pa.Field(gt=0)
    strada: Series[str]
    direction: Series[str]
    lat: Series[float64]
    lon: Series[float64]
    disponibile: Series[bool] = pa.Field(isin=[0,1])
    AVG_accuracy: Series[float64] = pa.Field(ge=0)
    AVG_speed: Series[float64] = pa.Field(ge=0)

    class Config:
        strict = True # if True, error for extra columns not defined
        coerce = True  # convertes automatically if wrong type