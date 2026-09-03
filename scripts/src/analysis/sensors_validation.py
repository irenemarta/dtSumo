"""
Validates simulation's edge output against real sensor data using:
- GEH statistic: standard transportation-engineering measure for
    comparing modeled vs observed flow (GEH<=5 good, 5-10 caution, >10 poor)
- Validation using an "affinity index" (as in TuST - Rapelli et al. 2018) per sensor.

Real sensor data is the ground truth used to validate both meso or micro simulations.
"""

import math
from pathlib import Path
from typing import List, Tuple

import click
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from scripts.src.analysis.meso_validation import load_edgedata

GEH_OK = 5.0
GEH_BAD = 10.0

MIN_GOOD_HOURS_FOR_THRESHOLD = 10  # sotto questa soglia il p90 interno non è affidabile



def compute_geh(sim_flow: float, real_flow: float) -> float:
    """
    GEH statistic between one simulated and one observed flow value (veh/h).
    0 = perfect match.
    """
    total = sim_flow + real_flow
    if total <= 0:
        return 0.0
    return float(np.sqrt(2 * (sim_flow - real_flow) ** 2 / total))


def hourly_sumo_flow(edgedata_df: pd.DataFrame, edge_id: str) -> Tuple[pd.DataFrame, pd.Series]:
    """
    Aggregates fine-grained (e.g. 5-min) SUMO flow samples for one edge
    into one mean value per hour of day (0-23).
    Keeping the raw samples for the scatter-cloud comparison plot.
    """
    sub = edgedata_df[edgedata_df["edge_id"] == edge_id].copy()
    sub["hour"] = (sub["begin"] // 3600).astype(int)
    hourly_mean = sub.groupby("hour")["flow_veh_h"].mean()
    return sub, hourly_mean


def hourly_real_flow(sensor_df: pd.DataFrame, cod_sens: int) -> pd.Series:
    """
    Real PASTA flow for one sensor, averaged across every considered day in sensor_df.
    One value per hour of day (0-23).
    sensor_df is the output of wave_speed_calibration.prepare_pasta_data() / load_validated_sensor_data().
    """
    sub = sensor_df[sensor_df["Cod_sens"] == cod_sens].copy()
    sub["hour_int"] = sub["hour"].str.slice(0, 2).astype(int)
    return sub.groupby("hour_int")["PASTA_count"].mean()


def geh_by_hour(sim_hourly: pd.Series, real_hourly: pd.Series) -> pd.Series:
    """GEH for every hour present on both sides (hours missing on either side are skipped)."""
    common_hours = sorted(set(sim_hourly.index) & set(real_hourly.index))
    return pd.Series(
        {h: compute_geh(sim_hourly[h], real_hourly[h]) for h in common_hours}
    ).sort_index()


def affinity_by_hour(sim_hourly: pd.Series, real_hourly: pd.Series) -> pd.Series:
    """
    Affinity index, computed as in Rapelli et al. paper, Sec. 5.2.
    """
    common_hours = sorted(
        h for h in set(sim_hourly.index) & set(real_hourly.index) if real_hourly[h] > 0
    )
    return pd.Series(
        {
            h: max(0.0, min(100.0, 100 * (1 - abs(sim_hourly[h] - real_hourly[h]) / real_hourly[h])))
            for h in common_hours
        }
    ).sort_index()


def hourly_variability(edgedata_df: pd.DataFrame, edge_id: str) -> pd.DataFrame:
    """
    CV = std/mean -> Variation Coefficient of 5 min SUMO samples, per hour. 
    Used to distinguish pure bias (eg. one-direction bias) from demand instability
    (eg. both over and underestimations), which GEH index does not reflect.
    """
    sub = edgedata_df[edgedata_df["edge_id"] == edge_id].copy()
    sub["hour"] = (sub["begin"] // 3600).astype(int)

    rows = []
    for hour, grp in sub.groupby("hour"):
        mean = grp["flow_veh_h"].mean()
        std = grp["flow_veh_h"].std()
        cv = (std / mean) if (mean and mean > 0 and len(grp) > 1) else float("nan")
        rows.append({"hour": hour, "mean_flow": mean, "cv": cv, "n_samples": len(grp)})
    return pd.DataFrame(rows).set_index("hour")


def bias_vs_variance_for_pair(
    edgedata_df: pd.DataFrame, edge_id: str, sensor_df: pd.DataFrame, cod_sens: int
) -> pd.DataFrame:
    """
    Per un edge/sensore: GEH e CV intra-ora affiancati, un'unica riga per
    ora. Base per l'aggregazione su più sensori in
    variance_vs_bias_summary().
    """
    sim_samples, sim_hourly = hourly_sumo_flow(edgedata_df, edge_id)
    real_hourly = hourly_real_flow(sensor_df, cod_sens)
    geh = geh_by_hour(sim_hourly, real_hourly)
    variability = hourly_variability(edgedata_df, edge_id)

    df = pd.DataFrame({"geh": geh}).join(variability[["cv", "n_samples"]], how="inner")
    df["edge_id"] = edge_id
    df["Cod_sens"] = cod_sens
    return df.reset_index().rename(columns={"index": "hour"})

def variance_vs_bias_summary(
    edgedata_df: pd.DataFrame,
    sensor_df: pd.DataFrame,
    sens_edge_pairs: List[Tuple[int, str]],
    geh_bad: float = GEH_BAD,
    geh_ok: float = GEH_OK,
    cv_threshold: float = None,
) -> dict:
    """
    ...
    cv_threshold: se fornito, usa questo valore fisso invece di ricalcolare
    il p90 sulle ore buone di QUESTA chiamata. Da passare quando il
    campione locale di ore buone è piccolo (es. finestra isolata): usa il
    p90 calcolato su un dataset più grande e indipendente, per evitare sia
    la circolarità (giudicare un sotto-campione con una soglia definita
    sullo stesso sotto-campione) sia un percentile instabile su pochi punti.
    """
    frames = [
        bias_vs_variance_for_pair(edgedata_df, edge_id, sensor_df, cod_sens)
        for cod_sens, edge_id in sens_edge_pairs
    ]
    combined = pd.concat(frames, ignore_index=True).dropna(subset=["cv"])

    bad = combined[combined["geh"] > geh_bad]
    good = combined[combined["geh"] <= geh_ok]

    if bad.empty or (cv_threshold is None and good.empty):
        return {
            "n_bad_hours": len(bad), "n_good_hours": len(good),
            "note": "dati insufficienti (ore buone o cattive con CV definito troppo poche) per un confronto affidabile",
            "combined": combined,
        }

    low_power = cv_threshold is None and len(good) < MIN_GOOD_HOURS_FOR_THRESHOLD

    if cv_threshold is not None:
        threshold_used = cv_threshold
        threshold_source = "esterna (passata esplicitamente)"
    else:
        threshold_used = float(good["cv"].quantile(0.90))
        threshold_source = f"interna, p90 su {len(good)} ore buone"

    pct_bad_high_variance = 100 * (bad["cv"] > threshold_used).mean()

    return {
        "n_bad_hours": len(bad),
        "n_good_hours": len(good),
        "median_cv_bad": float(bad["cv"].median()),
        "median_cv_good": float(good["cv"].median()) if not good.empty else float("nan"),
        "cv_threshold_used": float(threshold_used),
        "cv_threshold_source": threshold_source,
        "low_power_warning": low_power,
        "pct_bad_hours_high_variance": float(pct_bad_high_variance),
        "combined": combined,
    }

def _sensor_edge_pairs(
    edgedata_df: pd.DataFrame, sensor_df: pd.DataFrame, lookup_path: Path
) -> List[Tuple[int, str]]:
    """
    Check if sensor_lane_lookup.csv has correspondence to map edges.
    Returns edge-sensor pairs.
    """
    lookup = pd.read_csv(lookup_path)[["Cod_sens", "id_edge"]].drop_duplicates()
    available_edges = set(edgedata_df["edge_id"].unique())
    available_sensors = set(sensor_df["Cod_sens"].unique())

    pairs = []
    for cod_sens, edge_id in lookup.itertuples(index=False):
        if str(edge_id) in available_edges and cod_sens in available_sensors:
            pairs.append((cod_sens, str(edge_id)))
    return pairs


def plot_geh_bars(geh: pd.Series, sensor_label: str, out_path: Path) -> None:
    colors = ["red" if v > GEH_BAD else "gold" if v > GEH_OK else "green" for v in geh.values]
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.bar(geh.index, geh.values, color=colors, edgecolor="black")
    ax.axhline(GEH_OK, color="red", linestyle="--", label=f"Soglia di Validazione (GEH = {GEH_OK:.0f})")
    ax.axhline(GEH_BAD, color="darkred", linestyle=":", label=f"Errore Grave (GEH = {GEH_BAD:.0f})")
    ax.set_xlabel("Time (h)")
    ax.set_ylabel("GEH Index")
    ax.set_title(f"GEH index - Sensor {sensor_label}")
    ax.set_xticks(range(24))
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def plot_flow_comparison(
    sim_samples: pd.DataFrame, real_hourly: pd.Series, sensor_label: str, out_path: Path
) -> None:
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.scatter(sim_samples["hour"], sim_samples["flow_veh_h"], label="SUMO Flow", alpha=0.6)
    ax.scatter(real_hourly.index, real_hourly.values, label="Real Flow", color="orange")
    ax.set_xlabel("Time (h)")
    ax.set_ylabel("flow")
    ax.set_title(f"Flow on {sensor_label}")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


# Subplot standard dimension management
WIDTH_PER_SUBPLOT = 5.0
HEIGHT_PER_SUBPLOT = 3.5
DEFAULT_MAX_PER_PAGE = 10


def _grid_pages(
    sens_edge_pairs: List[Tuple[int, str]], max_per_page: int = DEFAULT_MAX_PER_PAGE
) -> List[List[Tuple[int, str]]]:
    if max_per_page < 1:
        raise ValueError("ERROR: max_per_page should be >1.")
    return [
        sens_edge_pairs[i : i + max_per_page]
        for i in range(0, len(sens_edge_pairs), max_per_page)
    ]


def plot_geh_grid(
    sens_edge_pairs: List[Tuple[int, str]],
    edgedata_df: pd.DataFrame,
    sensor_df: pd.DataFrame,
    out_dir: Path,
    filename_prefix: str = "geh_grid",
    max_per_page: int = DEFAULT_MAX_PER_PAGE,
) -> List[Path]:
    
    if not sens_edge_pairs:
        raise ValueError("Nessuna coppia sensore/edge da plottare.")
    out_dir.mkdir(parents=True, exist_ok=True)

    written_paths = []
    for page_idx, page_pairs in enumerate(_grid_pages(sens_edge_pairs, max_per_page), start=1):
        n_cols = math.ceil(len(page_pairs) / 2)
        fig, axs = plt.subplots(
            2, n_cols,
            figsize=(WIDTH_PER_SUBPLOT * n_cols, HEIGHT_PER_SUBPLOT * 2),
            constrained_layout=True, squeeze=False, sharex=True,
        )
        axs_flat = axs.flatten()

        for ax, (cod_sens, edge_id) in zip(axs_flat, page_pairs):
            _, sim_hourly = hourly_sumo_flow(edgedata_df, edge_id)
            real_hourly = hourly_real_flow(sensor_df, cod_sens)
            geh = geh_by_hour(sim_hourly, real_hourly)

            colors = ["red" if v > GEH_BAD else "gold" if v > GEH_OK else "green" for v in geh.values]
            ax.bar(geh.index, geh.values, color=colors, edgecolor="black")
            ax.axhline(GEH_OK, color="red", linestyle="--", linewidth=1)
            ax.axhline(GEH_BAD, color="darkred", linestyle=":", linewidth=1)
            ax.set_xlabel("Time (h)")
            ax.set_ylabel("GEH Index")
            ax.set_title(f"Sensor {cod_sens} ({edge_id})", fontsize=10)
            ax.set_xticks(range(0, 24, 4))

        for ax in axs_flat[len(page_pairs):]:
            ax.set_visible(False)

        handles = [
            plt.Line2D([0], [0], color="red", linestyle="--", label=f"Soglia validazione (GEH={GEH_OK:.0f})"),
            plt.Line2D([0], [0], color="darkred", linestyle=":", label=f"Errore grave (GEH={GEH_BAD:.0f})"),
        ]
        fig.legend(handles=handles, loc="upper center", ncol=2, bbox_to_anchor=(0.5, 1.05))

        out_path = out_dir / f"{filename_prefix}_page{page_idx}.png"
        fig.savefig(out_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        written_paths.append(out_path)

    return written_paths


def plot_flows_grid(
    sens_edge_pairs: List[Tuple[int, str]],
    edgedata_df: pd.DataFrame,
    sensor_df: pd.DataFrame,
    out_dir: Path,
    filename_prefix: str = "flow_grid",
    max_per_page: int = DEFAULT_MAX_PER_PAGE,
) -> List[Path]:

    if not sens_edge_pairs:
        raise ValueError("Nessuna coppia sensore/edge da plottare.")
    out_dir.mkdir(parents=True, exist_ok=True)

    written_paths = []
    for page_idx, page_pairs in enumerate(_grid_pages(sens_edge_pairs, max_per_page), start=1):
        n_cols = math.ceil(len(page_pairs) / 2)
        fig, axs = plt.subplots(
            2, n_cols,
            figsize=(WIDTH_PER_SUBPLOT * n_cols, HEIGHT_PER_SUBPLOT * 2),
            constrained_layout=True, squeeze=False, sharex=True,
        )
        axs_flat = axs.flatten()

        for ax, (cod_sens, edge_id) in zip(axs_flat, page_pairs):
            sim_samples, _ = hourly_sumo_flow(edgedata_df, edge_id)
            real_hourly = hourly_real_flow(sensor_df, cod_sens)

            ax.scatter(sim_samples["hour"], sim_samples["flow_veh_h"], label="SUMO Flow", alpha=0.6, s=15)
            ax.scatter(real_hourly.index, real_hourly.values, label="Real Flow", color="orange", s=25)
            ax.set_xlabel("Time (h)")
            ax.set_ylabel("flow")
            ax.set_title(f"Sensor {cod_sens} ({edge_id})", fontsize=10)

        for ax in axs_flat[len(page_pairs):]:
            ax.set_visible(False)

        handles = [
            plt.Line2D([0], [0], marker="o", linestyle="", label="SUMO Flow"),
            plt.Line2D([0], [0], marker="o", linestyle="", color="orange", label="Real Flow"),
        ]
        fig.legend(handles=handles, loc="upper center", ncol=2, bbox_to_anchor=(0.5, 1.05))

        out_path = out_dir / f"{filename_prefix}_page{page_idx}.png"
        fig.savefig(out_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        written_paths.append(out_path)

    return written_paths


def validate_edge_against_sensor(
    edgedata_df: pd.DataFrame,
    edge_id: str,
    sensor_df: pd.DataFrame,
    cod_sens: int,
    out_dir: Path,
) -> pd.Series:
    """
    Runs the full GEH validation for one edge/sensor pair, saves both reference plots.
    Returns the per-hour GEH series.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    sim_samples, sim_hourly = hourly_sumo_flow(edgedata_df, edge_id)
    real_hourly = hourly_real_flow(sensor_df, cod_sens)
    geh = geh_by_hour(sim_hourly, real_hourly)

    plot_geh_bars(geh, str(cod_sens), out_dir / f"geh_{edge_id}.png")
    plot_flow_comparison(sim_samples, real_hourly, str(cod_sens), out_dir / f"flow_{edge_id}.png")
    return geh


def plot_affinity_distribution(per_sensor_affinity: pd.Series, out_path: Path) -> dict:
    """
    Sensors sorted by (mean) affinity descending, plotted against their percentile rank (x: 0-100% of sensors, y: affinity %).
    High quality threshold = 75%, poor quality threshold = 50%.
    """
    sorted_aff = per_sensor_affinity.sort_values(ascending=False).reset_index(drop=True)
    n = len(sorted_aff)
    if n == 0:
        raise ValueError("No sensor to plot.")
    percentile = [100 * i / n for i in range(n)]

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.step(percentile, sorted_aff, where="post")
    ax.set_xlabel("Sensors [%]")
    ax.set_ylabel("Affinity [%]")
    ax.set_ylim(0, 100)
    ax.set_xlim(0, 100)
    ax.set_title(f"Quantitative validation analysis on sensors (n={n})")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)

    return {
        "pct_above_75": float(100 * (sorted_aff > 75).mean()),
        "pct_below_50": float(100 * (sorted_aff < 50).mean()),
    }


# def plot_bias_vs_variance(summary: dict, out_path: Path) -> None:
#     """
#     Scatter GEH (asse x) vs CV intra-ora (asse y) per ogni ora/sensore
#     disponibile, con la soglia di "CV anomalo" (dalle ore buone)
#     evidenziata — visualizza la separazione che variance_vs_bias_summary
#     quantifica in numeri.
#     """
#     combined = summary["combined"]
#     if combined.empty:
#         raise ValueError("Nessun dato da plottare (combined è vuoto).")

#     fig, ax = plt.subplots(figsize=(8, 6))
#     ax.scatter(combined["geh"], combined["cv"], alpha=0.6, s=25)
#     if "cv_threshold_from_good_p90" in summary:
#         ax.axhline(
#             summary["cv_threshold_from_good_p90"], color="red", linestyle="--",
#             label=f"soglia CV anomalo (90° perc. ore buone) = {summary['cv_threshold_from_good_p90']:.2f}",
#         )
#     ax.axvline(GEH_OK, color="green", linestyle=":", label=f"GEH buono (<={GEH_OK:.0f})")
#     ax.axvline(GEH_BAD, color="darkred", linestyle=":", label=f"GEH grave (>{GEH_BAD:.0f})")
#     ax.set_xlabel("GEH (per ora/sensore)")
#     ax.set_ylabel("CV intra-ora dei campioni SUMO")
#     ax.set_title("Bias vs varianza: separazione empirica")
#     ax.legend(fontsize=8)
#     ax.grid(alpha=0.3)
#     fig.tight_layout()
#     fig.savefig(out_path, dpi=150)
#     plt.close(fig)


def validate_all_sensors(
    edgedata_df: pd.DataFrame, sensor_df: pd.DataFrame, lookup_path: Path, out_dir: Path, cv_threshold: float = None,
) -> pd.DataFrame:
    """
    Validate each edge-sensor pair by plotting GEH, flows per edge, affinity index.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    pairs = _sensor_edge_pairs(edgedata_df, sensor_df, lookup_path)
    if not pairs:
        click.echo("[sensor_validation] no validated edges (no edge-sensor matches with available data.)")
        return pd.DataFrame()

    rows = []
    affinity_per_sensor = {}
    for cod_sens, edge_id in pairs:
        geh = validate_edge_against_sensor(edgedata_df, edge_id, sensor_df, cod_sens, out_dir)
        for hour, value in geh.items():
            rows.append({"edge_id": edge_id, "Cod_sens": cod_sens, "hour": hour, "geh": value})

        _, sim_hourly = hourly_sumo_flow(edgedata_df, edge_id)
        real_hourly = hourly_real_flow(sensor_df, cod_sens)
        affinity_h = affinity_by_hour(sim_hourly, real_hourly)
        if len(affinity_h) > 0:
            affinity_per_sensor[cod_sens] = {
                "edge_id": edge_id,
                "affinity_pct": affinity_h.mean(),
                "n_hours": len(affinity_h),
            }

    summary = pd.DataFrame(rows)
    if summary.empty:
        click.echo("[sensor_validation] no edge-sensor data.")
        return summary

    pct_ok = 100 * (summary["geh"] <= GEH_OK).mean()
    pct_caution = 100 * ((summary["geh"] > GEH_OK) & (summary["geh"] <= GEH_BAD)).mean()
    pct_bad = 100 * (summary["geh"] > GEH_BAD).mean()
    click.echo(
        f"[sensor_validation] {len(pairs)} validated edges, {len(summary)} sensors' hour samples."
    )
    click.echo(f"\t{pct_ok:.1f}% having GEH<={GEH_OK:.0f} (good fit)")
    click.echo(f"\t{pct_caution:.1f}% having {GEH_OK:.0f}<GEH<={GEH_BAD:.0f} (wrning)")
    click.echo(f"\t{pct_bad:.1f}% having GEH>{GEH_BAD:.0f} (wrong fit)")

    # worst = summary.groupby("edge_id")["geh"].mean().sort_values(ascending=False).head(10)
    # click.echo("\tpeggiori 10 edge (GEH medio):")
    # for eid, v in worst.items():
    #     click.echo(f"\t  {eid}: {v:.1f}")

    geh_grid_paths = plot_geh_grid(pairs, edgedata_df, sensor_df, out_dir)
    click.echo(f"\nGEH grid saved in: {', '.join(str(p) for p in geh_grid_paths)}")

    flow_grid_paths = plot_flows_grid(pairs, edgedata_df, sensor_df, out_dir)
    click.echo(f"\nFlows grid saved in: {', '.join(str(p) for p in flow_grid_paths)}")

    if affinity_per_sensor:
        affinity_df = (
            pd.DataFrame.from_dict(affinity_per_sensor, orient="index")
            .rename_axis("Cod_sens")
            .reset_index()
            .sort_values("affinity_pct", ascending=False)
        )

        affinity_plot_path = out_dir / "affinity_distribution.png"
        affinity_summary_plot = plot_affinity_distribution(
            affinity_df.set_index("Cod_sens")["affinity_pct"], affinity_plot_path
        )
        click.echo(f"\nAffinity plot saved in: {affinity_plot_path}")
        click.echo(
            f"\t{affinity_summary_plot['pct_above_75']:.0f}% sensor having affinity >75% (paper TuST: 80%)"
        )
        click.echo(
            f"\t{affinity_summary_plot['pct_below_50']:.0f}% sensor having affinity <50% (paper TuST: 5%)"
        )

        affinity_csv_path = out_dir / "affinity_by_sensor.csv"
        affinity_df.to_csv(affinity_csv_path, index=False)
        click.echo(
            f"\naffinity by sensor (n={len(affinity_df)}")
        #     f"con n così piccolo guarda questa tabella invece del solo grafico):"
        # )
        # click.echo(affinity_df.to_string(index=False, float_format=lambda v: f"{v:.1f}"))
        # click.echo(f"\tsalvata anche in {affinity_csv_path}")

    bv_summary = variance_vs_bias_summary(edgedata_df, sensor_df, pairs, cv_threshold=cv_threshold)
    if "note" in bv_summary:
        click.echo(f"\nbias-vs-varianza: {bv_summary['note']}")
    else:
        bv_plot_path = out_dir / "bias_vs_variance.png"
        plot_bias_vs_variance(bv_summary, bv_plot_path)
        click.echo("\nbias vs varianza (indizio quantitativo Ni et al. vs bias di domanda):")
        click.echo(
            f"\tCV mediano ore male (GEH>{GEH_BAD:.0f}): {bv_summary['median_cv_bad']:.2f} "
            f"(n={bv_summary['n_bad_hours']})"
        )
        click.echo(
            f"\tCV mediano ore buone (GEH<={GEH_OK:.0f}): {bv_summary['median_cv_good']:.2f} "
            f"(n={bv_summary['n_good_hours']})"
        )
        click.echo(
            f"\tsoglia CV anomalo usata: {bv_summary['cv_threshold_used']:.2f} "
            f"({bv_summary['cv_threshold_source']})"
        )
        if bv_summary.get("low_power_warning"):
            click.echo(
                f"\t[ATTENZIONE] soglia calcolata su meno di {MIN_GOOD_HOURS_FOR_THRESHOLD} ore buone: "
                "percentile poco affidabile. Usa --cv-threshold per riusare la soglia di un run più grande."
            )
        pct = bv_summary["pct_bad_hours_high_variance"]
        if pct > 0:
            click.echo(
                f"\t{pct:.0f}% delle ore cattive ha CV sopra la soglia — "
                "indizio a favore dell'instabilità (Ni et al.) per quella quota"
            )
        else:
            click.echo(
                "\t0% delle ore cattive ha CV sopra la soglia — nessun indizio di instabilità (Ni et al.); "
                "il gap in queste ore sembra spiegato da bias sistematico, non da varianza"
            )
        click.echo(f"\tgrafico salvato in {bv_plot_path}")
        
    return summary


@click.command(name="sensor-validation")
@click.option(
    "--edgedata", "edgedata_path", type=click.Path(exists=True, path_type=Path), required=True,
    help="edgeData XML già generato (es. edgeData_fine.xml scritto da "
        "meso_validation --sumocfg ...). Non rilancia SUMO.",
)
@click.option("--edge", "edge_id", default=None, help="Un singolo edge da validare.")
@click.option(
    "--from-sensors", is_flag=True,
    help="Valida tutti gli edge con un sensore corrispondente in cfg.SENS_LANE_LOOKUP "
        "e stampa la distribuzione aggregata (invece di un edge solo).",
)
@click.option(
    "--sensor-data", "sensor_data_path", type=click.Path(exists=True, path_type=Path), default=None,
    help="CSV già nello schema df_pasta (output di prepare_pasta_data()).",
)
@click.option(
    "--pasta-anagraphics", "anagraphics_path", type=click.Path(exists=True, path_type=Path), default=None,
    help="CSV grezzo anagrafica PASTA. Va con --pasta-flows.",
)
@click.option(
    "--pasta-flows", "flows_path", type=click.Path(exists=True, path_type=Path), default=None,
    help="CSV grezzo flussi PASTA. Va con --pasta-anagraphics.",
)
@click.option(
    "--out-dir", type=click.Path(path_type=Path), default=None,
    help="Cartella di output. Default: cfg.IMAGES_MESO_VALIDATION/sensor_validation.",
)
@click.option(
    "--cv-threshold", "cv_threshold", type=float, default=None,
    help="Soglia CV anomalo fissa da usare invece di ricalcolarla sulle ore buone di "
        "questo run (usa il valore 'cv_threshold_from_good_p90'/'cv_threshold_used' "
        "stampato da un run precedente su un campione più grande).",
)

def sensor_validation_cli(edgedata_path, edge_id, from_sensors, sensor_data_path, anagraphics_path, flows_path, out_dir, cv_threshold):
    """Valida l'output MESO contro i dati reali dei sensori PASTA (statistica GEH)."""
    import scripts.src.inputs.config as cfg
    import pandera.errors
    from scripts.src.analysis.wave_speed_calibration import load_validated_sensor_data, prepare_pasta_data

    if not edge_id and not from_sensors:
        raise click.UsageError("Serve --edge <id> oppure --from-sensors.")
    if sensor_data_path and (anagraphics_path or flows_path):
        raise click.UsageError("--sensor-data non si combina con --pasta-anagraphics/--pasta-flows.")
    if bool(anagraphics_path) != bool(flows_path):
        raise click.UsageError("--pasta-anagraphics e --pasta-flows vanno dati insieme.")
    if not (sensor_data_path or (anagraphics_path and flows_path)):
        raise click.UsageError("Servono dati sensore: --sensor-data oppure --pasta-anagraphics/--pasta-flows.")

    out_dir = out_dir or (cfg.IMAGES_MESO_VALIDATION / "sensor_validation")

    try:
        sensor_df = (
            load_validated_sensor_data(sensor_data_path) if sensor_data_path
            else prepare_pasta_data(pd.read_csv(anagraphics_path), pd.read_csv(flows_path))
        )
    except (pandera.errors.SchemaError, pandera.errors.SchemaErrors) as e:
        raise click.UsageError(f"Dati sensori non nello schema atteso:\n{e}")

    edgedata_df = load_edgedata(edgedata_path)

    if from_sensors:
        validate_all_sensors(edgedata_df, sensor_df, cfg.SENS_LANE_LOOKUP, out_dir, cv_threshold=cv_threshold)
        return

    lookup = pd.read_csv(cfg.SENS_LANE_LOOKUP)
    sensor_ids = lookup.loc[lookup["id_edge"].astype(str) == str(edge_id), "Cod_sens"].unique()
    if len(sensor_ids) == 0:
        raise click.UsageError(f"Nessun sensore trovato per l'edge {edge_id} in {cfg.SENS_LANE_LOOKUP}.")

    geh = validate_edge_against_sensor(edgedata_df, str(edge_id), sensor_df, sensor_ids[0], out_dir)
    click.echo(f"[sensor_validation] {edge_id} (sensore {sensor_ids[0]}): plot salvati in {out_dir}")
    click.echo(geh.to_string())


if __name__ == "__main__":
    sensor_validation_cli()