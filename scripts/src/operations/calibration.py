"""
extend_route_endpoints_coverage.py

Correzione rispetto a extend_route_endpoints.py: quella versione estendeva
una quota RANDOM di route (extend_share). Il paper TuST (Sez. 4.3) descrive
invece una selezione SISTEMATICA per TAZ: gli origin/destination non sono
piu' scelti a caso tra tutti gli edge della zona, ma specificamente tra
quelli residenziali/service, con l'obiettivo che ciascuno di essi venga
effettivamente usato come punto reale di partenza/arrivo almeno una volta
(non "in media di piu'", ma "sistematicamente coperto").

Questo script applica la stessa logica greedy set-cover di coverage_via.py,
ma al meccanismo sicuro di endpoint-extension (shortest-path splice) invece
che al via forzato a meta' percorso:

  1. Per ogni TAZ, individua il pool di edge locali (residenziali/service,
     priority < service_priority_threshold).
  2. Per ogni edge locale, trova le route la cui origine (o destinazione)
     appartiene a quella TAZ - sono le candidate a "spostare" il proprio
     endpoint su quell'edge locale specifico.
  3. Processa gli edge locali in ordine di scarsita' di candidati (i piu'
     difficili da coprire prima), assegna a ciascuno una route candidata
     (preferendo quelle gia' meno "usate" per spalmare il carico), calcola
     il connettore con shortest-path reale (sumolib) e lo innesta sulla
     route esistente.
  4. Ripete finche' ogni edge locale e' coperto (o non ci sono piu'
     candidati/connessioni valide).

Uso:
    from extend_route_endpoints_coverage import extend_route_endpoints_coverage

    output_path, stats = extend_route_endpoints_coverage(
        routes_xml_path="marouter_output_clean.rou.xml",
        edges=edges,
        taz_file=taz_file,
        net_file=net,
        output_path="marouter_output_extended.rou.xml",
        service_priority_threshold=5,   # residenziale/service, come nel paper
        max_uses_per_route=2,           # una route puo' essere estesa sia in origine che in destinazione
        max_connector_length=1500.0,
    )
    print(stats)
"""

import os
import sys
from tqdm import tqdm
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Dict


def _load_sumolib():
    try:
        import sumolib  # noqa
        return sumolib
    except ImportError:
        sumo_home = os.environ.get("SUMO_HOME")
        if sumo_home:
            sys.path.append(os.path.join(sumo_home, "tools"))
        try:
            import sumolib  # noqa
            return sumolib
        except ImportError:
            sys.exit("ERRORE: sumolib non trovato. Imposta SUMO_HOME.")


def _build_edge_to_taz(taz_file):
    tree = ET.parse(taz_file)
    root = tree.getroot()
    edge_to_taz = {}
    for taz in root.iter("taz"):
        tid = taz.get("id")
        for eid in taz.get("edges", "").split():
            edge_to_taz.setdefault(eid, []).append(tid)
    return edge_to_taz


def _build_local_pool_per_taz(taz_file, edges: Dict, service_priority_threshold: int,
                            valid_net_edges: set):
    tree = ET.parse(taz_file)
    root = tree.getroot()
    pools = {}
    for taz in root.iter("taz"):
        tid = taz.get("id")
        local_edges = []
        for eid in taz.get("edges", "").split():
            if eid not in valid_net_edges:
                continue
            priority = int(edges.get(eid, {}).get("priority", 20))
            if priority < service_priority_threshold:
                local_edges.append(eid)
        pools[tid] = local_edges
    return pools


def _cached_shortest_path(net, cache, from_id, to_id, max_connector_length):
    """
    Cache dei risultati Dijkstra per coppia (from_id, to_id): molte route
    condividono lo stesso edge di origine/destinazione, quindi la stessa
    coppia ricorre migliaia di volte - calcolarla una sola volta abbatte
    drasticamente il tempo totale.
    """
    key = (from_id, to_id)
    if key in cache:
        return cache[key]
    path_edges, cost = net.getShortestPath(net.getEdge(from_id), net.getEdge(to_id))
    if not path_edges or cost is None or cost > max_connector_length:
        result = None
    else:
        result = [e.getID() for e in path_edges]
    cache[key] = result
    return result


def _splice_origin(net, cache, edge_list, new_origin, max_connector_length):
    old_origin = edge_list[0]
    path_ids = _cached_shortest_path(net, cache, new_origin, old_origin, max_connector_length)
    if path_ids is None:
        return None
    if path_ids and path_ids[-1] == old_origin:
        path_ids = path_ids[:-1]
    return path_ids + edge_list


def _splice_destination(net, cache, edge_list, new_dest, max_connector_length):
    old_dest = edge_list[-1]
    path_ids = _cached_shortest_path(net, cache, old_dest, new_dest, max_connector_length)
    if path_ids is None:
        return None
    if path_ids and path_ids[0] == old_dest:
        path_ids = path_ids[1:]
    return edge_list + path_ids


def extend_route_endpoints_coverage(routes_xml_path, edges: Dict, taz_file, net_file,
                                     output_path, service_priority_threshold=5,
                                     max_uses_per_route=2, max_connector_length=1500.0):
    """
    Estende sistematicamente le route affinche' ogni edge locale
    (residenziale/service) di ogni TAZ venga usato almeno una volta come
    punto reale di origine o destinazione, invece di una quota random.

    Ritorna (output_path, stats).
    """
    sumolib = _load_sumolib()
    net = sumolib.net.readNet(str(net_file), withInternal=False)
    valid_net_edges = {e.getID() for e in net.getEdges()}

    edge_to_taz = _build_edge_to_taz(taz_file)
    local_pools = _build_local_pool_per_taz(taz_file, edges, service_priority_threshold,
                                             valid_net_edges)

    tree = ET.parse(routes_xml_path)
    root = tree.getroot()
    route_elements = [r for r in root.iter("route") if r.get("edges")]
    n_total = len(route_elements)

    # route -> lista corrente di edge (mutabile durante il processo)
    current_edges = {i: route_elements[i].get("edges").split() for i in range(n_total)}
    uses_per_route = defaultdict(int)

    # candidati per ogni edge locale target, sia per origine che per destinazione
    origin_candidates_by_local_edge = defaultdict(list)   # local_edge -> [route_idx,...]
    dest_candidates_by_local_edge = defaultdict(list)

    for i in range(n_total):
        edge_list = current_edges[i]
        if not edge_list:
            continue
        origin_taz_ids = edge_to_taz.get(edge_list[0], [])
        for tid in origin_taz_ids:
            for local_edge in local_pools.get(tid, []):
                origin_candidates_by_local_edge[local_edge].append(i)

        dest_taz_ids = edge_to_taz.get(edge_list[-1], [])
        for tid in dest_taz_ids:
            for local_edge in local_pools.get(tid, []):
                dest_candidates_by_local_edge[local_edge].append(i)

    path_cache = {}  # condivisa tra origine e destinazione: stesse coppie edge possono ricorrere

    def _process(candidates_by_local_edge, splice_fn, label, max_tries_per_edge=25):
        covered = 0
        no_candidates = 0
        no_valid_connector = 0
        order = sorted(candidates_by_local_edge.items(), key=lambda kv: len(kv[1]))
        n_local_edges = len(order)
        for progress_i, (local_edge, route_idxs) in enumerate(order):
            if progress_i % 200 == 0:
                print(f"[extend_route_endpoints_coverage] {label}: "
                    f"{progress_i}/{n_local_edges} edge locali processati, "
                    f"cache size={len(path_cache)}")
            route_idxs_sorted = sorted(route_idxs, key=lambda r: uses_per_route[r])
            done = False
            tries = 0
            for r in route_idxs_sorted:
                if uses_per_route[r] >= max_uses_per_route:
                    continue
                if tries >= max_tries_per_edge:
                    # tetto ai tentativi: se dopo N prove nessun connettore
                    # valido e' stato trovato, molto probabilmente quell'edge
                    # locale e' troppo lontano da TUTTI i candidati - non ha
                    # senso continuare a provarne altri identici in pratica
                    break
                tries += 1
                new_edges = splice_fn(net, path_cache, current_edges[r], local_edge,
                                    max_connector_length)
                if new_edges is None:
                    continue
                current_edges[r] = new_edges
                uses_per_route[r] += 1
                covered += 1
                done = True
                break
            if not done:
                no_valid_connector += 1
        return covered, no_candidates, no_valid_connector

    print(f"[extend_route_endpoints_coverage] avvio elaborazione origine "
        f"({len(origin_candidates_by_local_edge)} edge locali target)...")
    origin_covered, _, origin_no_conn = _process(
        origin_candidates_by_local_edge, _splice_origin, "origine"
    )
    print(f"[extend_route_endpoints_coverage] avvio elaborazione destinazione "
        f"({len(dest_candidates_by_local_edge)} edge locali target)...")
    dest_covered, _, dest_no_conn = _process(
        dest_candidates_by_local_edge, _splice_destination, "destinazione"
    )

    for i in tqdm(range(n_total), desc=f"Resetting edges"):
        route_elements[i].set("edges", " ".join(current_edges[i]))

    ET.indent(root, space="  ", level=0)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)

    stats = {
        "routes_total": n_total,
        "local_edges_origin_target": len(origin_candidates_by_local_edge),
        "local_edges_origin_covered": origin_covered,
        "local_edges_dest_target": len(dest_candidates_by_local_edge),
        "local_edges_dest_covered": dest_covered,
        "origin_no_valid_connector": origin_no_conn,
        "dest_no_valid_connector": dest_no_conn,
        "routes_modified": sum(1 for v in uses_per_route.values() if v > 0),
    }

    print(f"[extend_route_endpoints_coverage] route totali: {n_total}")
    print(f"[extend_route_endpoints_coverage] edge locali ORIGINE coperti: "
        f"{origin_covered}/{len(origin_candidates_by_local_edge)} "
        f"({origin_no_conn} senza connettore valido)")
    print(f"[extend_route_endpoints_coverage] edge locali DESTINAZIONE coperti: "
        f"{dest_covered}/{len(dest_candidates_by_local_edge)} "
        f"({dest_no_conn} senza connettore valido)")
    print(f"[extend_route_endpoints_coverage] route modificate: {stats['routes_modified']}/{n_total}")

    return output_path, stats