"""
@file       coordinates.py
@author     Irene Marta
@date       2026


This program is aimed at calculating the bounding box to give JOSM as an input.
The script analyses three different cases:
1. The input is a name (ex. Piazza Baldissera)
2. The input is a set of coordinates to use as offset (lat/lon)
3. The input is a set of roads, for which the intersection as to be taken as bbox central point.
"""


# More on Overpass API in Python: https://pybit.es/articles/openstreetmaps-overpass-api-and-python/ 
# and https://wiki.openstreetmap.org/wiki/Overpass_API/Overpass_API_by_Example#Building_Blocks

import requests
import webbrowser

from geopy.distance import distance
from geopy.geocoders import Nominatim
from geopy.point import Point

# HACK: JOSM interface is supposed to be opened for the output to be delivered


def josm_data(name=None, coordinates=None, intersection=None, radius=150):
    lat, lon = None, None
    geolocator = Nominatim(user_agent='sumo_map_extractor')

    if name:
        print(f'Search by name: {name}')
        location = geolocator.geocode(name)
        if location:
            lat, lon = location.latitude, location.longitude
    
    elif coordinates: # migliorabile inserendo conversione di coordinate in gradi
        print(f'Search by coordinates: {coordinates}')
        lat, lon = coordinates[0], coordinates[1]

    elif intersection and len(intersection) == 3:
        road1, road2, city = intersection
        print(f'Search by intersection: {road1} and {road2} in {city}')

        # Query for Overpass API
        query = f"""
        [out:json][timeout:25];
        area["name"="{city}"]->.c;
        (
        way(area.c)["highway"]["name"~"{road1}",i];
        way(area.c)["highway"]["name"~"{road2}",i];
        );
        out body;
        >;
        out skel qt;
        """

        try:
            overpass_url = "http://overpass-api.de/api/interpreter"
            response = requests.get(overpass_url, params={'data': query})
            data = response.json()
            if data['elements']:
                lat, lon = data['elements'][0]['lat'], data['elements'][0]['lon']
            else:
                print("Intersection not found")
                return
        except Exception as e:
            print(f"Overpass query error: {e}")
            return

    # Compute bbox (stands for each and every case):
    if lat and lon: 
        center = Point(lat, lon)
        # Calculate cardinal points at radius-distance from the center - max distance from the center in the bbox 
        north = distance(meters=radius).destination(center, bearing=0).latitude
        south = distance(meters=radius).destination(center, bearing=180).latitude
        east  = distance(meters=radius).destination(center, bearing=90).longitude
        west  = distance(meters=radius).destination(center, bearing=270).longitude

        # Bounding-box coordinates
        print(f'Center of the bounding box: {center}')
        print(f"Bounding box: {south}, {west}, {north}, {east}\n")

        # Send coordinates to JOSM (left = west, bottom = south, right = east, top = north)
        josm_url = f"http://127.0.0.1:8111/load_and_zoom?left={west}&bottom={south}&right={east}&top={north}"

        try:
            webbrowser.open(josm_url)
            print("Sent the area to JOSM.")
        except Exception as e:
            print(f"Error while opening JOSM: {e}")
        
        return south, west, north, east
    
    else:
        print("Could not find the specified location")
        return None
    

def main():
    josm_data(name='Via Giorgio Catti, Parella, Circoscrizione 4, Torino, Piemonte, 10100, Italia')

if __name__ == '__main__':
    main()