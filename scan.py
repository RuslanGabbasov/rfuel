#!/usr/bin/env python3
"""
Scan all of Russia for fuel availability using gdebenz.ru API.
Produces an HTML heatmap of fuel availability across the country.

Usage:
    python scan.py                     # Default: 50km grid, 10km radius
    python scan.py --step 100          # Coarser grid (faster)
    python scan.py --step 30           # Finer grid (slower, more detail)
    python scan.py --radius 15         # Larger API radius per point
"""

import argparse
import asyncio
import json
import math
import time
from pathlib import Path

import aiohttp

API_BASE = "https://gdebenz.ru/api/nearby"

# Russia bounding box (approximate — mainland)
RUSSIA_BBOX = {
    "lat_min": 43.0,  # south (Caucasus)
    "lat_max": 72.0,  # north (Arctic)
    "lon_min": 28.0,  # west (Kaliningrad border)
    "lon_max": 180.0,  # east (Kamchatka)
}


def generate_grid(step_km: float) -> list[tuple[float, float]]:
    """Generate a lat/lon grid covering Russia."""
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


async def fetch_one(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    lat: float,
    lon: float,
    radius_km: int,
) -> dict | None:
    """Fetch fuel data for one point with retry."""
    async with sem:
        for attempt in range(3):
            try:
                async with session.get(
                    API_BASE,
                    params={"lat": lat, "lon": lon, "radius_km": radius_km},
                    timeout=aiohttp.ClientTimeout(total=60, sock_read=30),
                ) as resp:
                    if resp.status == 200:
                        raw = await resp.read()
                        try:
                            data = json.loads(raw)
                            return {
                                "lat": lat,
                                "lon": lon,
                                "summary": data.get("summary", {}),
                                "updated": data.get("updated", ""),
                            }
                        except json.JSONDecodeError:
                            if attempt < 2:
                                await asyncio.sleep(2)
                                continue
                            return None
                    if resp.status == 429:
                        await asyncio.sleep(5)
                        continue
                    return None
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(1)
                    continue
                return None
    return None


def summarize_point(data: dict | None) -> dict | None:
    """Extract availability metrics from one API response."""
    if not data or not data.get("summary"):
        return None

    s = data["summary"]
    total = s.get("yes", 0) + s.get("queue", 0) + s.get("no", 0) + s.get("low", 0)
    if total == 0:
        return None

    return {
        "lat": data["lat"],
        "lon": data["lon"],
        "total": total,
        "yes": s.get("yes", 0),
        "queue": s.get("queue", 0),
        "no": s.get("no", 0),
        "low": s.get("low", 0),
        "yes_pct": round(s.get("yes", 0) / total * 100, 1) if total > 0 else 0,
        "availability_score": round(
            (s.get("yes", 0) + s.get("queue", 0) * 0.5) / total * 100, 1
        )
        if total > 0
        else 0,
        "updated": data.get("updated", ""),
    }


def build_html(points: list[dict], output_path: str) -> None:
    """Build an HTML heatmap with Leaflet + CartoDB tiles (free, no key)."""
    points_json = json.dumps(points)

    avg_score = (
        sum(p["availability_score"] for p in points) / len(points) if points else 0
    )
    worst_score = min(p["availability_score"] for p in points) if points else 0

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
body {{ font-family: -apple-system, system-ui, sans-serif; background: #111; }}
#map {{ height: 100vh; width: 100%; }}
#stats {{
  position: fixed; top: 12px; right: 12px; z-index: 1000;
  background: rgba(17,17,17,0.92); backdrop-filter: blur(6px);
  padding: 14px 18px; border-radius: 10px; border: 1px solid #333;
  font-size: 13px; color: #e0e0e0; min-width: 200px;
}}
#stats h3 {{ color: #fff; font-size: 15px; margin-bottom: 6px; }}
.legend {{ margin-top: 8px; }}
.legend-item {{ display: flex; align-items: center; gap: 6px; margin: 2px 0; font-size: 12px; }}
.legend-item span {{ width: 14px; height: 14px; border-radius: 50%; flex-shrink: 0; }}
.footer {{
  position: fixed; bottom: 12px; left: 50%; transform: translateX(-50%); z-index: 1000;
  background: rgba(17,17,17,0.85); padding: 6px 16px; border-radius: 6px;
  color: #666; font-size: 11px; text-align: center;
}}
.footer a {{ color: #6688aa; }}
</style>
</head>
<body>

<div id="stats">
  <h3>&#9981; Топливная карта России</h3>
  <div>Точек: <b>{len(points)}</b></div>
  <div>Средняя доступность: <b id="avg">{avg_score:.1f}%</b></div>
  <div class="legend">
    <div class="legend-item"><span style="background:#00c853"></span> 80-100% — есть везде</div>
    <div class="legend-item"><span style="background:#ffd600"></span> 50-80% — в целом есть</div>
    <div class="legend-item"><span style="background:#ff9100"></span> 20-50% — проблемно</div>
    <div class="legend-item"><span style="background:#ff1744"></span> 0-20% — критично</div>
  </div>
  <div style="margin-top:6px;font-size:11px;color:#666;">Наведите на точку для деталей</div>
</div>

<div id="map"></div>

<div class="footer">
  Данные: <a href="https://gdebenz.ru" target="_blank">gdebenz.ru</a>,
  сканирование: {time.strftime("%Y-%m-%d %H:%M")},
  подложка: &copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> / CartoDB
</div>

<script>
const points = {points_json};

var map = L.map('map', {{
  center: [62, 100],
  zoom: 3,
  zoomControl: true,
}});

L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> / <a href="https://carto.com/">CARTO</a>',
  subdomains: 'abcd',
  maxZoom: 19,
}}).addTo(map);

var heatData = points.map(function(p) {{
  return [p.lat, p.lon, p.availability_score / 100.0];
}});

var heat = L.heatLayer(heatData, {{
  radius: 30,
  blur: 20,
  maxZoom: 10,
  max: 1.0,
  gradient: {{
    0.0: '#ff1744',
    0.2: '#ff9100',
    0.5: '#ffd600',
    0.8: '#00c853',
    1.0: '#00c853'
  }}
}}).addTo(map);

map.on('click', function(e) {{
  var lat = e.latlng.lat;
  var lng = e.latlng.lng;
  var closest = null, minDist = Infinity;
  points.forEach(function(p) {{
    var d = Math.hypot(p.lat - lat, p.lon - lng);
    if (d < minDist) {{ minDist = d; closest = p; }}
  }});
  if (closest && minDist < 4) {{
    var sc = closest.availability_score >= 80 ? 'color:#00c853' :
      closest.availability_score >= 50 ? 'color:#ffd600' :
      closest.availability_score >= 20 ? 'color:#ff9100' : 'color:#ff1744';
    L.popup()
      .setLatLng([closest.lat, closest.lon])
      .setContent(
        '<b>Точка ' + closest.lat.toFixed(2) + ', ' + closest.lon.toFixed(2) + '</b><br>' +
        'Всего АЗС: ' + closest.total + '<br>' +
        '&#9989; Есть: ' + closest.yes + ' (' + closest.yes_pct + '%)<br>' +
        '&#9888; Очередь: ' + closest.queue + '<br>' +
        '&#10060; Нет: ' + closest.no + '<br>' +
        'Доступность: <span style="' + sc + ';font-weight:bold;font-size:16px">' +
          closest.availability_score + '%</span>'
      )
      .openOn(map);
  }}
}});

if (points.length > 0) {{
  var bounds = points.map(function(p) {{ return [p.lat, p.lon]; }});
  map.fitBounds(bounds, {{ padding: [50, 50] }});
}}
</script>
</body>
</html>"""

    Path(output_path).write_text(html, encoding="utf-8")
    print(f"Saved HTML to {output_path} ({len(points)} points)")


async def main():
    parser = argparse.ArgumentParser(description="Scan Russia for fuel availability")
    parser.add_argument(
        "--step", type=float, default=50, help="Grid step in km (default: 50)"
    )
    parser.add_argument(
        "--radius",
        type=int,
        default=10,
        help="API radius per point in km (default: 10)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=20,
        help="Max concurrent requests (default: 20)",
    )
    parser.add_argument(
        "--output", default="fuel_heatmap.html", help="Output HTML file"
    )
    args = parser.parse_args()

    grid = generate_grid(args.step)
    print(f"Grid: {len(grid)} points (step={args.step}km, radius={args.radius}km)")

    sem = asyncio.Semaphore(args.concurrency)
    results = []
    total = len(grid)
    start = time.time()

    async with aiohttp.ClientSession() as session:
        batch_size = 20
        for i in range(0, total, batch_size):
            batch = grid[i : i + batch_size]
            tasks = [
                fetch_one(session, sem, lat, lon, args.radius) for lat, lon in batch
            ]
            batch_results = await asyncio.gather(*tasks)
            results.extend(batch_results)

            done = min(i + batch_size, total)
            ok = sum(1 for r in results if r is not None)
            elapsed = time.time() - start
            eta = (elapsed / max(done, 1)) * (total - done) if done > 0 else 0
            print(
                f"  [{done}/{total}] ok={ok} failed={done - ok} "
                f"elapsed={elapsed:.0f}s eta={eta:.0f}s"
            )

            # Delay between batches
            if i + batch_size < total:
                await asyncio.sleep(1.5)

    # Aggregate
    points = []
    for r in results:
        p = summarize_point(r)
        if p:
            points.append(p)

    elapsed = time.time() - start
    print(
        f"\nResults: {len(points)} valid points out of {total} scanned in {elapsed:.0f}s"
    )

    if points:
        avg = sum(p["availability_score"] for p in points) / len(points)
        worst = min(p["availability_score"] for p in points)
        best = max(p["availability_score"] for p in points)
        print(f"Average availability: {avg:.1f}%")
        print(f"Range: {worst:.1f}% - {best:.1f}%")

        # Save raw data as JSON
        json_path = args.output.replace(".html", ".json")
        Path(json_path).write_text(json.dumps(points, indent=1), encoding="utf-8")
        print(f"Raw data saved: {json_path}")

    # Build HTML
    build_html(points, args.output)


if __name__ == "__main__":
    asyncio.run(main())
