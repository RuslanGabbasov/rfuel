#!/usr/bin/env python3
"""
Scan Russia for fuel availability via /api/nearby on an overlapping grid.
Deduplicate stations by osm_id across all grid cells.
Produces an HTML heatmap with Leaflet + CartoDB tiles.

Strategy: one API call per grid cell (no phase 2 needed — status comes with station).
"""

import argparse
import asyncio
import json
import math
import time
from pathlib import Path

import aiohttp

API_NEARBY = "https://gdebenz.ru/api/nearby"
RUSSIA_BBOX = {"lat_min": 43.0, "lat_max": 72.0, "lon_min": 28.0, "lon_max": 180.0}


def generate_grid(step_km: float) -> list[tuple[float, float]]:
    """Generate grid points covering Russia."""
    lat_step = step_km / 111.0
    points = []
    lat = RUSSIA_BBOX["lat_min"]
    while lat <= RUSSIA_BBOX["lat_max"]:
        lon_step = step_km / (111.0 * max(math.cos(math.radians(lat)), 0.1))
        lon = RUSSIA_BBOX["lon_min"]
        while lon <= RUSSIA_BBOX["lon_max"]:
            points.append((round(lat, 3), round(lon, 3)))
            lon += lon_step
        lat += lat_step
    return points


async def fetch_nearby(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    lat: float,
    lon: float,
    radius_km: int,
) -> list[dict]:
    """Fetch stations from /api/nearby, return list of stations."""
    async with sem:
        for attempt in range(3):
            try:
                async with session.get(
                    API_NEARBY,
                    params={"lat": lat, "lon": lon, "radius_km": radius_km},
                    timeout=aiohttp.ClientTimeout(total=60, sock_read=30),
                ) as resp:
                    if resp.status == 200:
                        raw = await resp.read()
                        try:
                            data = json.loads(raw)
                            return data.get("stations", [])
                        except json.JSONDecodeError:
                            if attempt < 2:
                                await asyncio.sleep(2)
                                continue
                            return []
                    if resp.status == 429:
                        await asyncio.sleep(5)
                        continue
                    return []
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(1)
                    continue
                return []
    return []


def build_html(stations: list[dict], output_path: str) -> None:
    """Build HTML heatmap with Leaflet + CartoDB tiles."""
    total = len(stations)
    yes = sum(1 for s in stations if s.get("status") == "yes")
    queue = sum(1 for s in stations if s.get("status") == "queue")
    no = sum(1 for s in stations if s.get("status") == "no")
    low = sum(1 for s in stations if s.get("status") == "low")

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
  <div style="margin-top:6px;font-size:11px;color:#888;">Кликните на маркер для деталей</div>
</div>

<div id="map"></div>

<div class="footer">
  Данные: <a href="https://gdebenz.ru" target="_blank">gdebenz.ru</a>,
  {time.strftime("%Y-%m-%d %H:%M")},
  подложка: &copy; <a href="https://carto.com">CARTO</a>
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

// Heatmap from stations with fuel
var heatData = stations
  .filter(function(s) {{ return s.status === 'yes' || s.status === 'queue'; }})
  .map(function(s) {{
    var intensity = s.status === 'yes' ? 1.0 : 0.6;
    return [s.lat, s.lon, intensity];
  }});

if (heatData.length > 0) {{
  L.heatLayer(heatData, {{
    radius: 20, blur: 12, maxZoom: 12, max: 1.0,
    gradient: {{ 0.0: '#ff1744', 0.5: '#ffd600', 0.8: '#00c853', 1.0: '#00c853' }}
  }}).addTo(map);
}}

// Circle markers per station
stations.forEach(function(s) {{
  var color = statusColors[s.status] || '#999';
  L.circleMarker([s.lat, s.lon], {{
    radius: 5, fillColor: color, color: '#333',
    weight: 0.5, opacity: 0.8, fillOpacity: 0.7
  }}).bindPopup(
    '<b>' + (s.brand || 'АЗС') + '</b><br>' +
    'Статус: ' + {{ 'yes':'✅ Есть','queue':'⚠️ Очередь','no':'❌ Нет','unknown':'❓' }}[s.status] + '<br>' +
    (s.detail ? 'Топливо: ' + s.detail + '<br>' : '') +
    'Подтверждений: ' + (s.confirmations || 0)
  ).addTo(map);
}});

if (stations.length > 0) {{
  map.fitBounds(stations.map(function(s) {{ return [s.lat, s.lon]; }}), {{ padding: [50, 50] }});
}}
</script>
</body>
</html>"""

    Path(output_path).write_text(html, encoding="utf-8")
    print(f"Saved HTML to {output_path} ({len(stations)} stations)")


async def main():
    parser = argparse.ArgumentParser(description="Scan Russia for fuel availability")
    parser.add_argument(
        "--step",
        type=float,
        default=80,
        help="Grid step in km (default: 80 — ~1 request per city)",
    )
    parser.add_argument(
        "--radius",
        type=int,
        default=60,
        help="API radius per point in km (default: 60)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Max concurrent requests (default: 10)",
    )
    parser.add_argument("--output", default="fuel_map.html", help="Output HTML file")
    args = parser.parse_args()

    grid = generate_grid(args.step)
    print(f"Grid: {len(grid)} points (step={args.step}km, radius={args.radius}km)")

    sem = asyncio.Semaphore(args.concurrency)
    all_stations: dict[str, dict] = {}
    total = len(grid)
    start = time.time()

    async with aiohttp.ClientSession() as session:
        batch_size = 10
        for i in range(0, total, batch_size):
            batch = grid[i : i + batch_size]
            tasks = [
                fetch_nearby(session, sem, lat, lon, args.radius) for lat, lon in batch
            ]
            results = await asyncio.gather(*tasks)

            for stations in results:
                for s in stations:
                    oid = s.get("osm_id")
                    if oid:
                        all_stations[oid] = s  # dedup

            done = min(i + batch_size, total)
            elapsed = time.time() - start
            eta = (elapsed / max(done, 1)) * (total - done) if done > 0 else 0
            print(
                f"  [{done}/{total}] {len(all_stations)} unique stations, "
                f"elapsed={elapsed:.0f}s eta={eta:.0f}s"
            )

            if i + batch_size < total:
                await asyncio.sleep(0.3)

    elapsed = time.time() - start
    stations = list(all_stations.values())
    yes = sum(1 for s in stations if s.get("status") == "yes")
    queue = sum(1 for s in stations if s.get("status") == "queue")
    no = sum(1 for s in stations if s.get("status") == "no")
    print(
        f"\nDone in {elapsed:.0f}s: {len(stations)} stations (yes={yes} queue={queue} no={no})"
    )

    # Save raw
    json_path = args.output.replace(".html", ".json")
    Path(json_path).write_text(json.dumps(stations, indent=1), encoding="utf-8")
    print(f"Raw data: {json_path}")

    build_html(stations, args.output)


if __name__ == "__main__":
    asyncio.run(main())
