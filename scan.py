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


def _geo_to_svg(lat: float, lon: float, width: int, height: int) -> tuple[float, float]:
    """Project lat/lon to SVG x/y using Mercator-like projection for Russia."""
    lat_min, lat_max = 43.0, 72.0
    lon_min, lon_max = 28.0, 180.0
    x = (lon - lon_min) / (lon_max - lon_min) * width * 0.85 + width * 0.08
    # Meridian compression at high latitudes
    lat_rad = math.radians(lat)
    lat_rad_min = math.radians(lat_min)
    lat_rad_max = math.radians(lat_max)
    y = 1 - (lat_rad - lat_rad_min) / (lat_rad_max - lat_rad_min)
    y = y * height * 0.75 + height * 0.15
    return (x, y)


def build_html(points: list[dict], output_path: str) -> None:
    """Build a self-contained HTML with SVG map of Russia."""
    W, H = 1400, 900

    # Russia outline (simplified path in geographic coords: lon,lat)
    # Approximate border of Russian Federation (key points)
    ru_border = [
        (28.0, 54.5),
        (29.5, 55.0),
        (31.0, 54.8),
        (32.0, 55.5),
        (33.5, 54.5),
        (35.0, 55.0),
        (36.0, 56.0),
        (37.0, 55.0),
        (38.0, 56.5),
        (39.0, 57.0),
        (40.0, 58.0),
        (42.0, 59.0),
        (44.0, 60.0),
        (46.0, 61.0),
        (48.0, 62.0),
        (50.0, 63.0),
        (52.0, 64.0),
        (55.0, 65.0),
        (58.0, 66.0),
        (60.0, 67.0),
        (63.0, 68.0),
        (66.0, 69.0),
        (70.0, 69.5),
        (73.0, 70.0),
        (76.0, 71.0),
        (80.0, 72.0),
        (85.0, 72.0),
        (90.0, 71.0),
        (95.0, 70.0),
        (100.0, 70.0),
        (105.0, 71.0),
        (110.0, 72.0),
        (115.0, 72.0),
        (120.0, 71.0),
        (125.0, 70.0),
        (130.0, 69.0),
        (135.0, 68.0),
        (140.0, 67.0),
        (145.0, 66.0),
        (150.0, 65.0),
        (155.0, 64.0),
        (160.0, 63.0),
        (165.0, 62.0),
        (170.0, 61.0),
        (175.0, 60.0),
        (180.0, 59.0),
        (178.0, 55.0),
        (172.0, 53.0),
        (168.0, 52.0),
        (165.0, 50.0),
        (162.0, 49.0),
        (160.0, 48.0),
        (158.0, 47.0),
        (155.0, 48.0),
        (152.0, 49.0),
        (148.0, 50.0),
        (145.0, 51.0),
        (142.0, 52.0),
        (140.0, 53.0),
        (138.0, 54.0),
        (136.0, 55.0),
        (134.0, 56.0),
        (132.0, 57.0),
        (130.0, 56.5),
        (128.0, 56.0),
        (126.0, 55.0),
        (124.0, 54.0),
        (122.0, 53.5),
        (120.0, 53.0),
        (118.0, 52.0),
        (116.0, 51.5),
        (114.0, 51.0),
        (112.0, 51.5),
        (110.0, 52.0),
        (108.0, 53.0),
        (106.0, 54.0),
        (104.0, 55.0),
        (102.0, 56.0),
        (100.0, 57.0),
        (98.0, 57.5),
        (96.0, 57.0),
        (94.0, 56.0),
        (92.0, 55.0),
        (90.0, 54.0),
        (88.0, 53.0),
        (86.0, 52.0),
        (84.0, 51.0),
        (82.0, 50.5),
        (80.0, 50.0),
        (78.0, 49.5),
        (76.0, 49.0),
        (74.0, 48.5),
        (72.0, 48.0),
        (70.0, 47.5),
        (68.0, 47.0),
        (66.0, 47.5),
        (64.0, 48.0),
        (62.0, 48.5),
        (60.0, 49.0),
        (58.0, 49.5),
        (56.0, 50.0),
        (54.0, 50.5),
        (52.0, 51.0),
        (50.0, 51.5),
        (48.0, 52.0),
        (46.0, 52.5),
        (44.0, 53.0),
        (42.0, 53.5),
        (40.0, 54.0),
        (38.0, 54.5),
        (36.0, 54.0),
        (34.0, 54.5),
        (32.0, 54.0),
        (30.0, 54.5),
        (28.0, 54.5),
    ]
    # Crimea
    crimea = [
        (33.5, 44.4),
        (34.0, 45.0),
        (34.5, 45.5),
        (35.0, 45.0),
        (35.5, 44.5),
        (36.0, 45.0),
        (36.5, 45.5),
        (36.0, 46.0),
        (35.0, 46.5),
        (34.0, 46.0),
        (33.5, 45.5),
        (33.0, 45.0),
        (33.5, 44.4),
    ]
    # Kaliningrad
    kaliningrad = [
        (28.0, 54.5),
        (29.0, 55.0),
        (30.0, 55.0),
        (30.5, 54.5),
        (30.0, 54.0),
        (29.0, 54.0),
        (28.0, 54.5),
    ]

    def path_d(coords, close=True):
        parts = []
        for i, (lon, lat) in enumerate(coords):
            x, y = _geo_to_svg(lat, lon, W, H)
            cmd = "M" if i == 0 else "L"
            parts.append(f"{cmd}{x:.0f},{y:.0f}")
        if close:
            parts.append("Z")
        return " ".join(parts)

    border_d = path_d(ru_border)
    crimea_d = path_d(crimea)
    kaliningrad_d = path_d(kaliningrad)

    # Generate circles
    circles_svg = ""
    tooltip_data = []
    for p in points:
        cx, cy = _geo_to_svg(p["lat"], p["lon"], W, H)
        score = p["availability_score"]
        if score >= 80:
            color = "#00c853"
        elif score >= 50:
            color = "#ffd600"
        elif score >= 20:
            color = "#ff9100"
        else:
            color = "#ff1744"
        r = max(3, min(10, 4 + score / 20))
        opacity = max(0.3, score / 100)
        tid = f"p{len(tooltip_data)}"
        circles_svg += f"""<circle cx="{cx:.0f}" cy="{cy:.0f}" r="{r:.0f}"
    fill="{color}" fill-opacity="{opacity:.2f}" stroke="{color}" stroke-width="0.5"
    class="dot" data-idx="{len(tooltip_data)}"/>\n"""
        tooltip_data.append(
            f"""<?xhr id: {len(tooltip_data)}, lat: {p["lat"]}, lon: {p["lon"]}, score: {score}%, yes: {p["yes"]}, total: {p["total"]}, queue: {p["queue"]}, no: {p["no"]}"""
        )
    # Mark selected cities for reference
    cities = [
        ("Москва", 55.76, 37.62),
        ("СПб", 59.93, 30.33),
        ("Казань", 55.80, 49.10),
        ("Екб", 56.84, 60.65),
        ("Новосиб", 55.01, 82.93),
        ("Красноярск", 56.01, 92.85),
        ("Иркутск", 52.29, 104.30),
        ("Хабаровск", 48.48, 135.07),
        ("Владивосток", 43.11, 131.88),
        ("Уфа", 54.74, 55.97),
        ("РостовнД", 47.24, 39.71),
        ("Мурманск", 68.97, 33.08),
    ]
    cities_svg = ""
    for name, lat, lon in cities:
        cx, cy = _geo_to_svg(lat, lon, W, H)
        cities_svg += (
            f"""<text x="{cx:.0f}" y="{cy - 6:.0f}" class="city">{name}</text>\n"""
        )

    # Build JSON for tooltips (inline data)
    tooltip_json = json.dumps(
        [
            {
                "i": i,
                "lat": p["lat"],
                "lon": p["lon"],
                "score": p["availability_score"],
                "yes_pct": p["yes_pct"],
                "yes": p["yes"],
                "total": p["total"],
                "queue": p["queue"],
                "no": p["no"],
            }
            for i, p in enumerate(points)
        ]
    )

    avg_score = sum(p["availability_score"] for p in points) / len(points)
    worst_score = min(p["availability_score"] for p in points)

    static_points = len(points)

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Топливная карта России — gdebenz.ru</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ background: #0a1628; font-family: -apple-system, system-ui, sans-serif; color: #e0e0e0; display: flex; flex-direction: column; align-items: center; }}
h1 {{ margin: 16px 0 4px; font-size: 20px; color: #fff; }}
.subtitle {{ color: #888; font-size: 13px; margin-bottom: 8px; }}
#map {{ width: 100%; max-width: 1400px; }}
#map svg {{ width: 100%; height: auto; }}
#stats {{ position: fixed; top: 12px; right: 12px; background: rgba(10,22,40,0.93); padding: 12px 16px; border-radius: 10px; border: 1px solid #333; font-size: 13px; z-index: 10; min-width: 200px; }}
#stats h3 {{ color: #fff; font-size: 14px; margin-bottom: 6px; }}
.legend {{ margin-top: 8px; }}
.legend-item {{ display: flex; align-items: center; gap: 6px; margin: 2px 0; font-size: 12px; }}
.legend-item span {{ width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; }}
#tooltip {{ position: fixed; display: none; background: rgba(10,22,40,0.95); border: 1px solid #444; border-radius: 8px; padding: 10px 14px; font-size: 13px; pointer-events: none; z-index: 20; line-height: 1.6; }}
#tooltip .tt-title {{ color: #fff; font-weight: bold; }}
#tooltip .tt-score {{ font-size: 18px; font-weight: bold; }}
.score-high {{ color: #00c853; }}
.score-mid {{ color: #ffd600; }}
.score-low {{ color: #ff9100; }}
.score-bad {{ color: #ff1744; }}
.border {{ fill: none; stroke: #4a6a8a; stroke-width: 1; }}
.border-sea {{ fill: none; stroke: #3a5a7a; stroke-width: 0.5; stroke-dasharray: 3,3; }}
.city {{ fill: #6688aa; font-size: 9px; text-anchor: middle; }}
.land {{ fill: #1a2a3e; stroke: #3a5a7a; stroke-width: 0.8; }}
.footer {{ margin: 12px 0 24px; color: #555; font-size: 12px; text-align: center; }}
.footer a {{ color: #6688aa; }}
</style>
</head>
<body>

<div id="stats">
  <h3>&#9981; Топливная карта</h3>
  <div>Точек: <b>{static_points}</b></div>
  <div>Средняя доступность: <b class="{"score-high" if avg_score >= 80 else "score-mid" if avg_score >= 50 else "score-low"}">{avg_score:.1f}%</b></div>
  <div>Худшая зона: <b class="score-bad">{worst_score:.1f}%</b></div>
  <div class="legend">
    <div class="legend-item"><span style="background:#00c853"></span> 80-100% — есть</div>
    <div class="legend-item"><span style="background:#ffd600"></span> 50-80% — средне</div>
    <div class="legend-item"><span style="background:#ff9100"></span> 20-50% — проблемы</div>
    <div class="legend-item"><span style="background:#ff1744"></span> 0-20% — критично</div>
  </div>
  <div style="margin-top:6px;font-size:11px;color:#666;">
    Данные: gdebenz.ru<br>
    {tooltip_json.split("items")[0] if False else "Наведите на точку"}
  </div>
</div>

<div id="tooltip"></div>

<div id="map">
<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">
  <!-- Russia landmass -->
  <path class="land" d="{border_d}"/>
  <path class="land" d="{crimea_d}"/>
  <path class="land" d="{kaliningrad_d}"/>

  <!-- Cities -->
  {cities_svg}

  <!-- Fuel dots -->
  <g id="dots">
{circles_svg}
  </g>
</svg>
</div>

<div class="footer">
  Данные: <a href="https://gdebenz.ru" target="_blank">gdebenz.ru</a> |
  сканирование выполнено в <code>{time.strftime("%Y-%m-%d %H:%M")}</code>
</div>

<script>
const data = {tooltip_json};
const tooltip = document.getElementById('tooltip');

function getScoreClass(score) {{
  if (score >= 80) return 'score-high';
  if (score >= 50) return 'score-mid';
  if (score >= 20) return 'score-low';
  return 'score-bad';
}}

document.querySelectorAll('.dot').forEach(function(el) {{
  el.addEventListener('mouseenter', function(e) {{
    var idx = this.getAttribute('data-idx');
    var d = data[parseInt(idx)];
    if (!d) return;
    var sc = getScoreClass(d.score);
    tooltip.innerHTML =
      '<div class="tt-title">Точка ' + d.lat.toFixed(2) + ', ' + d.lon.toFixed(2) + '</div>' +
      '<div>Доступность: <span class="tt-score ' + sc + '">' + d.score + '%</span></div>' +
      '<div>&#9989; Есть: ' + d.yes + ' (' + d.yes_pct + '%)</div>' +
      '<div>&#9888; Очередь: ' + d.queue + '</div>' +
      '<div>&#10060; Нет: ' + d.no + '</div>' +
      '<div>Всего АЗС: ' + d.total + '</div>';
    tooltip.style.display = 'block';
    tooltip.style.left = (e.clientX + 14) + 'px';
    tooltip.style.top = (e.clientY - 10) + 'px';
  }});
  el.addEventListener('mousemove', function(e) {{
    tooltip.style.left = (e.clientX + 14) + 'px';
    tooltip.style.top = (e.clientY - 10) + 'px';
  }});
  el.addEventListener('mouseleave', function() {{
    tooltip.style.display = 'none';
  }});
}});
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
