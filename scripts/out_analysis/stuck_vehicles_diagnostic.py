"""
Script diagnostico: isola i veicoli con durata di viaggio anomala nella coda
residua (es. run TLS-on con flow-penalty), recupera le loro route, e
verifica se sono concentrati geograficamente/strutturalmente su:
  (a) gli edge sub-15m (bug --meso-tls-penalty, issue #16014)
  (b) un piccolo insieme di edge ricorrenti (indizio di collo di bottiglia
      puntuale, es. corridoio Francia/Peschiera)

Uso:
    uv run python stuck_vehicles_diagnostic.py \
        --tripinfo /path/to/tripinfo.xml \
        --vehroute /path/to/vehroute.xml \
        --short-edges /path/to/sub15_edges.json \
        --net /path/to/network.net.xml \
        --duration-threshold 3600 \
        --top-n 30

Note:
- --short-edges è opzionale: un json con lista di edge id (puoi esportare
  la tua lista `sub_15` con json.dump([list(d.keys())[0] for d in sub_15], f)).
- --net è opzionale, serve solo per calcolare le coordinate medie degli
  edge sospetti (utile per un controllo visivo veloce in netedit).
"""

import argparse
import json
import statistics
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional


def parse_tripinfo(path: Path) -> Dict[str, dict]:
    """Ritorna {veh_id: {duration, waitingTime, timeLoss, departure, arrival}}"""
    tree = ET.parse(path)
    out = {}
    for trip in tree.getroot().findall("tripinfo"):
        vid = trip.get("id")
        out[vid] = {
            "duration": float(trip.get("duration", 0)),
            "waitingTime": float(trip.get("waitingTime", 0)),
            "timeLoss": float(trip.get("timeLoss", 0)),
            "depart": float(trip.get("depart", 0)),
            "arrival": float(trip.get("arrival", -1)),  # -1 se mai arrivato
        }
    return out


def parse_vehroute_edges(path: Path, vehicle_ids: set) -> Dict[str, List[str]]:
    """
    Ritorna {veh_id: [edge1, edge2, ...]} solo per i veicoli richiesti.
    Gestisce sia <route edges="..."/> diretta sia routeDistribution
    (prende la route con probability piu' alta, coerente con la logica
    gia' usata in assignment.py _select_route_element).
    """
    tree = ET.parse(path)
    root = tree.getroot()
    out = {}

    for veh in root.findall("vehicle"):
        vid = veh.get("id")
        if vid not in vehicle_ids:
            continue

        route_el = veh.find("route")
        if route_el is None:
            route_dist = veh.find("routeDistribution")
            if route_dist is not None:
                routes = route_dist.findall("route")
                if routes:
                    route_el = max(
                        routes, key=lambda r: float(r.get("probability", 0) or 0)
                    )
        if route_el is not None:
            out[vid] = route_el.get("edges", "").split()

    return out


def summarize_duration(trip_data: Dict[str, dict]) -> None:
    durations = [v["duration"] for v in trip_data.values()]
    never_arrived = sum(1 for v in trip_data.values() if v["arrival"] < 0)
    print(f"Veicoli totali (tripinfo): {len(durations)}")
    print(f"Mai arrivati (arrival=-1): {never_arrived}")
    print(f"Durata media: {statistics.mean(durations):.1f}s | mediana: {statistics.median(durations):.1f}s")
    p95 = statistics.quantiles(durations, n=100)[94]
    print(f"95° percentile durata: {p95:.1f}s ({p95/60:.1f} min)")
    # Confronto diretto col dato del paper TuST (95% < 15min36s = 936s)
    print(f"[rif. paper TuST: 95% dei viaggi < 936s / 15min36s]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tripinfo", type=Path, required=True)
    ap.add_argument("--vehroute", type=Path, required=True)
    ap.add_argument("--short-edges", type=Path, default=None)
    ap.add_argument("--net", type=Path, default=None)
    ap.add_argument("--duration-threshold", type=float, default=3600.0,
                     help="Soglia in secondi sopra cui un veicolo e' 'outlier' (default 3600 = 1h)")
    ap.add_argument("--top-n", type=int, default=30,
                     help="Quanti edge piu' frequenti mostrare nel riepilogo finale")
    args = ap.parse_args()

    trip_data = parse_tripinfo(args.tripinfo)
    summarize_duration(trip_data)

    outliers = {
        vid: d for vid, d in trip_data.items()
        if d["duration"] >= args.duration_threshold or d["arrival"] < 0
    }
    print(f"\nVeicoli outlier (durata >= {args.duration_threshold}s o mai arrivati): "
          f"{len(outliers)} ({100*len(outliers)/len(trip_data):.2f}% del totale)")

    if not outliers:
        print("Nessun outlier trovato con questa soglia: abbassa --duration-threshold.")
        return

    routes = parse_vehroute_edges(args.vehroute, set(outliers.keys()))
    print(f"Route recuperate per {len(routes)}/{len(outliers)} veicoli outlier "
          f"(alcuni potrebbero mancare se non presenti nel file vehroute).")

    edge_counter = Counter()
    for vid, edges in routes.items():
        edge_counter.update(edges)

    print(f"\nTop {args.top_n} edge piu' ricorrenti tra le route dei veicoli outlier:")
    top_edges = edge_counter.most_common(args.top_n)
    for eid, count in top_edges:
        print(f"  {eid}: presente in {count} route outlier")

    # Cross-reference con la lista di edge sub-15m, se fornita
    if args.short_edges and args.short_edges.exists():
        with open(args.short_edges) as f:
            short_edge_ids = set(json.load(f))
        overlap = [eid for eid, _ in top_edges if eid in short_edge_ids]
        print(f"\nDei {len(top_edges)} edge piu' frequenti, {len(overlap)} sono "
              f"anche nella lista sub-15m: {overlap}")
    else:
        print("\n[--short-edges non fornito: salto il confronto con gli edge sub-15m]")

    # Se disponibile il .net.xml, stampa nome via + coordinate dei top edge.
    # Il nome (attributo OSM 'name') e' spesso condiviso da piu' edge
    # consecutivi della stessa arteria: aggregarlo aiuta a leggere subito
    # se il collo di bottiglia e' un'unica via, senza controllo manuale.
    if args.net and args.net.exists():
        try:
            import sumolib
            net = sumolib.net.readNet(str(args.net))
            print(f"\nNome via + coordinate (centro edge) dei top {len(top_edges)} edge:")
            name_counter = Counter()
            for eid, count in top_edges:
                if net.hasEdge(eid):
                    name = net.getEdge(eid).getName() or "(senza nome OSM)"
                    name_counter[name] += count

            for eid, count in top_edges:
                if net.hasEdge(eid):
                    edge = net.getEdge(eid)
                    shape = edge.getShape()
                    mid = shape[len(shape) // 2]
                    name = edge.getName() or "(senza nome OSM)"
                    print(f"  {eid} (x{count}) -- {name} -- x={mid[0]:.1f}, y={mid[1]:.1f}")

            print(f"\nVie aggregate per numero totale di presenze (tra i top {len(top_edges)} edge):")
            for name, total in name_counter.most_common(10):
                print(f"  {name}: {total}")
        except ImportError:
            print("\n[sumolib non disponibile in questo ambiente: salto nomi/coordinate]")


if __name__ == "__main__":
    main()