"""
@file       sumoResults.py
@author     Irene Marta
@date       2026

Useful visualisations
"""

import seaborn as sns
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import xml.etree.ElementTree as ET
import pandas as pd
from pathlib import Path

import programmi.config_OLD as cfg

# from programmi.allDayOD import AM_PEAK, PM_PEAK

from typing import Dict, List, TypedDict
from typing import TypedDict

PALETTE_RUNNING = sns.color_palette("Blues", as_cmap=True)
PALETTE_FAILURES = sns.color_palette("coolwarm", as_cmap=True)


def parse_sumo_summary(file_path: Path) -> pd.DataFrame:
    print("\tParsing SUMO summary")
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        data = []
        for step in root.findall("step"):
            data.append(step.attrib)
        df_summary = pd.DataFrame(data).apply(pd.to_numeric)

        # dfs_seconds = {label: parse_sumo_summary(path) for label, path in zip(labels, path_list)}

        return df_summary

    except ET.ParseError as e:
        raise ValueError(f"ERROR:\t Could not parse XML file:{e}")
    except FileNotFoundError:
        raise FileNotFoundError(f"ERROR:\t File not found at {file_path}")


def datetime_format(df_summary):
    df_summary["time"] = pd.to_datetime(df_summary["time"], unit="s")
    df_summary["meanWaitingTime_dt"] = pd.to_datetime(
        df_summary["meanWaitingTime"], unit="s"
    )
    df_summary["meanTravelTime_dt"] = pd.to_datetime(
        df_summary["meanTravelTime"], unit="s"
    )

    return df_summary


def ax_style(
    ax,
    xlim: int,
    title: str,
    ylabel: str,
    load_start: int,
    load_end: int,
    load_start_2: int = None,
    load_end_2: int = None,
    xlabel: str = "Time (h)",
):

    # X-axis time conversion
    xlim_dt = pd.to_datetime(xlim, unit="s")
    load_start_dt = pd.to_datetime(load_start, unit="s")
    load_end_dt = pd.to_datetime(load_end, unit="s")

    ax.axvspan(load_start_dt, load_end_dt, color="blue", alpha=0.1, label="Load")
    if load_start_2 is not None and load_end_2 is not None:
        load_start_dt_2 = pd.to_datetime(load_start_2, unit="s")
        load_end_dt_2 = pd.to_datetime(load_end_2, unit="s")
        ax.axvspan(
            load_start_dt_2,
            load_end_dt_2,
            color="orange",
            linestyle="--",
            alpha=0.1,
            label="Load",
        )
    elif load_start_2 is not None or load_end_2 is not None:
        raise ValueError("Both load_start_2 and load_end_2 must be provided together.")

    ax.set_title(title, fontweight="semibold", pad=10)
    ax.set_ylabel(ylabel)
    ax.set_xlim(right=xlim_dt)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.legend(loc="best", frameon=False, fontsize="small")


def plot_summary(
    df_summary: pd.DataFrame,
    title: str,
    xlim: int,
    load_start: int,
    load_end: int,
    load_start_2: int = None,
    load_end_2: int = None,
):

    sns.set_theme(style="whitegrid")

    fig, axs = plt.subplots(3, 2, figsize=(16, 12), constrained_layout=True)
    fig.suptitle(f"SUMO Summary - {title}", fontsize=14, fontweight="bold")

    # RUNNING VS HALTING
    sns.lineplot(
        data=df_summary,
        x="time",
        y="running",
        ax=axs[0, 0],
        label="Running",
        color="blue",
    )
    sns.lineplot(
        data=df_summary,
        x="time",
        y="halting",
        ax=axs[0, 0],
        label="Halting",
        color="red",
    )
    ax_style(
        ax=axs[0, 0],
        xlim=xlim,
        title="Net congestion",
        ylabel="Number of vehicles",
        load_start=load_start,
        load_end=load_end,
        load_start_2=load_start_2,
        load_end_2=load_end_2,
    )

    # INSERTED VS ARRIVED
    sns.lineplot(
        data=df_summary,
        x="time",
        y="inserted",
        ax=axs[0, 1],
        label="Inserted",
        color="green",
    )
    sns.lineplot(
        data=df_summary,
        x="time",
        y="arrived",
        ax=axs[0, 1],
        label="Arrived",
        color="darkred",
    )
    ax_style(
        ax=axs[0, 1],
        xlim=xlim,
        title="Inserted vs Arrived",
        ylabel="Cumulative total",
        load_start=load_start,
        load_end=load_end,
        load_start_2=load_start_2,
        load_end_2=load_end_2,
    )

    # SPEED CHECK
    ax_speed_rel = axs[1, 0].twinx()  # NO ASSE UGUALE, MEAN RELATIVE è SCALATA
    sns.lineplot(
        data=df_summary,
        x="time",
        y="meanSpeed",
        ax=axs[1, 0],
        color="lightblue",
        label="AVG speed (m/s)",
    )
    sns.lineplot(
        data=df_summary,
        x="time",
        y="meanSpeedRelative",
        ax=ax_speed_rel,
        color="grey",
        label="AVG Rel Speed (%)",
        alpha=0.5,
    )
    ax_style(
        ax=axs[1, 0],
        xlim=xlim,
        title="Average speed and average relative speed",
        ylabel="Speed (m/s)",
        load_start=load_start,
        load_end=load_end,
    )

    ax_speed_rel.set_ylabel("Ratio with respect to the limit")
    # set combined legend
    lines, labels = axs[1, 0].get_legend_handles_labels()
    lines2, labels2 = ax_speed_rel.get_legend_handles_labels()
    ax_speed_rel.legend(lines + lines2, labels + labels2, loc="best")
    axs[1, 0].get_legend().remove()

    # AVERAGE TIME
    sns.lineplot(
        data=df_summary,
        x="time",
        y="meanWaitingTime_dt",
        ax=axs[1, 1],
        color="orange",
        label="AVG waiting time",
    )
    sns.lineplot(
        data=df_summary,
        x="time",
        y="meanTravelTime_dt",
        ax=axs[1, 1],
        color="green",
        label="AVG travel time",
    )
    ax_style(
        ax=axs[1, 1],
        xlim=xlim,
        title="Running and waiting times",
        ylabel="Time (h)",
        load_start=load_start,
        load_end=load_end,
        load_start_2=load_start_2,
        load_end_2=load_end_2,
    )
    axs[1, 1].yaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

    # INFRASTRUCTURE PERFORMANCE
    sns.lineplot(
        data=df_summary,
        x="time",
        y="inserted",
        label="Inserted",
        color="#2E7D32",
        linewidth=2,
        ax=axs[2, 0],
    )  # Number of vehicles inserted so far (including reported time step)
    sns.lineplot(
        data=df_summary,
        x="time",
        y="loaded",
        label="Loaded",
        color="#FFA000",
        ax=axs[2, 0],
    )  # Number of vehicles that were loaded from input files up to this time step. This can included vehicle with depart times in the future.

    sns.lineplot(
        data=df_summary,
        x="time",
        y="waiting",
        label="Waiting",
        color="#F70A0A",
        linewidth=2,
        ax=axs[2, 0],
    )  # Number of vehicles inserted so far (including reported time step)

    ax_style(
        ax=axs[2, 0],
        xlim=xlim,
        title="Infrastructure performance",
        ylabel="Number of Vehicles",
        load_start=load_start,
        load_end=load_end,
        load_start_2=load_start_2,
        load_end_2=load_end_2,
    )

    # INCONVENIENCES
    sns.lineplot(
        data=df_summary,
        x="time",
        y="collisions",
        label="Collisions",
        color="#CA4514",
        linewidth=2,
        ax=axs[2, 1],
    )
    sns.lineplot(
        data=df_summary,
        x="time",
        y="discarded",
        label="Discarded",
        color="#DF0707",
        ax=axs[2, 1],
    )

    sns.lineplot(
        data=df_summary,
        x="time",
        y="teleports",
        label="Teleports",
        color="#0951CE",
        linewidth=2,
        ax=axs[2, 1],
    )

    ax_style(
        ax=axs[2, 1],
        xlim=xlim,
        title="Simulation failures",
        ylabel="Number of events",
        load_start=load_start,
        load_end=load_end,
        load_start_2=load_start_2,
        load_end_2=load_end_2,
    )

    # Shared x-axes
    for ax in [axs[0, 1], axs[2, 0], axs[2, 1]]:
        ax.sharey(axs[2, 0])

    plt.savefig(output_dir / f"dashboard_{title}", bbox_inches="tight", dpi=300)
    plt.close()


def plot_net_performace(
    df_tls: pd.DataFrame,
    df_no_tls: pd.DataFrame,
    xlim: int,
    title: str,
    load_start: int,
    load_end: int,
    load_start_2: int = None,
    load_end_2: int = None,
):

    sns.set_theme(style="whitegrid")

    fig, axs = plt.subplots(3, 1, figsize=(20, 14), constrained_layout=True)
    fig.suptitle(f"Net congestion comparison - {title}", fontsize=18, fontweight="bold")

    sns.lineplot(
        data=df_tls, x="time", y="running", color="blue", label="ON", ax=axs[0]
    )
    sns.lineplot(
        data=df_no_tls,
        x="time",
        y="running",
        label="OFF",
        color="blue",
        ax=axs[0],
        linestyle="-",
        linewidth=2,
    )

    sns.lineplot(data=df_tls, x="time", y="halting", color="red", label="ON", ax=axs[1])

    sns.lineplot(
        data=df_no_tls,
        x="time",
        y="halting",
        label="OFF",
        color="red",
        ax=axs[1],
        linestyle="-",
        linewidth=2,
    )

    sns.lineplot(
        data=df_tls, x="time", y="teleports", color="darkviolet", label="ON", ax=axs[2]
    )

    sns.lineplot(
        data=df_no_tls,
        x="time",
        y="teleports",
        label="OFF",
        color="darkviolet",
        ax=axs[2],
        linestyle="-",
        linewidth=2,
    )

    ax_style(
        ax=axs[0],
        xlim=xlim,
        title="Running",
        ylabel="Number of Vehicles",
        load_start=load_start,
        load_end=load_end,
        load_start_2=load_start_2,
        load_end_2=load_end_2,
    )

    ax_style(
        ax=axs[1],
        xlim=xlim,
        title="Halting",
        ylabel="Number of Vehicles",
        load_start=load_start,
        load_end=load_end,
        load_start_2=load_start_2,
        load_end_2=load_end_2,
    )

    ax_style(
        ax=axs[2],
        xlim=xlim,
        title="Teleports",
        ylabel="Number of Vehicles",
        load_start=load_start,
        load_end=load_end,
        load_start_2=load_start_2,
        load_end_2=load_end_2,
    )

    handles, labels = plt.gca().get_legend_handles_labels()
    dedup_label = dict(zip(labels, handles))
    fig.legend(
        dedup_label.keys(),
        dedup_label.values(),
        loc="upper right",
        bbox_to_anchor=(1, 1),
        fancybox=True,
    )

    plt.savefig(output_dir / f"congestion_{title}", bbox_inches="tight", dpi=300)
    plt.close()


def plot_time_metrics(
    df_tls: pd.DataFrame,
    df_no_tls: pd.DataFrame,
    xlim: int,
    title: str,
    output_dir: Path,
    load_start: int,
    load_end: int,
    load_start_2: int = None,
    load_end_2: int = None,
):

    sns.set_theme(style="whitegrid")

    fig, axs = plt.subplots(2, 1, figsize=(10, 8), constrained_layout=True, sharey=True)
    fig.suptitle(f"Time metrics comparison - {title}", fontsize=18, fontweight="bold")

    def single_time_plot(df: pd.DataFrame, ax, title_subplot: str):
        sns.lineplot(
            data=df,
            x="time",
            y="meanWaitingTime_dt",
            ax=ax,
            color="orange",
            label="AVG waiting time",
        )
        sns.lineplot(
            data=df,
            x="time",
            y="meanTravelTime_dt",
            ax=ax,
            color="green",
            label="AVG travel time",
        )
        ax_style(
            ax=ax,
            xlim=xlim,
            title=title_subplot,
            ylabel="Time (h)",
            load_start=load_start,
            load_end=load_end,
            load_start_2=load_start_2,
            load_end_2=load_end_2,
        )

    single_time_plot(df_tls, axs[0], "With 2 TLS on")
    single_time_plot(df_no_tls, axs[1], "All TLS off")

    for i in range(len(axs)):
        axs[i].yaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

    plt.savefig(output_dir / f"timeMet_{title}", bbox_inches="tight", dpi=300)
    plt.close()


class AlgoInfo(TypedDict):
    title: str
    df: pd.DataFrame


def compare_algos(
    algo_dataframes: List[AlgoInfo],
    xlim: int,
    title: str,
    load_start: int,
    load_end: int,
    load_start_2: int = None,
    load_end_2: int = None,
):

    sns.set_theme(style="whitegrid")

    fig, axs = plt.subplots(3, 1, figsize=(20, 14), constrained_layout=True)
    algos_string = " vs. ".join([x["title"] for x in algo_dataframes])
    fig.suptitle(
        f"Net congestion comparison - {algos_string} ({title})",
        fontsize=18,
        fontweight="bold",
    )

    for a in algo_dataframes:
        sns.lineplot(
            data=a["df"],
            x="time",
            y="running",
            label=a["title"],
            palette=PALETTE_RUNNING,
            ax=axs[0],
        )

        sns.lineplot(
            data=a["df"],
            x="time",
            y="halting",
            palette=PALETTE_RUNNING,
            label=a["title"],
            ax=axs[1],
        )

        sns.lineplot(
            data=a["df"],
            x="time",
            y="teleports",
            palette=PALETTE_FAILURES,
            label=["title"],
            ax=axs[2],
        )

    ax_style(
        ax=axs[0],
        xlim=xlim,
        title="Running",
        ylabel="Number of Vehicles",
        load_start=load_start,
        load_end=load_end,
        load_start_2=load_start_2,
        load_end_2=load_end_2,
    )

    ax_style(
        ax=axs[1],
        xlim=xlim,
        title="Halting",
        ylabel="Number of Vehicles",
        load_start=load_start,
        load_end=load_end,
        load_start_2=load_start_2,
        load_end_2=load_end_2,
    )

    ax_style(
        ax=axs[2],
        xlim=xlim,
        title="Teleports",
        ylabel="Number of Vehicles",
        load_start=load_start,
        load_end=load_end,
        load_start_2=load_start_2,
        load_end_2=load_end_2,
    )

    handles, labels = plt.gca().get_legend_handles_labels()
    dedup_label = dict(zip(labels, handles))
    fig.legend(
        dedup_label.keys(),
        dedup_label.values(),
        loc="upper right",
        bbox_to_anchor=(1, 1),
        fancybox=True,
    )

    plt.savefig(output_dir / f"CompareAlgos_{title}", bbox_inches="tight", dpi=300)
    plt.close()


def main():

    SUMMARY_PATHS = {
        ("no_TLS", "AM"): cfg.SUMO_OUTPUT / "no_TLS/Summary_morning.xml",
        ("no_TLS", "PM"): cfg.SUMO_OUTPUT / "no_TLS/Summary_night.xml",
        ("no_TLS", "DAY"): cfg.SUMO_OUTPUT / "no_TLS/Summary_day.xml",
        ("with_TLS", "AM"): cfg.SUMO_OUTPUT / "with_TLS/Summary_morning.xml",
        ("with_TLS", "PM"): cfg.SUMO_OUTPUT / "with_TLS/Summary_night.xml",
        ("with_TLS", "DAY"): cfg.SUMO_OUTPUT / "with_TLS/Summary_day.xml",
    }

    OUTPUT_DIRS = {
        "AM": cfg.OUTPUT_DASHBOARDS / "duarouter/out-DataExtr-OD-morning",
        "PM": cfg.OUTPUT_DASHBOARDS / "duarouter/out-DataExtr-OD-evening",
        "DAY": cfg.OUTPUT_DASHBOARDS / "out-DataExtr-OD-allDay",
    }

    summaries = {}
    for (scenario, period), path in SUMMARY_PATHS.items():
        df = parse_sumo_summary(path)
        summaries[(scenario, period)] = datetime_format(df)

    # shorthand to summaries
    am_no = summaries[("no_TLS", "AM")]
    am_tls = summaries[("with_TLS", "AM")]
    pm_no = summaries[("no_TLS", "PM")]
    pm_tls = summaries[("with_TLS", "PM")]
    day_no = summaries[("no_TLS", "DAY")]
    day_tls = summaries[("with_TLS", "DAY")]

    # AM PEAK
    plot_summary(
        am_tls,
        title="AM - 2 TLS on",
        xlim=36000,
        load_start=8 * 3600,
        load_end=9 * 3600,
    )
    plot_net_performace(
        df_no_tls=am_no,
        df_tls=am_tls,
        xlim=36000,
        title="Morning peak",
        load_start=8 * 3600,
        load_end=9 * 3600,
    )
    plot_time_metrics(
        df_tls=am_tls,
        df_no_tls=am_no,
        xlim=10 * 3600,
        title="Morning Peak",
        load_start=8 * 3600,
        load_end=9 * 3600,
    )

    # PM PEAK
    plot_summary(
        pm_no,
        title="PM - all TLS off",
        xlim=72000,
        load_start=18 * 3600,
        load_end=19 * 3600,
    )
    plot_summary(
        pm_tls,
        title="PM - 2 TLS on",
        xlim=72000,
        load_start=18 * 3600,
        load_end=19 * 3600,
    )
    plot_net_performace(
        df_no_tls=pm_no,
        df_tls=pm_tls,
        xlim=72000,
        title="Evening peak",
        load_start=18 * 3600,
        load_end=19 * 3600,
    )
    plot_time_metrics(
        df_tls=pm_tls,
        df_no_tls=pm_no,
        xlim=20 * 3600,
        title="Evening Peak",
        load_start=18 * 3600,
        load_end=19 * 3600,
    )

    # ALL DAY
    plot_summary(
        day_no,
        title="DAY - all TLS off",
        xlim=24 * 3600,
        load_start=8 * 3600,
        load_end=9 * 3600,
        load_start_2=18 * 3600,
        load_end_2=19 * 3600,
    )
    plot_summary(
        day_tls,
        title="DAY - 2 TLS on",
        xlim=24 * 3600,
        load_start=8 * 3600,
        load_end=9 * 3600,
        load_start_2=18 * 3600,
        load_end_2=19 * 3600,
    )
    plot_time_metrics(
        df_tls=am_tls,
        df_no_tls=am_no,
        xlim=10 * 3600,
        title="All day",
        load_start=8 * 3600,
        load_end=9 * 3600,
    )
    plot_time_metrics(
        df_tls=day_tls,
        df_no_tls=day_no,
        xlim=24 * 3600,
        title="All day",
        load_start=8 * 3600,
        load_end=9 * 3600,
        load_start_2=18 * 3600,
        load_end_2=19 * 3600,
    )


if __name__ == "__main__":
    main()
