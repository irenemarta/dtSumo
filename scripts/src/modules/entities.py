from dataclasses import dataclass
from pathlib import Path
import pandas as pd
from typing import TypedDict, Optional, Union, List
import os
import subprocess


@dataclass
class CfgAttributes:
    net: Path
    routes: Path
    output_cfg: Path
    output_sumo: Path
    config_name: str
    meso: bool = False
    teleport: Union[int, str] = "100"
    setting: Optional[Path] = None

    def build(self, method: str, begin: int, end: int, taz: str = None, tazrel: str = None, detectors: str = None, edgedata: str = None):        

        add_files: List[str] = []
        if taz:
            add_files.append(str(taz))
        if tazrel:
            add_files.append(str(tazrel))
        if detectors:
            add_files.append(str(detectors))
        if edgedata:
            add_files.append(str(edgedata))

        self.output_sumo.mkdir(parents=True, exist_ok=True)
        self.output_cfg.mkdir(parents=True, exist_ok=True)

        period = self.config_name.replace(".sumocfg", "").split("_")[-1]  # "random", "morning"
        suffix = f"{method}_{period}"

        cmd = [
            os.path.join(os.environ.get("SUMO_HOME", ""), "bin", "sumo"),
            "-n", str(self.net),
            "-r", str(self.routes),
            "--save-configuration", str(self.output_cfg / self.config_name),
            "--tls.all-off", "true" if "no_TLS" in self.config_name else "false",
            "--time-to-teleport", str(self.teleport),
            "--summary-output", str(self.output_sumo / f"Summary_{suffix}.xml"),
            "--vehroute-output", str(self.output_sumo / f"VehTraces_{suffix}.xml"),
            "--tripinfo-output", str(self.output_sumo / f"TripInfo_{suffix}.xml"),
            "--vehroute-output.exit-times", "true",
            "--vehroute-output.sorted", "true",
            "--vehroute-output.route-length", "true",
            "--vehroute-output.write-unfinished", "true",
        ]

        if self.meso:
            cmd += [
                "--mesosim",
                "--meso-recheck", "10", # to delay traffic flow into a fully occupied segment. 
                "--meso-minor-penalty", "1.5",
                "--meso-junction-control",
                "--meso-tls-penalty", "10",
                "--meso-jam-threshold", "0.5",
            ]
        else:
            cmd += [
                "--lanechange.duration", "5.0",
                "--ignore-junction-blocker", "5",
            ]

        if add_files:
            cmd += ["-a", ",".join(add_files)]

        if begin is not None and end is not None:
            cmd += ["--begin", str(begin), "--end", str(end)]

        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            print(f"{self.config_name} saved in {self.output_cfg}")
        except subprocess.CalledProcessError as e:
            print(f"Configuration saving error: {e.stderr}")

        return str(self.output_cfg / self.config_name)


class AlgoInfo(TypedDict):
    title: str
    df: pd.DataFrame