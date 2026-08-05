import traci
import os
import sys

"""
REFERENCE: https://sumo.dlr.de/docs/TraCI/index.html -> https://sumo.dlr.de/docs/TraCI/Change_Traffic_Lights_State.html
Protocol for TraCI usage: https://sumo.dlr.de/docs/TraCI/Protocol.html

"""
if "SUMO_HOME" in os.environ:
    tools = os.path.join(os.environ["SUMO_HOME"], "tools")
    sys.path.append(tools)
else:
    sys.exit("Declare environment variable SUMO_HOME")

CONTROLLED_TLS = {
    "joinedS_5726802070_#5more",
    #"joinedS_1491322243_246625689_2602093460_2602093464_#17more",
    "joinedS_7044704202_#8more",
    "joinedS_12090799868_12136674195_12136674196_2602200967_#25more",
    # "joinedS_10176227249_10176227251_442883041_442883057_#17more",
    # "joinedS_12101709375_#14more",
    "joinedS_442883032_442883033_442883058_5726788762_#10more",
    "joinedS_1274135270_480164728_5743628119_5743628120_#3more",
    "joinedS_5743605972_#11more",
    "joinedS_429516080_5743628105_5743628106_5743628107_#10more",
    #"joinedS_28807263_441653642_441653644_5758251607_#22more",
}
SUMO_CONFIG = [
    "sumo-gui",
    "-c",
    "/home/marta/tesi-5t/DTBaldissera/vm-file/project/scripts/output/config/francia_peschiera_MAROUTER_with_TLS_AM.sumocfg",
]

traci.start(SUMO_CONFIG)

all_tls = traci.trafficlight.getIDList()
for tls_id in all_tls:
    if tls_id not in CONTROLLED_TLS:
        traci.trafficlight.setProgram(tls_id, "off")


step = 0
while traci.simulation.getMinExpectedNumber() > 0:

    for tls_id in CONTROLLED_TLS:
        traci.trafficlight.getPhase(tls_id)
        traci.trafficlight.getNextSwitch(tls_id)

    step += 1

    traci.simulationStep()  # move simulation one step forward

traci.close()
