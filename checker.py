"""API client for gdebenz.ru and fuel-matching logic."""

from typing import TypedDict

import aiohttp

API_BASE = "https://gdebenz.ru/api"
CHECK_RADIUS_KM = 10


class Station(TypedDict):
    osm_id: str
    brand: str
    name: str
    addr: str
    lat: float
    lon: float
    distance_km: float
    status: str  # "yes", "queue", "no", "low"
    detail: str
    confirmations: int
    confirmed: bool
    last_at: str


class NearbyResponse(TypedDict):
    summary: dict
    stations: list[Station]
    updated: str


FUEL_LABELS: dict[str, str] = {
    "92": "АИ-92",
    "95": "АИ-95",
    "98": "АИ-98",
    "100": "АИ-100",
    "ДТ": "ДТ",
    "Газ": "Газ",
    "any": "Любое топливо",
}

STATUS_EMOJI = {"yes": "✅", "queue": "⚠️", "no": "❌", "low": "🤔"}
STATUS_TEXT = {
    "yes": "Есть в наличии",
    "queue": "Есть, но очередь",
    "no": "Нет",
    "low": "Маловероятно",
}


def parse_fuel_types(detail: str) -> set[str]:
    """Extract fuel types from the detail field.

    Examples:
        "95" -> {"95"}
        "92, 95, 98, 100, ДТ" -> {"92", "95", "98", "100", "ДТ"}
        "92 · Небольшая очередь" -> {"92"}
        "92, 95, ДТ · Большая очередь" -> {"92", "95", "ДТ"}
        "" -> set()
    """
    if not detail:
        return set()

    parts = detail.split("·")
    fuel_part = parts[0].strip()

    if not fuel_part:
        return set()

    fuels = {f.strip() for f in fuel_part.split(",") if f.strip()}
    return fuels


def fuel_matches(station: Station, requested_fuel: str) -> bool:
    """Check if a station has the requested fuel type."""
    if station["status"] not in ("yes", "queue"):
        return False

    if requested_fuel == "any":
        return True

    detail = station.get("detail", "")
    fuels = parse_fuel_types(detail)

    if not fuels:
        return True

    return requested_fuel in fuels


def format_station_message(station: Station, requested_fuel: str) -> str:
    """Format a station notification message."""
    brand = station.get("brand", "") or station.get("name", "Неизвестно")
    name = station.get("name", "")
    addr = station.get("addr", "")
    detail = station.get("detail", "")
    distance = station.get("distance_km", 0)
    confirmations = station.get("confirmations", 0)
    status = station.get("status", "")

    display_name = brand
    if name and name != brand:
        display_name = f"{brand} ({name})"

    lines = [f"⛽ <b>{display_name}</b>"]

    if addr:
        lines.append(f"📍 {addr}")

    lines.append(f"📏 Расстояние: <b>{distance:.1f} км</b>")

    emoji = STATUS_EMOJI.get(status, "❓")
    text = STATUS_TEXT.get(status, status)
    lines.append(f"{emoji} Статус: <b>{text}</b>")

    if detail:
        lines.append(f"📝 Детали: {detail}")

    lines.append(f"👍 Подтверждений: {confirmations}")

    return "\n".join(lines)


def build_maps_url(lat: float, lon: float) -> str:
    """Build a Yandex Navigator deep link.

    Uses yandexnavi:// scheme which opens Yandex Navigator directly.
    Falls back to Yandex Maps in browser if Navigator is not installed.
    """
    return f"https://yandex.ru/maps/?rtext=~{lat},{lon}&rtt=auto"


async def fetch_nearby(
    session: aiohttp.ClientSession,
    lat: float,
    lon: float,
    radius_km: int = CHECK_RADIUS_KM,
) -> NearbyResponse | None:
    """Fetch nearby stations from the API."""
    url = f"{API_BASE}/nearby"
    params = {"lat": lat, "lon": lon, "radius_km": radius_km}

    try:
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            return None
    except Exception:
        return None
