#!/usr/bin/env python3
"""
Scan all of Russia for fuel availability using gdebenz.ru API.
Two phases:
1. Collect unique station OSM IDs via /api/stations?lat1=...&lon1=...
2. Fetch status for each unique station via /api/nearby (single point)

Produces an HTML heatmap.
"""

import argparse
import asyncio
import json
import math
import time
from pathlib import Path

import aiohttp

API_STATIONS = "https://gdebenz.ru/api/stations"
API_NEARBY = "https://gdebenz.ru/api/nearby"

RUSSIA_BBOX = {"lat_min": 43.0, "lat_max": 72.0, "lon_min": 28.0, "lon_max": 180.0}


def generate_tiles(
    tile_size_deg: float = 1.0,
) -> list[tuple[float, float, float, float]]:
    """Generate overlapping tile boundaries covering Russia."""
    tiles = []
    overlap = tile_size_deg * 0.3  # 30% overlap to catch edge stations
    lat = RUSSIA_BBOX["lat_min"]
    while lat < RUSSIA_BBOX["lat_max"]:
        lon = RUSSIA_BBOX["lon_min"]
        while lon < RUSSIA_BBOX["lon_max"]:
            lat2 = min(lat + tile_size_deg, RUSSIA_BBOX["lat_max"])
            lon2 = min(lon + tile_size_deg, RUSSIA_BBOX["lon_max"])
            tiles.append((lat, lon, lat2, lon2))
            lon += tile_size_deg - overlap
        lat += tile_size_deg - overlap
    return tiles


async def fetch_stations_tile(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> list[dict]:
    """Fetch all stations in a bounding box from /api/stations."""
    async with sem:
        for attempt in range(3):
            try:
                async with session.get(
                    API_STATIONS,
                    params={"lat1": lat1, "lon1": lon1, "lat2": lat2, "lon2": lon2},
                    timeout=aiohttp.ClientTimeout(total=30, sock_read=15),
                ) as resp:
                    if resp.status == 200:
                        raw = await resp.read()
                        try:
                            data = json.loads(raw)
                            if isinstance(data, list):
                                return data
                            return []
                        except json.JSONDecodeError:
                            if attempt < 2:
                                await asyncio.sleep(2)
                                continue
                            return []
                    return []
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(1)
                    continue
                return []
    return []


async def fetch_station_status(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    osm_id: str,
    lat: float,
    lon: float,
) -> dict | None:
    """Fetch fuel status for a single station via /api/nearby (small radius)."""
    async with sem:
        try:
            async with session.get(
                API_NEARBY,
                params={
                    "lat": lat,
                    "lon": lon,
                    "radius_km": 0.5,
                },  # tiny radius to get just this station
                timeout=aiohttp.ClientTimeout(total=20, sock_read=10),
            ) as resp:
                if resp.status == 200:
                    raw = await resp.read()
                    try:
                        data = json.loads(raw)
                        stations = data.get("stations", [])
                        for s in stations:
                            if s.get("osm_id") == osm_id:
                                return s
                        # Station not found in response — it's gone or API didn't return it
                        return {
                            "osm_id": osm_id,
                            "status": "unknown",
                            "detail": "",
                            "confirmations": 0,
                            "confirmed": False,
                            "last_at": "",
                            "name": "",
                            "brand": "",
                            "addr": "",
                            "lat": lat,
                            "lon": lon,
                        }
                    except json.JSONDecodeError:
                        return None
                return None
        except Exception:
            return None


def build_html(stations: list[dict], output_path: str) -> None:
    """Build HTML heatmap with Leaflet + CartoDB tiles."""
    # Count statuses
    total = len(stations)
    yes = sum(1 for s in stations if s.get("status") == "yes")
    queue = sum(1 for s in stations if s.get("status") == "queue")
    no = sum(1 for s in stations if s.get("status") in ("no",))
    unknown = sum(1 for s in stations if s.get("status") == "unknown")
    availability_score = (yes + queue * 0.5) / total * 100 if total > 0 else 0

    points_json = json.dumps(
        [
            {
                "lat": s["lat"],
                "lon": s["lon"],
                "brand": s.get("brand", "") or s.get("name", ""),
                "status": s.get("status", "unknown"),
                "detail": s.get("detail", ""),
                "confirmations": s.get("confirmations", 0),
            }
            for s in stations
        ]
    )

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Топливная карта России — gdebenz.ru</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, system-ui, sans-serif; background: #f5f5f5; }}
#map {{ height: 100vh; width: 100%; }}
#stats {{
  position: fixed; top: 12px; right: 12px; z-index: 1000;
  background: rgba(255,255,255,0.95); box-shadow: 0 2px 12px rgba(0,0,0,0.12);
  padding: 14px 18px; border-radius: 10px; border: 1px solid #e0e0e0;
  font-size: 13px; color: #333; min-width: 220px;
}}
#stats h3 {{ color: #111; font-size: 15px; margin-bottom: 6px; }}
.legend-item {{ display: flex; align-items: center; gap: 6px; margin: 2px 0; font-size: 12px; }}
.legend-item span {{ width: 14px; height: 14px; border-radius: 50%; flex-shrink: 0; }}
.footer {{
  position: fixed; bottom: 12px; left: 50%; transform: translateX(-50%); z-index: 1000;
  background: rgba(255,255,255,0.9); box-shadow: 0 1px 6px rgba(0,0,0,0.1);
  padding: 6px 16px; border-radius: 6px;
  color: #666; font-size: 11px; text-align: center;
}}
.footer a {{ color: #446688; }}
</style>
</head>
<body>

<div id="stats">
  <h3>&#9981; Топливная карта России</h3>
  <div>Всего АЗС: <b>{total}</b></div>
  <div>&#9989; Есть: <b>{yes}</b> ({yes / total * 100:.0f}%)</div>
  <div>&#9888; Очередь: <b>{queue}</b> ({queue / total * 100:.0f}%)</div>
  <div>&#10060; Нет: <b>{no}</b> ({no / total * 100:.0f}%)</div>
  <div style="margin-top:6px;font-size:11px;color:#888;">Неизвестно: {unknown}</div>
  <div style="margin-top:6px;font-size:11px;color:#888;">Кликните на маркер для деталей</div>
</div>

<div id="map"></div>

<div class="footer">
  Данные: <a href="https://gdebenz.ru" target="_blank">gdebenz.ru</a>,
  сканирование: {time.strftime("%Y-%m-%d %H:%M")},
  подложка: &copy; <a href="https://carto.com">CARTO</a> / <a href="https://www.openstreetmap.org/copyright">OSM</a>
</div>

<script>
const stations = {points_json};
const statusColors = {{
  'yes': '#00c853',
  'queue': '#ffd600',
  'no': '#ff1744',
  'low': '#ff9100',
  'unknown': '#999'
}};

var map = L.map('map', {{ center: [62, 100], zoom: 3 }});

L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
  attribution: '&copy; CARTO',
  subdomains: 'abcd',
  maxZoom: 19,
}}).addTo(map);

// Heatmap from stations with fuel (yes + queue)
var heatData = stations
  .filter(function(s) {{ return s.status === 'yes' || s.status === 'queue'; }})
  .map(function(s) {{
    var intensity = s.status === 'yes' ? 0.9 : 0.5;
    return [s.lat, s.lon, intensity];
  }});

if (heatData.length > 0) {{
  L.heatLayer(heatData, {{
    radius: 25,
    blur: 15,
    maxZoom: 12,
    max: 1.0,
    gradient: {{ 0.0: '#ff1744', 0.5: '#ffd600', 0.8: '#00c853', 1.0: '#00c853' }}
  }}).addTo(map);
}}

// Circle markers
stations.forEach(function(s) {{
  var color = statusColors[s.status] || '#999';
  var r = s.detail && s.detail.length > 0 ? 7 : 5;
  L.circleMarker([s.lat, s.lon], {{
    radius: r,
    fillColor: color,
    color: '#333',
    weight: 0.5,
    opacity: 0.8,
    fillOpacity: 0.7
  }}).bindPopup(
    '<b>' + (s.brand || 'АЗС') + '</b><br>' +
    'Статус: ' + {{
      'yes': '&#9989; Есть',
      'queue': '&#9888; Очередь',
      'no': '&#10060; Нет',
      'unknown': '&#10067; Неизвестно'
    }}[s.status] + '<br>' +
    (s.detail ? 'Топливо: ' + s.detail + '<br>' : '') +
    'Подтверждений: ' + (s.confirmations || 0)
  ).addTo(map);
}});

if (stations.length > 0) {{
  var bounds = stations.map(function(s) {{ return [s.lat, s.lon]; }});
  map.fitBounds(bounds, {{ padding: [50, 50] }});
}}
</script>
</body>
</html>"""

    Path(output_path).write_text(html, encoding="utf-8")
    print(f"Saved HTML to {output_path} ({len(stations)} stations)")


async def main():
    parser = argparse.ArgumentParser(description="Scan Russia for fuel availability")
    parser.add_argument(
        "--tile-deg",
        type=float,
        default=1.0,
        help="Tile size in degrees (default: 1.0, ~100km)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=15,
        help="Max concurrent requests (default: 15)",
    )
    parser.add_argument(
        "--skip-status",
        action="store_true",
        help="Skip phase 2 (status fetch), only collect stations",
    )
    parser.add_argument("--output", default="fuel_map.html", help="Output HTML file")
    parser.add_argument(
        "--resume", default="", help="Resume from saved JSON file (phase 2)"
    )
    args = parser.parse_args()

    if args.resume:
        # Resume: load stations from JSON, do phase 2 only
        print(f"Resuming from {args.resume}...")
        with open(args.resume) as f:
            all_stations = json.load(f)
        print(f"Loaded {len(all_stations)} stations from {args.resume}")
        start_phase = 2
    else:
        # Phase 1: collect stations
        tiles = generate_tiles(args.tile_deg)
        print(f"Phase 1: scanning {len(tiles)} tiles...")

        sem = asyncio.Semaphore(args.concurrency)
        all_stations: dict[str, dict] = {}

        async with aiohttp.ClientSession() as session:
            batch_size = 20
            for i in range(0, len(tiles), batch_size):
                batch_tiles = tiles[i : i + batch_size]
                tasks = [
                    fetch_stations_tile(session, sem, lat1, lon1, lat2, lon2)
                    for (lat1, lon1, lat2, lon2) in batch_tiles
                ]
                results = await asyncio.gather(*tasks)
                for tile_stations in results:
                    for s in tile_stations:
                        oid = s.get("osm_id")
                        if oid:
                            all_stations[oid] = s  # dedup by osm_id

                done = min(i + batch_size, len(tiles))
                print(
                    f"  Phase 1: [{done}/{len(tiles)}] tiles, {len(all_stations)} unique stations"
                )

                if i + batch_size < len(tiles):
                    await asyncio.sleep(1)

        stations_list = list(all_stations.values())
        print(f"Phase 1 done: {len(stations_list)} unique stations")

        # Save intermediate
        json_path = args.output.replace(".html", ".json")
        Path(json_path).write_text(
            json.dumps(stations_list, indent=1), encoding="utf-8"
        )
        print(f"Stations saved: {json_path}")

        if args.skip_status:
            # Build simple marker map without status
            build_html(stations_list, args.output)
            return

        start_phase = 2

    # Phase 2: fetch status for each station
    if start_phase == 2:
        stations_list = list(all_stations.values()) if not args.resume else all_stations

    print(f"Phase 2: fetching status for {len(stations_list)} stations...")
    sem = asyncio.Semaphore(args.concurrency)
    enriched = []
    start = time.time()

    async with aiohttp.ClientSession() as session:
        batch_size = 15
        for i in range(0, len(stations_list), batch_size):
            batch = stations_list[i : i + batch_size]
            tasks = [
                fetch_station_status(
                    session, sem, s.get("osm_id", ""), s.get("lat", 0), s.get("lon", 0)
                )
                for s in batch
            ]
            results = await asyncio.gather(*tasks)

            for orig, status in zip(batch, results):
                if status:
                    enriched.append(status)
                else:
                    enriched.append(orig)

            done = min(i + batch_size, len(stations_list))
            elapsed = time.time() - start
            eta = (
                (elapsed / max(done, 1)) * (len(stations_list) - done)
                if done > 0
                else 0
            )
            ok = sum(1 for s in enriched if s.get("status") != "unknown")
            print(
                f"  Phase 2: [{done}/{len(stations_list)}] ok={ok} elapsed={elapsed:.0f}s eta={eta:.0f}s"
            )

            if i + batch_size < len(stations_list):
                await asyncio.sleep(0.5)

    elapsed = time.time() - start
    yes = sum(1 for s in enriched if s.get("status") == "yes")
    queue = sum(1 for s in enriched if s.get("status") == "queue")
    no = sum(1 for s in enriched if s.get("status") == "no")
    print(
        f"\nDone in {elapsed:.0f}s:"
        f" {len(enriched)} stations,"
        f" yes={yes}, queue={queue}, no={no}"
    )

    build_html(enriched, args.output)


if __name__ == "__main__":
    asyncio.run(main())
