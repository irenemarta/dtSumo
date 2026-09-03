"""
Theoretical triangular fundamental diagram (FD), used as a self-consistency
check against MESO edge output — see Ni et al. 2026
(https://arxiv.org/html/2606.09282v1), Section 6 / Figures 9-10: macroscopic
traffic states that fall outside the theoretical FD envelope are a direct
symptom of the queue-dynamics problems described in Section 3.

This is a diagnostic approximation, not a calibrated model: jam density is
derived from vehicle packing (length + minGap), free-flow speed from the
edge speed limit, and the backward wave speed is a literature-typical
default (see DEFAULT_WAVE_SPEED_KMH below) — TuST does not currently
calibrate SUMO's own meso-tauff/-taujf/-taujj parameters (see
entities.py:CfgAttributes.build), so there is no in-repo calibrated value
to derive it from instead.
"""

from dataclasses import dataclass
from typing import Optional

import sumolib

# Typical backward (congestion) wave speed for urban arterials, km/h.
# Not calibrated for this network — override via `wave_speed_kmh` using
# have a better estimate.
DEFAULT_WAVE_SPEED_KMH = 20.0

# Matches TuST's own vehicle length (see the TuST paper, Table 1) and
# SUMO's default minGap.
DEFAULT_VEHICLE_LENGTH_M = 4.5
DEFAULT_MIN_GAP_M = 2.5


@dataclass
class FDParams:
    """Triangular FD: free-flow branch up to k_crit, congested branch beyond."""

    free_flow_speed_kmh: float
    jam_density_veh_km: float
    wave_speed_kmh: float = DEFAULT_WAVE_SPEED_KMH

    @property
    def capacity_veh_h(self) -> float:
        # Intersection of the two branches: q_max = w * vf * k_jam / (vf + w)
        return (
            self.wave_speed_kmh * self.free_flow_speed_kmh * self.jam_density_veh_km
            / (self.free_flow_speed_kmh + self.wave_speed_kmh)
        )

    @property
    def critical_density_veh_km(self) -> float:
        return self.capacity_veh_h / self.free_flow_speed_kmh

    def envelope_flow(self, density_veh_km: float) -> float:
        """Max theoretical flow admissible at a given density (upper envelope)."""
        if density_veh_km <= 0:
            return 0.0
        if density_veh_km <= self.critical_density_veh_km:
            return self.free_flow_speed_kmh * density_veh_km
        return max(0.0, self.wave_speed_kmh * (self.jam_density_veh_km - density_veh_km))


def fd_params_for_edge(
    net: "sumolib.net.Net",
    edge_id: str,
    vehicle_length_m: float = DEFAULT_VEHICLE_LENGTH_M,
    min_gap_m: float = DEFAULT_MIN_GAP_M,
    wave_speed_kmh: float = DEFAULT_WAVE_SPEED_KMH,
    num_lanes: Optional[int] = None,
) -> FDParams:
    """
    Builds the theoretical FD for one edge from its speed limit and the vehicle packing geometry.
    """
    edge = net.getEdge(edge_id)
    free_flow_speed_kmh = edge.getSpeed() * 3.6
    lanes = num_lanes if num_lanes is not None else len(edge.getLanes())
    jam_density_veh_km = lanes * 1000.0 / (vehicle_length_m + min_gap_m)
    return FDParams(
        free_flow_speed_kmh=free_flow_speed_kmh,
        jam_density_veh_km=jam_density_veh_km,
        wave_speed_kmh=wave_speed_kmh,
    )