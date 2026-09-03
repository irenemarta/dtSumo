"""
TuST — MESO distortion check (Ni et al. 2026, arxiv:2606.09282).

Reproduces, on TuST's own MESO output, the two self-consistency checks the
paper uses when no microscopic reference is available (its Scenario 2 — the
same situation as ours: demand too high for a full-network micro run):

1. plot_density_evolution(): link density over time (5-min bins), to spot
   the free-jam -> jam-jam "spike then collapse" artifact (paper Fig. 3/8).
2. plot_flow_density(): flow-density scatter vs. the theoretical triangular
   FD envelope (fundamental_diagram.py), to quantify how many observed
   states are physically inconsistent (paper Fig. 9/10).

Does NOT touch the existing assignment pipeline: it reads (or, if asked,
regenerates once) the edgeData dump already produced by
feedback_cycle.build_final_sumocfg / build_sumocfg_day. The pipeline's own
runs default to EDGEDATA_FREQ=1800s (feedback_cycle.py) — fine for the SUE
travel-time feedback loop, too coarse for a 5-min congestion-onset plot on
a few-hour peak period, hence regenerate_fine_edgedata() below.

CAVEAT: the edgeData attribute names assumed here (density/entered/speed)
match SUMO's default <edgeData> meandata output and are consistent with
the sampledSeconds/traveltime attributes already relied upon in
feedback_cycle.load_traveltimes(); this has not been run against a real
SUMO edgeData.xml in this environment (no SUMO install here) — spot-check
attribute names on your first real output before trusting the plots.

A micro-vs-meso spot check on a low-demand corridor (following the
reduced-load approach the original TuST paper already used in its own
validation, Sec. 5.3) is a natural follow-up once this module has
identified low-traffic candidate edges — not implemented here.
"""

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional

import click
import matplotlib.pyplot as plt
import pandas as pd
import sumolib

from scripts.src.operations.cmd import _ensure_sumo_home, _sumo_bin, run_cmd
from scripts.src.operations.feedback_cycle import _write_edgedata_additional
from scripts.src.analysis.fundamental_diagram import DEFAULT_WAVE_SPEED_KMH, FDParams

ANALYSIS_EDGEDATA_FREQ = 300  # 5 minuti, come nel paper (Sez. 6)


# --- Step 0 (optional): pull real detector locations instead of guessing edges ---


def load_sensor_edge_ids(lookup_path: Path) -> List[str]:
    """
    Reads the sensor->lane->edge lookup already built by
    detectors.py: returns the unique edge_ids it covers. 
    Validate against real sensors edges, instead of hand-picked edges.
    """
    df = pd.read_csv(lookup_path)
    return sorted(df["id_edge"].dropna().astype(str).unique().tolist())


# --- Step 1: (re)generate a fine-grained edgeData dump for analysis ---


def _existing_additional_files(sumocfg_path: Path) -> List[str]:
    """
    Reads the additional-files list already baked into a .sumocfg (written
    via SUMO's --save-configuration, see entities.py:CfgAttributes.build),
    so regenerate_fine_edgedata() can append to it rather than silently
    replacing it: passing -a on the command line together with -c overrides
    the config file's own additional-files entirely, not merges with it —
    that was dropping VType.add.xml and causing
    "vehicle type 'passenger' ... not known".
    """
    tree = ET.parse(sumocfg_path)
    el = tree.getroot().find(".//additional-files")
    if el is None:
        return []
    return [v for v in el.get("value", "").split(",") if v]


def regenerate_fine_edgedata(
    sumocfg_path: Path, out_dir: Path, freq: int = ANALYSIS_EDGEDATA_FREQ
) -> Path:
    """
    Re-runs SUMO once on an already-built final .sumocfg, adding a dedicated
    edgeData dump at `freq` seconds. Writes only to out_dir — does not
    modify the original .sumocfg or its outputs.
    """
    _ensure_sumo_home()
    out_dir.mkdir(parents=True, exist_ok=True)
    edgedata_out = out_dir / "edgeData_fine.xml"
    edgedata_additional = out_dir / "edgedata_fine.add.xml"
    _write_edgedata_additional(edgedata_additional, edgedata_out, freq=freq)

    existing = _existing_additional_files(sumocfg_path)
    if not existing:
        click.echo(
            f"[meso_validation] ATTENZIONE: nessun additional-files trovato in {sumocfg_path}, "
            f"verrà usato solo {edgedata_additional} — se il run fallisce per un vType/TAZ "
            f"mancante, controlla il .sumocfg."
        )
    all_additionals = existing + [str(edgedata_additional)]

    cmd = [
        _sumo_bin("sumo"),
        "-c", str(sumocfg_path),
        "-a", ",".join(all_additionals),
    ]
    run_cmd(cmd, tool_name="sumo (fine edgeData re-run)")
    return edgedata_out


# --- Step 2: parse edgeData into a tidy DataFrame ---


def load_edgedata(edgedata_file: Path, edge_ids: Optional[List[str]] = None) -> pd.DataFrame:
    """
    Parses a SUMO edgeData dump into one row per (edge, interval): density
    (veh/km), flow (veh/h, derived from the `entered` vehicle count over
    the interval duration) and speed.
    """
    tree = ET.parse(edgedata_file)
    rows = []
    for interval in tree.getroot().findall("interval"):
        begin = float(interval.get("begin"))
        end = float(interval.get("end"))
        duration_h = (end - begin) / 3600.0
        for edge in interval.findall("edge"):
            eid = edge.get("id")
            if edge_ids is not None and eid not in edge_ids:
                continue
            entered = edge.get("entered", "0")
            rows.append({
                "edge_id": eid,
                "begin": begin,
                "end": end,
                "density": float(edge.get("density", "nan")),
                "flow_veh_h": (float(entered) / duration_h) if duration_h > 0 else float("nan"),
                "speed": float(edge.get("speed", "nan")),
            })
    return pd.DataFrame(rows)


# --- Step 3: the two paper-style checks ---


MAX_LINES_PER_PLOT = 15  # oltre, anche con colori distinti il grafico è illeggibile


def plot_density_evolution(
    df: pd.DataFrame, edge_ids: List[str], out_path: Path, title: str = ""
) -> None:
    """
    Paper Fig. 3/8 style: link density over time, one line per edge.

    Uses a 20-color palette (matplotlib's default cycle only has 10 —
    with more edges, two different lines silently get the same color and
    become indistinguishable). Beyond MAX_LINES_PER_PLOT edges, splits into
    several files (out_path stem suffixed _1, _2, ...) instead of a single
    unreadable plot.
    """
    chunks = [
        edge_ids[i:i + MAX_LINES_PER_PLOT] for i in range(0, len(edge_ids), MAX_LINES_PER_PLOT)
    ] or [[]]

    for chunk_idx, chunk in enumerate(chunks):
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.set_prop_cycle(color=plt.cm.tab20.colors)
        for eid in chunk:
            sub = df[df["edge_id"] == eid].sort_values("begin")
            if sub.empty:
                continue
            ax.plot(sub["begin"] / 3600, sub["density"], marker="o", markersize=3, label=eid)
        ax.set_xlabel("Time of day [h]")
        ax.set_ylabel("Density [veh/km]")
        suffix = f" ({chunk_idx + 1}/{len(chunks)})" if len(chunks) > 1 else ""
        ax.set_title((title or "Link density evolution (MESO)") + suffix)
        ax.legend(fontsize=8, ncol=2)
        fig.tight_layout()

        chunk_out_path = (
            out_path if len(chunks) == 1
            else out_path.with_stem(f"{out_path.stem}_{chunk_idx + 1}")
        )
        fig.savefig(chunk_out_path, dpi=150)
        plt.close(fig)
        

import matplotlib.pyplot as plt
import math

def plot_density_grid(density_df, edge_ids, out_path):
    """
    density_df: DataFrame con colonne 'time', 'edge_id', 'density'
    (o quello che già usate per generare i due grafici attuali)
    """
    n = len(edge_ids)
    n_cols = 3
    n_rows = math.ceil(n / n_cols)

    fig, axs = plt.subplots(n_rows, n_cols, figsize=(5*n_cols, 3*n_rows), squeeze=False)
    axs_flat = axs.flatten()

    for ax, eid in zip(axs_flat, edge_ids):
        sub = density_df[density_df["edge_id"] == eid]
        ax.plot(sub["begin"] / 3600, sub["density"], linewidth=0.8)
        ax.set_title(eid, fontsize=9)
        ax.set_xlabel("Time [h]")
        ax.set_ylabel("Density [veh/km]")
        # niente ylim fisso: ogni subplot si auto-scala sul proprio range

    for ax in axs_flat[len(edge_ids):]:
        ax.set_visible(False)

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)

def plot_fundamental_diagram_grid(fd_df, edge_ids, out_path, theoretical_fd=None):
    """
    fd_df: DataFrame con colonne 'edge_id', 'density', 'flow'
    (density in veh/km, flow in veh/h — stessa fonte di plot_density_grid)
    theoretical_fd: opzionale, dict {edge_id: (v_free, k_jam, w)} se volete
    sovrapporre la curva teorica triangolare già calcolata da
    fundamental_diagram.py, per confrontare osservato vs atteso edge per edge.
    """
    n = len(edge_ids)
    n_cols = 3
    n_rows = math.ceil(n / n_cols)

    fig, axs = plt.subplots(n_rows, n_cols, figsize=(5*n_cols, 3.5*n_rows), squeeze=False)
    axs_flat = axs.flatten()

    for ax, eid in zip(axs_flat, edge_ids):
        sub = fd_df[fd_df["edge_id"] == eid]
        ax.scatter(sub["density"], sub["flow"].fillna(0), s=4, alpha=0.5)
        ax.set_title(eid, fontsize=9)
        ax.set_xlabel("Density [veh/km]")
        ax.set_ylabel("Flow [veh/h]")

        if theoretical_fd and eid in theoretical_fd:
            v_free, k_jam, w = theoretical_fd[eid]
            k_crit = v_free * k_jam / (v_free + w)  # densità critica del triangolo
            k_range = [0, k_crit, k_jam]
            q_range = [0, v_free * k_crit, 0]
            ax.plot(k_range, q_range, color="red", linewidth=1, linestyle="--", label="Theoretical FD")
            ax.legend(fontsize=7)

    for ax in axs_flat[len(edge_ids):]:
        ax.set_visible(False)

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)

# --- Step 4: triage helpers (length/lanes/sample count) ---


def edge_diagnostics(net: "sumolib.net.Net", edge_id: str) -> Dict[str, Optional[float]]:
    """
    Length and lane count for one edge, straight from the loaded net —
    useful to tell "short edge, noisy flow estimate" apart from "long edge,
    genuine congestion", and to check whether a density plateau (e.g.
    ~420-440 veh/km) matches the *physical* jam density for that many
    lanes (num_lanes * 1000 / (vehicle_length + min_gap), see
    fundamental_diagram.py) rather than a MESO-specific artifact.
    """
    if not net.hasEdge(edge_id):
        return {"length_m": None, "num_lanes": None}
    edge = net.getEdge(edge_id)
    return {"length_m": edge.getLength(), "num_lanes": len(edge.getLanes())}


def plot_flow_density(df: pd.DataFrame, edge_id: str, fd: FDParams, out_path: Path) -> float:
    """
    Paper Fig. 9/10 style: flow-density scatter for one edge vs. the
    theoretical FD envelope. Returns the percentage of observed states
    above the envelope (i.e. physically inconsistent under the triangular
    FD assumption) — the "distortion score" for that edge.
    """
    sub = df[df["edge_id"] == edge_id].dropna(subset=["density", "flow_veh_h"])
    if sub.empty:
        return float("nan")

    envelope = sub["density"].apply(fd.envelope_flow)
    outside = sub["flow_veh_h"] > envelope * 1.02  # 2% tolerance
    distortion_pct = 100.0 * outside.mean()

    k_curve = sorted(set(sub["density"].tolist()) | {0.0, fd.jam_density_veh_km})
    fig, ax = plt.subplots(figsize=(6, 5))
    ax.scatter(sub["density"], sub["flow_veh_h"], s=12, alpha=0.6, label="MESO states")
    ax.plot(k_curve, [fd.envelope_flow(k) for k in k_curve], color="red", linewidth=1.5, label="Theoretical FD")
    ax.set_xlabel("Density [veh/km]")
    ax.set_ylabel("Flow [veh/h]")
    ax.set_title(f"{edge_id} — {distortion_pct:.1f}% of states outside FD")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return distortion_pct


# --- Orchestration ---


def run_meso_validation(
    sumocfg_path: Path,
    edge_ids: List[str],
    fd_params: Dict[str, FDParams],
    out_dir: Path,
    regenerate: bool = True,
    net: Optional["sumolib.net.Net"] = None,
) -> Dict[str, float]:
    """
    Runs both checks for a list of edges of one already-built .sumocfg,
    saving plots under out_dir. Returns {edge_id: distortion_pct}.

    `fd_params` must have one FDParams per edge_id — build them with
    fundamental_diagram.fd_params_for_edge(net, edge_id) using the same
    `net` from an AssignmentContext. Pass the same `net` here too (optional)
    to also print length/lane-count/sample-count per edge for triage.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    edgedata_file = (
        regenerate_fine_edgedata(sumocfg_path, out_dir)
        if regenerate
        else sumocfg_path.parent / "edge_output_final.xml"
    )

    df = load_edgedata(edgedata_file, edge_ids=edge_ids)

    plot_density_evolution(df, edge_ids, out_dir / "density_evolution.png")
    plot_density_grid(df, edge_ids, out_dir / "density_grid.png")
    plot_fundamental_diagram_grid(df, edge_ids, out_dir / "fd_grid.png")

    distortion = {}
    for eid in edge_ids:
        if eid not in fd_params:
            continue
        distortion[eid] = plot_flow_density(
            df, eid, fd_params[eid], out_dir / f"fd_{eid}.png"
        )

    print(f"[meso_validation] plots saved in {out_dir}")
    for eid, pct in distortion.items():
        n_samples = len(df[df["edge_id"] == eid].dropna(subset=["density", "flow_veh_h"]))
        extra = f"n={n_samples}"
        if net is not None:
            diag = edge_diagnostics(net, eid)
            if diag["length_m"] is not None:
                extra += f", length={diag['length_m']:.0f}m, lanes={diag['num_lanes']}"
        print(f"\t{eid}: {pct:.1f}% outside FD ({extra})")

    return distortion


@click.command()
@click.option(
    "--sumocfg", "sumocfg_path", type=click.Path(exists=True, path_type=Path), required=True,
    help="Percorso del .sumocfg finale già costruito (build_final_sumocfg / build_sumocfg_day).",
)
@click.option(
    "--edge", "edge_ids", multiple=True,
    help="Edge ID da ispezionare (ripetibile, es. --edge 570 --edge 603). "
         "Si combina con --from-sensors se entrambi dati.",
)
@click.option(
    "--from-sensors", is_flag=True,
    help="Aggiunge tutti gli edge_id dei sensori reali in cfg.SENS_LANE_LOOKUP "
         "(gli stessi punti già validati nel paper TuST originale, Sez. 5.2/5.3).",
)
@click.option(
    "--out-dir", type=click.Path(path_type=Path), default=None,
    help="Cartella di output per i plot. Default: cfg.IMAGES_MESO_VALIDATION/<nome sumocfg>.",
)
@click.option(
    "--regenerate/--no-regenerate", default=True,
    help="Rilancia SUMO una volta per un dump edgeData a 300s. Con --no-regenerate legge "
         "edge_output_final.xml già esistente (grana 1800s, più grezza).",
)
@click.option(
    "--wave-speed", type=float, default=DEFAULT_WAVE_SPEED_KMH, show_default=True,
    help="Velocità dell'onda di congestione (km/h) di default/fallback per il FD teorico.",
)
@click.option(
    "--sensor-data", "sensor_data_path", type=click.Path(exists=True, path_type=Path), default=None,
    help="CSV già nello schema df_pasta (output di sensors.py:pasta_db_merge(), validato "
         "con DataFrameSchemaPasta) — usalo se hai già esportato quel dataframe altrove. "
         "In alternativa, usa --pasta-anagraphics/--pasta-flows per partire dai due grezzi.",
)
@click.option(
    "--pasta-anagraphics", "anagraphics_path", type=click.Path(exists=True, path_type=Path), default=None,
    help="CSV grezzo dell'anagraphics sensori PASTA (es. export di _get_pasta_data()[0]). "
         "Va usato insieme a --pasta-flows: i due vengono uniti con "
         "sensors.py:pasta_db_merge() prima di essere usati per la calibrazione, non presi "
         "così come sono.",
)
@click.option(
    "--pasta-flows", "flows_path", type=click.Path(exists=True, path_type=Path), default=None,
    help="CSV grezzo dei flussi PASTA (es. export di _get_pasta_data()[1]). Va usato insieme "
         "a --pasta-anagraphics.",
)
def cli(sumocfg_path, edge_ids, from_sensors, out_dir, regenerate, wave_speed,
        sensor_data_path, anagraphics_path, flows_path):
    """Verifica le distorsioni MESO (Ni et al. 2026, arxiv:2606.09282) su un run TuST già costruito."""
    import sumolib
    import scripts.src.inputs.config as cfg
    import pandera.errors
    from scripts.src.analysis.fundamental_diagram import fd_params_for_edge
    from scripts.src.analysis.wave_speed_calibration import (
        load_validated_sensor_data,
        prepare_pasta_data,
        wave_speed_for_edge,
    )

    if sensor_data_path and (anagraphics_path or flows_path):
        raise click.UsageError(
            "--sensor-data non si combina con --pasta-anagraphics/--pasta-flows: "
            "scegli o il df già pronto o i due grezzi da unire."
        )
    if bool(anagraphics_path) != bool(flows_path):
        raise click.UsageError("--pasta-anagraphics e --pasta-flows vanno dati insieme.")

    edge_ids = list(edge_ids)
    if from_sensors:
        if not cfg.SENS_LANE_LOOKUP.exists():
            raise click.UsageError(
                f"--from-sensors richiesto ma {cfg.SENS_LANE_LOOKUP} non esiste "
                f"(va generato prima, es. con `just detectors`)."
            )
        sensor_edges = load_sensor_edge_ids(cfg.SENS_LANE_LOOKUP)
        edge_ids = sorted(set(edge_ids) | set(sensor_edges))
        click.echo(f"--from-sensors: {len(sensor_edges)} edge dai sensori reali in {cfg.SENS_LANE_LOOKUP}")

    if not edge_ids:
        raise click.UsageError("Nessun edge da ispezionare: usa --edge e/o --from-sensors.")

    out_dir = out_dir or (cfg.IMAGES_MESO_VALIDATION / sumocfg_path.stem)

    net = sumolib.net.readNet(str(cfg.NET_FILE))

    sensor_df = None
    try:
        if sensor_data_path:
            sensor_df = load_validated_sensor_data(sensor_data_path)
        elif anagraphics_path and flows_path:
            sensor_df = prepare_pasta_data(
                pd.read_csv(anagraphics_path), pd.read_csv(flows_path)
            )
    except (pandera.errors.SchemaError, pandera.errors.SchemaErrors) as e:
        raise click.UsageError(
            f"I dati sensori non sono nello schema atteso (DataFrameSchemaPasta, "
            f"output di sensors.py:pasta_db_merge()). Dettaglio pandera:\n{e}"
        )

    fd_params = {}
    for eid in edge_ids:
        edge_wave_speed = wave_speed
        if sensor_df is not None:
            calibrated = wave_speed_for_edge(
                eid, sensor_df, cfg.SENS_LANE_LOOKUP,
                free_flow_speed_kmh=net.getEdge(eid).getSpeed() * 3.6,
            )
            if calibrated is not None:
                edge_wave_speed = calibrated
                click.echo(f"  {eid}: wave-speed calibrato da dati reali = {calibrated:.1f} km/h")
            else:
                click.echo(f"  {eid}: dati reali insufficienti, uso il fallback {wave_speed} km/h")
        fd_params[eid] = fd_params_for_edge(net, eid, wave_speed_kmh=edge_wave_speed)

    run_meso_validation(
        sumocfg_path=sumocfg_path,
        edge_ids=edge_ids,
        fd_params=fd_params,
        out_dir=out_dir,
        regenerate=regenerate,
        net=net,
    )


if __name__ == "__main__":
    cli()