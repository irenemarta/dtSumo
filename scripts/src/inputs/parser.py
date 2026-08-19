import os
import sys
from typing import Dict
import math

# Set environment variable and import sumolib -> more info here: https://sumo.dlr.de/docs/Tools/Sumolib.html
if "SUMO_HOME" in os.environ:
    sys.path.append(os.path.join(os.environ["SUMO_HOME"], "tools"))

import sumolib
from sumolib.net import Net
from sumolib.net.lane import SUMO_VEHICLE_CLASSES # to define disallowed vehicles over edges
import xml.etree.ElementTree as ET
from xml.etree.ElementTree import indent  # file layout
from pyproj import Transformer
from pathlib import Path

from scripts.src.helpers import format_xml
import scripts.src.inputs.config as cfg
from scripts.src.operations.cmd import random_routes


class XMLBuilder:
    TRANSFORMER = Transformer.from_crs("EPSG:32632", "EPSG:4326", always_xy=True)
    _HEADER_SCHEMA = {
            "version": "1.20",
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
        }
    
    FORCE_ALLOW_BY_TYPE = {
        "highway.service": {"passenger"},
    }
    
    def _root(self, element_name: str) -> dict:
        return {
            **self._HEADER_SCHEMA,
            "xsi:noNamespaceSchemaLocation": f"http://sumo.dlr.de/xsd/{element_name}"
            }

    def _transform_coord(self, x: float, y:float):
        return self.TRANSFORMER.transform(x, y)
    
    def _shape_to_str(self, shape) -> str:
        return " ".join(
            f"{self._transform_coord(x, y)[0]},{self._transform_coord(x, y)[1]}"
            for x, y in shape
        )
        
    @staticmethod
    def calculate_edge_length(shape):
        length = 0.0
        # shape = {(x0, y0), (x1, y1), ... , (xn, yn)} --> lista di tuple
        # iterative eucledian distance computation to get the total lenght
        for i in range(1, len(shape)):
            x1, y1 = shape[i - 1]
            x2, y2 = shape[i]
            length += math.sqrt((x1 - x2) ** 2 + (y1 - y2) ** 2)
        return length
    
    @staticmethod
    def disallowed(lane, force_allow: set = None) -> str:
        allowed_vclasses = lane.getPermissions() or SUMO_VEHICLE_CLASSES
        if force_allow:
            allowed_vclasses |= force_allow
        return " ".join(sorted(SUMO_VEHICLE_CLASSES - set(allowed_vclasses)))
    
    def build(self, net) -> ET.Element:
        raise NotImplementedError


class NodeBuilder(XMLBuilder):
    def build(self, net) -> ET.Element:
        root = ET.Element("nodes", self._root("nodes_file.xsd"))
        for node in net.getNodes():
            x, y = node.getCoord()  # UTM coordinates
            lon, lat = self._transform_coord(x, y)  # conversion in lat/lon coordinates
            # To get matches with the Google API csv database
            # lon = round(lon, 6)
            # lat = round(lat, 6)
            
            ET.SubElement(
                root,
                "node",
                {
                    "id": node.getID(),
                    "x": str(lon),
                    "y": str(lat),
                    "type": node.getType() if node.getType() else "",
                },
            )
        
        return root


class ConnectionBuilder(XMLBuilder):
    def build(self, net) -> ET.Element:
        root = ET.Element("connections", self._root("connections_file.xsd"))
        
        for edge in net.getEdges():
            for lane in edge.getLanes():  # see lane.py
                for conn in lane.getOutgoing():
                    ET.SubElement(
                        root,
                        "connection",
                        {
                            "from": conn.getFromLane().getEdge().getID(),
                            "to": conn.getToLane().getEdge().getID(),
                            "fromLane": str(conn.getFromLane().getIndex()),
                            "toLane": str(conn.getToLane().getIndex()),
                        },
                    )
        return root


class TypeBuilder(XMLBuilder):
    def build(self, net) -> ET.Element:
        root = ET.Element("types", self._root("types_file.xsd"))
        
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
            disallowed = set()
            oneway= "1" # assume one direction
            
            for lane in edge.getLanes():
                disallowed = self.disallowed(lane)
            disallowed_str = " ".join(sorted(disallowed))
            
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
                    
        ET.SubElement(
            root,
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
        
        return root


class EdgeBuilder(XMLBuilder):
    def build(self, net) -> ET.Element:
        root = ET.Element("edges", self._root("edges_file.xsd"))

        for edge in net.getEdges():
            edge_attribs = {"id": edge.getID()}
            edge_type = getattr(edge, "_type", "")
            
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
                        "numLanes": str(len(edge.getLanes())),
                        "type": edge_type,
                        "length": str(self.calculate_edge_length(edge.getShape())),
                        "shape": self._shape_to_str(edge.getShape())
                    }
                )
            # Creation of edge subelement
            edge_element = ET.SubElement(root, "edge", edge_attribs)
            
            force_allow = self.FORCE_ALLOW_BY_TYPE.get(edge_type)
            
            
            # Definition of lane element
            for lane in edge.getLanes():
                # Definition of edge-element subelement
                ET.SubElement(
                    edge_element,
                    "lane",
                    {
                        "id": lane.getID(),
                        "index": str(lane.getIndex()),
                        "disallow": self.disallowed(lane, force_allow),
                        "speed": str(lane.getSpeed()),
                        "length": str(lane.getLength()),
                        "width": str(lane.getWidth()),
                        "shape": self._shape_to_str(lane.getShape()),
                    },
                )
    
        return root


class TLLBuilder(XMLBuilder):
    def build(self, net) -> ET.Element:
        root = ET.Element("tlLogics", self._root("tllogic_file.xsd"))
        
        for tls in net.getTrafficLights():
            # Check tls programs
            programs = tls.getPrograms()
            if not programs:  # blanck list -> initialize at least one program
                ET.SubElement(root, "tlLogic", {"id": tls.getID()})
                continue

            for program in programs.values():
                tlElement = ET.SubElement(
                    root,
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
                            root,
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
                        
        return root


class RoundaboutBuilder(XMLBuilder):
    def build(self, net) -> ET.Element:
        root = ET.Element("roundabouts", self._root("roundabouts_file.xsd"))
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
                root,
                "roundabout",
                {"nodes": " ".join(list_nod), "edges": " ".join(list_edg)},
            )
            
        return root


class JunctionBuilder(XMLBuilder):
    """
    NB: <request> not accessible via sumolib!!
    """

    def __init__(self, net_xml_path: str):
        self.net_xml_path = net_xml_path

    def _load_requests(self) -> dict:
        #print(f"Net path: {self.net_xml_path}")
        requests = {}
        if not self.net_xml_path:
            print("WARN: net not found or empty")
            return requests
        try:
            root = ET.parse(self.net_xml_path).getroot()
            junctions = root.findall("junction")
            for junc in junctions:
                jid = junc.get("id")
                reqs = junc.findall("request")
                requests[jid] = [req.attrib for req in reqs]
        except Exception as e:
            print(f"Warning: could not parse requests from {self.net_xml_path}: {e}")
        return requests

    def build(self, net) -> ET.Element:
        root = ET.Element("junctions", self._root("junctions_file.xsd"))
        requests_dict = self._load_requests()
        # print(f"REQUESTS:\n{requests_dict}")

        for j in net.getNodes():
            jid = j.getID()
            x, y = j.getCoord()
            lon, lat = self._transform_coord(x, y)

            inc_lanes = [
                lane.getID()
                for edge in j.getIncoming()
                for lane in edge.getLanes()
            ]

            junction_el = ET.SubElement(root, "junction", {
                "id": jid,
                "type": j.getType(),
                "x": str(lon),
                "y": str(lat),
                "incLanes": " ".join(inc_lanes),
                "intLanes": " ".join(j.getInternal()),
                "shape": self._shape_to_str(j.getShape()),
            })
            
            jreq = requests_dict.get(jid, [])
            for req_attribs in jreq:
                ET.SubElement(junction_el, "request", {
                    "index": req_attribs.get("index", ""),
                        "response": req_attribs.get("response", ""),
                        "foes": req_attribs.get("foes", ""),
                        "cont": req_attribs.get("cont", "0")
                })

        return root


class NetParser:
    _BUILDERS = {
        ".nod.xml": NodeBuilder,
        ".con.xml": ConnectionBuilder,
        ".typ.xml": TypeBuilder,
        ".edg.xml": EdgeBuilder,
        ".tll.xml": TLLBuilder,
        ".round.xml": RoundaboutBuilder,
        ".jun.xml": JunctionBuilder,
    }
    
    def __init__(self, net: Path, out_dir: Path, map_name="francia_peschiera"):
        self.net = net
        self.output = out_dir
        self.map_name = map_name
        
    def _load_net(self):
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
        return sumolib.net.readNet(
            self.net, withPrograms=True, withConnections=True, withInternal=True
        )

    def static_xml(self, net) -> Dict[str, ET.Element]:
        print("\nCREATING STATIC XML FILES")
        
        files = {}
        for suffix, builder_class in self._BUILDERS.items():
            if builder_class == JunctionBuilder:
                files[self.map_name + suffix] = builder_class(str(self.net)).build(net)
            else:
                files[self.map_name + suffix] = builder_class().build(net)
        return files
    
    def export_output(self):
        net = self._load_net()
        xml_files = self.static_xml(net)
        format_xml(xml_files, self.output)
        random_routes(self.net, self.output)
        
        print(f"\nAll files exported to {self.output}")


def main():
    print("Running parse.py")
    net_file = cfg.NET_FILE
    out_dir = cfg.OUTPUT_DIR_PARSING
    parser = NetParser(net_file, out_dir)
    
    parser.export_output()
    
if __name__ == "__main__":
    main()