"""
TuST section 4 routing logic applied by this script:

Script to produce and validate realistic traffic scenarios.

4.1  Road Graph + TAZ (see taz_zones.py):
4.2  Traffic Assignment macroscopico (see marouter_cycle.py)
4.3  Extension O'/D' using duarouter (see od_extension.py)

This module orchestrates the three steps together and exposes the CLI entry point.
"""

import random
import click
from typing import List, Optional
from colorama import init, Fore
from scripts.src.operations.taz_zones import AssignmentContext, build_context
from scripts.src.operations.marouter_cycle import (
    DEFAULT_DAY_SCALE,
    build_final_sumocfg,
    build_sumocfg_day,
    run_cycle,
    run_macro_assignment_day_iterative,
)
from scripts.src.operations.od_extension import extend_subset_trips

init(autoreset=True)

# Configuration
SCENARIOS_MA = ["MA_no_TLS", "MA_with_TLS"]
PERIODS_TO_RUN = ["AM", "PM"]  # "DAY" separately managed


# MAIN
def run_pipeline_for(
    scenario: str,
    period: str,
    ctx: AssignmentContext,
    n_rounds: int = 3,
    scouting_duration: Optional[int] = None,
):
    routes_macro_iterated = run_cycle(
        scenario, period, ctx,
        n_rounds=n_rounds,
        scouting_step=scouting_duration,
    )
    routes_final = extend_subset_trips(
        scenario, period, routes_macro_iterated, ctx
    )
    build_final_sumocfg(scenario, period, ctx.taz_file, routes_final)


def main(
    run_peaks: bool = True,
    run_day: bool = True,
    day_scale: float = DEFAULT_DAY_SCALE,
    day_rounds: int = 1,
    n_rounds: int = 3,
    scouting_duration: Optional[int] = None,
    scenarios: Optional[List[str]] = None,
    periods: Optional[List[str]] = None,
):
    random.seed(42) 
    scenarios = scenarios or SCENARIOS_MA
    periods = periods or PERIODS_TO_RUN
    ctx = build_context()

    for scenario in scenarios:
        if run_peaks:
            for period in periods:
                run_pipeline_for(
                    scenario, period, ctx,
                    n_rounds=n_rounds, scouting_duration=scouting_duration,
                ) 
        if run_day:
            routes_day = run_macro_assignment_day_iterative(
                scenario, ctx.taz_file, n_rounds=day_rounds, scale=day_scale
            )
            routes_day_final = extend_subset_trips(
                scenario, "DAY", routes_day, ctx
            )
            build_sumocfg_day(scenario, ctx.taz_file, routes_day_final, scale=day_scale)

    print(
        Fore.GREEN +
        f"Pipeline completed — peaks: {run_peaks} | day scenario (scale={day_scale}, "
        f"rounds={day_rounds}): {run_day}"
    )

@click.command()
@click.option("--day-only", is_flag=True, help="Only performs simulation for entire day.")
@click.option("--peaks-only", is_flag=True, help="Only performs simulation for peak scenarios (AM/PM).")
@click.option("--scale", type=float, default=DEFAULT_DAY_SCALE, show_default=True, help="Demand scale factor for DAY scenario.")
@click.option("--rounds", type=int, default=3, show_default=True, help="Number of rounds to perform feedback cycle for peak scenario.")
@click.option("--day-rounds", type=int, default=1, show_default=True, help="Number of rounds to perform feedback cycle for day scenario.")
@click.option("--scenario", "scenarios", multiple=True, type=click.Choice(SCENARIOS_MA), help="Scenarios to perform. (Default = MA_no_TLS + MA_with_TLS)")
@click.option("--scout-duration", type=int, default=None, help="Temporal limit for each round: end = begin + scouting instead of the simulation's termination.")
@click.option("--period", "periods", multiple=True, type=click.Choice(PERIODS_TO_RUN), help="Peak/s period/s to run (not allowed for DAY scenario).")

def cli(day_only, peaks_only, scale, day_rounds, rounds, scout_duration, scenarios, periods):
    if day_only and peaks_only:
        raise click.UsageError(Fore.RED + "ERROR:--day-only and --peaks-only are mutually exclusive!.")
    main(
        run_peaks=not day_only,
        run_day=not peaks_only,
        day_scale=scale,
        day_rounds=day_rounds,
        n_rounds=rounds,
        scouting_duration=scout_duration,
        scenarios=list(scenarios) or None,
        periods=list(periods) or None,
    )

if __name__ == "__main__":
    cli()