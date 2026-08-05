"""
@file       parse.py
@author     Irene Marta
@date       2026

This script aims at producing both static and dynamix xml files
related to a specific net file (input).

Workflow:
1. It creates static xml files - .nod, .edg, .typ, .con, .tll, .round - starting from a net file;
every file is then added to a dictionary
2. It saves all the xml files into the output directory
3. It creates dynamic xmls by leveraging subprocesses (duarouter and randomTirps)
4. It reads the SUMO net and finally adds up all the functions in a finals export function,
exporting all the files (both static and dynamic xmls) to the output directory

"""

import os
import sys
from typing import Dict

# Set environment variable and import sumolib -> more info here: https://sumo.dlr.de/docs/Tools/Sumolib.html
if "SUMO_HOME" in os.environ:
    sys.path.append(os.path.join(os.environ["SUMO_HOME"], "tools"))

import sumolib

from sumolib.net import Net
from sumolib.net.lane import SUMO_VEHICLE_CLASSES # to define disallowed vehicles over edges

import xml.etree.ElementTree as ET
from xml.etree.ElementTree import indent  # file layout

from pyproj import Transformer

import math

from scripts.src.helpers import format_xml
import scripts.src.inputs.config as cfg
from scripts.src.operations.cmd import random_routes

# from UTM 32N to WGS84
transformer = Transformer.from_crs("EPSG:32632", "EPSG:4326", always_xy=True)


# see sumolib python scripts for more on used functions (net directory) -> leverage specific functions for sumo objects/files


# Function to create a dictionary with all xml files (for nodes, connections, edges, types of edges, tll, roundabout)
def static_xml_files(net, map_name="francia_peschiera") -> Dict:
    print("\nCREATING STATIC XML FILES\t")
    xml = {}

    # nod.xml
    nodes_root = ET.Element(
        "nodes",
        {
            "version": "1.20",
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:noNamespaceSchemaLocation": "http://sumo.dlr.de/xsd/nodes_file.xsd",
        },
    )

    """
    class xml.etree.ElementTree.ElementTree(element=None, file=None)
        ElementTree represents the whole XML document as a tree, and Element represents a single node in this tree.

    class xml.etree.ElementTree.Element(tag, attrib={}, **extra)
        Element class. This class defines the Element interface, and provides a reference implementation of this interface."""

    for node in net.getNodes():
        x, y = node.getCoord()  # UTM coordinates
        lon, lat = transformer.transform(x, y)  # conversion in lat/lon coordinates
        # To get matches with the Google API csv database
        # lon = round(lon, 6)
        # lat = round(lat, 6)

        """get(key, default=None)
            Gets the element attribute named key.
            Returns the attribute value, or default if the attribute was not found."""

        # Creation of subelements (and dictionary with correlatred attributes)

        """The SubElement() function also provides a convenient way to create new sub-elements for a given element.
        
        xml.etree.ElementTree.SubElement(parent, tag, attrib={}, **extra)
        Subelement factory. This function creates an element instance, and appends it to an existing element.

        The element name, attribute names, and attribute values can be either bytestrings or Unicode strings. 
        parent is the parent element. 
        tag is the subelement name. attrib is an optional dictionary, containing element attributes. 
        extra contains additional attributes, given as keyword arguments. Returns an element instance.

        """

        ET.SubElement(
            nodes_root,
            "node",
            {
                "id": node.getID(),
                "x": str(lon),
                "y": str(lat),
                "type": node.getType() if node.getType() else "",
            },
        )

    xml[map_name + ".nod.xml"] = nodes_root

    # con.xml
    connections_root = ET.Element(
        "connections",
        {
            "version": "1.20",
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:noNamespaceSchemaLocation": "http://sumo.dlr.de/xsd/connections_file.xsd",
        },
    )

    for edge in net.getEdges():
        for lane in edge.getLanes():  # see lane.py
            for conn in lane.getOutgoing():
                ET.SubElement(
                    connections_root,
                    "connection",
                    {
                        "from": conn.getFromLane().getEdge().getID(),
                        "to": conn.getToLane().getEdge().getID(),
                        "fromLane": str(conn.getFromLane().getIndex()),
                        "toLane": str(conn.getToLane().getIndex()),
                    },
                )

    xml[map_name + ".con.xml"] = connections_root

    # typ.xml
    # TODO: codifica di vehicles allow/disallow
    # HACK: disallow/allow classes have been hardcoded in orrder to ensure only correct flows in the simulation
    type_root = ET.Element(
        "types",
        {
            "version": "1.20",
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:noNamespaceSchemaLocation": "http://sumo.dlr.de/xsd/types_file.xsd",
        },
    )

    # 1. Collect all the street types of the net file
    type_ids: Dict[str, list] = {}  # the edge attribute "type" is the type attribute "id"
    for edge in net.getEdges():
        id_type = getattr(edge, "_type", "").strip().lower()

        """
        The getattr() function returns the value of the specified attribute from the specified object.
        SYNTAX: getattr(object, attribute, default)
        """
        if not id_type:
            continue
        if id_type not in type_ids:
            type_ids[id_type] = []  # list of all edges for each dictionary's entry
        type_ids[id_type].append(edge)

    # 2. Collect parameters for each edge type in the list
    for id_type, edges in type_ids.items():

        # Use the first value (edge) in the list as sample for the specific type
        sample_edge = edges[0]

        # Check again for disallowed classes of vehicles for every edge (all have the same attributes values)
        disallowed = set()
        for edge in edges:
            for lane in edge.getLanes():
                allowed_vclasses = lane.getPermissions()
            if allowed_vclasses is None:
                allowed_vclasses = SUMO_VEHICLE_CLASSES  # all allowed

            # disallow = all vehicles which aren't in the allowed_vclasses variable
            disallowed |= SUMO_VEHICLE_CLASSES - set(allowed_vclasses)

        disallowed_str = " ".join(sorted(disallowed))

        # Check the oneway attribute: if - then the type has 2 directions
        oneway = "1"  # assume one direction

        """
        QUESTO CONTROLLO NON BASTA

        for e in edges:
            edge_id = e.getID()
            if edge_id[0] == "-":
                oneway = "0"
                break
        """
        # Check by swapping "from" - "to" edges
        for e in edges:
            from_e = e.getFromNode().getID()
            to_e = e.getToNode().getID()
            for reversed in edges:
                if (
                    reversed.getFromNode().getID() == to_e
                    and reversed.getToNode().getID() == from_e
                ):
                    oneway = "0"  # at least one zero in the net
                    break

        # see file edge.py + ___init___.py + lane.py for class definitions
        ET.SubElement(
            type_root,
            "type",
            {
                "id": id_type,
                "priority": str(sample_edge.getPriority()),
                "numLanes": str(len(sample_edge.getLanes())),
                "speed": str(sample_edge.getSpeed()),
                "disallow": disallowed_str,
                "oneway": oneway,
            },
        )

    xml[map_name + ".typ.xml"] = type_root

    # edg.xml
    edges_root = ET.Element(
        "edges",
        {
            "version": "1.20",
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:noNamespaceSchemaLocation": "http://sumo.dlr.de/xsd/edges_file.xsd",
        },
    )

    for edge in net.getEdges():
        edge_attribs = {"id": edge.getID()}

        # Calculate edge lenght and number of lanes
        def calculate_edge_length(shape):
            length = 0.0
            # shape = {(x0, y0), (x1, y1), ... , (xn, yn)} --> lista di tuple
            # iterative eucledian distance computation to get the total lenght
            for i in range(1, len(shape)):
                x1, y1 = shape[i - 1]
                x2, y2 = shape[i]
                length += math.sqrt((x1 - x2) ** 2 + (y1 - y2) ** 2)
            return length

        edge_shape = edge.getShape()
        edge_length = calculate_edge_length(edge_shape)
        num_lanes = len(edge.getLanes())

        # Internal edge management
        if edge.getID()[0] == ":":
            edge_attribs["function"] = "internal"
        else:
            edge_attribs.update(
                {
                    "from": edge.getFromNode().getID(),
                    "to": edge.getToNode().getID(),
                    "name": edge.getName(),
                    "priority": str(edge.getPriority()),
                    "numLanes": str(num_lanes),
                    "type": getattr(edge, "_type", ""),
                    "length": str(edge_length),
                    "shape": " ".join(
                        f"{transformer.transform(x, y)[0]},{transformer.transform(x, y)[1]}"
                        for x, y in edge.getShape()
                    ),
                }
            )
        # Creation of edge subelement
        edge_element = ET.SubElement(edges_root, "edge", edge_attribs)

        # Definition of lane element
        for lane in edge.getLanes():
            # Check for disallowed categories
            allowed_vclasses = lane.getPermissions()
            if allowed_vclasses is None:
                allowed_vclasses = SUMO_VEHICLE_CLASSES

            disallowed = SUMO_VEHICLE_CLASSES - set(allowed_vclasses)
            disallowed_str = " ".join(sorted(disallowed))

            lane_shape = " ".join(
                f"{transformer.transform(x, y)[0]},{transformer.transform(x, y)[1]}"
                for x, y in lane.getShape()
            )

            # Definition of edge-element subelement
            ET.SubElement(
                edge_element,
                "lane",
                {
                    "id": lane.getID(),
                    "index": str(lane.getIndex()),
                    "disallow": disallowed_str,
                    "speed": str(lane.getSpeed()),
                    "length": str(lane.getLength()),
                    "shape": lane_shape,
                },
            )

        """
        FROM LANE.PY (-> need a for cycle on lanes, otherwise the method can't be used)

        def get_allowed(allow, disallow):
            # Normalize the given string attributes as a set of all allowed vClasses.
            if allow is None and disallow is None:
                return SUMO_VEHICLE_CLASSES
            elif disallow is None:
                return set(allow.split())
            elif disallow == "all":
                return set()
            else:
                return SUMO_VEHICLE_CLASSES.difference(disallow.split())

        """

    xml[map_name + ".edg.xml"] = edges_root

    # tll.xml
    tll_root = ET.Element(
        "tlLogics",
        {
            "version": "1.20",
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:noNamespaceSchemaLocation": "http://sumo.dlr.de/xsd/tllogic_file.xsd",
        },
    )

    for tls in net.getTrafficLights():
        # see sumolib -> net -> __init__.py (classes tls, tlsProgram, phase)
        """from __init__():

        class TLS:
            def __init__(self, id):
                self._id = id
                self._connections = []
                self._maxConnectionNo = -1
                self._programs = {}

            def getPrograms(self):
                return self._programs

        class TLSProgram:
            def __init__(self, id, offset, type):
                self._id = id
                self._type = type
                self._offset = offset
                self._phases = []
                self._params = {}
                self._conditions = {}

        class Phase:
            def __init__(self, duration, state,
            minDur=None, maxDur=None, next=tuple(),
            name="", earlyTarget=""):
                self.duration = duration
                self.state = state

        """

        # Check tls programs
        programs = tls.getPrograms()
        if not programs:  # blanck list -> initialize at least one program
            ET.SubElement(tll_root, "tlLogic", {"id": tls.getID()})
            continue

        for program in programs.values():
            tlElement = ET.SubElement(
                tll_root,
                "tlLogic",
                {
                    "id": tls.getID(),
                    "programID": program._id,
                    "type": program.getType(),
                    "offset": str(program.getOffset()),
                },
            )

            # Check tls phases
            # Every program has its associated tls phases
            for phase in program.getPhases():
                ET.SubElement(
                    tlElement,
                    "phase",
                    {  # the phase is a subelement of the tl logic
                        "duration": str(phase.duration),
                        "state": phase.state,
                    },
                )

    # The file should also contain all the involved connections
    for edge in net.getEdges():
        for lane in edge.getLanes():
            for conn in lane.getOutgoing():
                if conn.getTLSID():  # keep just the connections having TLSs
                    ET.SubElement(
                        tll_root,
                        "connection",
                        {
                            "from": conn.getFromLane().getID(),
                            "to": conn.getToLane().getID(),
                            "fromLane": str(conn.getFromLane().getIndex()),
                            "toLane": str(conn.getToLane().getIndex()),
                            "tl": conn.getTLSID(),
                            "linkIndex": str(conn.getTLLinkIndex()),
                        },
                    )

    xml[map_name + ".tll.xml"] = tll_root

    # round.xml
    roundabouts_root = ET.Element(
        "roundabouts",
        {
            "version": "1.20",
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:noNamespaceSchemaLocation": "http://sumo.dlr.de/xsd/roundabouts_file.xsd",
        },
    )

    for ra in net.getRoundabouts():
        # List of edges
        list_edg = []
        for edge in ra.getEdges():
            list_edg.append(edge)

        # List of nodes
        list_nod = []
        for node in ra.getNodes():
            list_nod.append(node)

        # Sublement creation
        ET.SubElement(
            roundabouts_root,
            "roundabout",
            {"nodes": " ".join(list_nod), "edges": " ".join(list_edg)},
        )

    xml[map_name + ".round.xml"] = roundabouts_root

    """
    # jun.xml
    junctions_root = ET.Element('junctions', {
        "version": "1.20",
        "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
        "xsi:noNamespaceSchemaLocation": "http://sumo.dlr.de/xsd/junctions_file.xsd"
    })

    requests_dict = {}
    if net_xml_path:
        try:
            original_tree = ET.parse(net_xml_path)
            original_root = original_tree.getroot()
            
            for orig_junc in original_root.findall('junction'):
                jid = orig_junc.get('id')
                requests_dict[jid] = list(orig_junc.findall('request'))
        except Exception as e:
            print(f"Warning: Could not parse requests from {net_xml_path}: {e}")

    for j in net.getNodes():
        jid = j.getID()
        x, y = j.getCoord()
        lon, lat = transformer.transform(x, y)
        
        # Incoming lanes
        incLanes = []
        for edge in j.getIncoming():
            incLanes.extend([lane.getID() for lane in edge.getLanes()])
        incLanes_str = " ".join(incLanes)
        
        # Internal lanes
        intLanes_str = " ".join(j.getInternal())
        
        # Shape coordinates (trasforma in lon/lat)
        shape_coords = []
        for sx, sy in j.getShape():
            slon, slat = transformer.transform(sx, sy)
            shape_coords.append(f"{slon},{slat}")
        shape_str = " ".join(shape_coords)
        
        junction_elem = ET.SubElement(junctions_root, "junction", {
            "id": jid,
            "type": j.getType(),
            "x": str(lon),
            "y": str(lat),
            "incLanes": incLanes_str,
            "intLanes": intLanes_str,
            "shape": shape_str
        })
        
        if jid in requests_dict:
            for request in requests_dict[jid]:
                ET.SubElement(junction_elem, "request", request.attrib)

    xml[map_name + '.jun.xml'] = junctions_root
    """

    return xml


# Function to export all saved xml, trips.xml and rou.xml files
def export_output(net_file, out_dir):
    # Read net with sumolib
    net = sumolib.net.readNet(
        net_file, withPrograms=True, withConnections=True, withInternal=True
    )
    """
    SUMO DOCUMENTATION - sumolib:
    The following named arguments may be given to the readNet function (i.e. readNet('myNet.net.xml', withInternal=True)):

        - withPrograms (bool): import all traffic light programs (default False)
        - withLatestPrograms (bool) : import only the last program for each traffic light. 
        This is the program that would be active in sumo by default. (default False)
        - withConnections (bool) : import all connections (default True)
        - withFoes (bool) : import right-of-way information (default True)
        - withInternal (bool) : import internal edges and lanes (default False)
        - withPedestrianConnections (bool) : import connections between sidewalks, crossings (default False)
    """

    xml_files = static_xml_files(net)
    format_xml(xml_files, out_dir)
    random_routes(net_file, out_dir)

    print(f"\nAll files exported to {out_dir}")


# Main
def main():
    print("Running parse.py")
    net_file = cfg.NET_FILE
    out_dir = cfg.OUTPUT_DIR_PARSING

    export_output(net_file, out_dir)


if __name__ == "__main__":
    main()
