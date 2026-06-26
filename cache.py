"""In-memory cache and clustering for API responses.

Groups users into geographic clusters. Each cluster gets ONE API call
with a radius large enough to cover all users in that cluster.
Results are cached and filtered per-user by distance and fuel type.
"""

import math
import time
from dataclasses import dataclass, field

from checker import Station

# Users within this distance (km) of a cluster center are grouped together
MAX_CLUSTER_RADIUS_KM = 30
# How long to keep a cached API response (seconds) — slightly less than check interval
CACHE_TTL_SECONDS = 290  # ~5 min, check interval is 300s


@dataclass
class Cluster:
    """A group of nearby users sharing one API call."""

    center_lat: float
    center_lon: float
    fetch_radius_km: float  # radius needed to cover all users' 10km search areas
    user_ids: set[int] = field(default_factory=set)

    # Cached API response
    stations: list[Station] = field(default_factory=list)
    fetched_at: float = 0.0


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance between two points in km (haversine formula)."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def build_clusters(users: list[dict]) -> list[Cluster]:
    """Group users into geographic clusters.

    Each user has: chat_id, lat, lon, fuel_type.
    Returns list of clusters with fetch radii computed.
    """
    if not users:
        return []

    clusters: list[Cluster] = []

    for user in users:
        ulat = user["lat"]
        ulon = user["lon"]
        uid = user["chat_id"]

        # Find the nearest existing cluster
        best_cluster = None
        best_dist = float("inf")

        for c in clusters:
            d = _haversine_km(ulat, ulon, c.center_lat, c.center_lon)
            if d < best_dist:
                best_dist = d
                best_cluster = c

        # If within range, add to existing cluster
        if best_cluster is not None and best_dist <= MAX_CLUSTER_RADIUS_KM:
            best_cluster.user_ids.add(uid)
            # Recompute center as average
            _recompute_center(best_cluster, users)
        else:
            # Create new cluster
            c = Cluster(
                center_lat=ulat,
                center_lon=ulon,
                fetch_radius_km=10.0,  # will be expanded below
                user_ids={uid},
            )
            clusters.append(c)

    # Compute fetch radius for each cluster:
    # max distance from center to any user + 10km (user's personal search radius)
    user_map = {u["chat_id"]: u for u in users}
    for c in clusters:
        max_dist = 0.0
        for uid in c.user_ids:
            u = user_map[uid]
            d = _haversine_km(c.center_lat, c.center_lon, u["lat"], u["lon"])
            if d > max_dist:
                max_dist = d
        # Fetch radius = distance to farthest user + 10km search radius + 5km buffer
        c.fetch_radius_km = math.ceil(max_dist + 15)

    return clusters


def _recompute_center(cluster: Cluster, users: list[dict]) -> None:
    """Recompute cluster center as average of all member coordinates."""
    user_map = {u["chat_id"]: u for u in users}
    total_lat = 0.0
    total_lon = 0.0
    count = 0
    for uid in cluster.user_ids:
        if uid in user_map:
            total_lat += user_map[uid]["lat"]
            total_lon += user_map[uid]["lon"]
            count += 1
    if count > 0:
        cluster.center_lat = total_lat / count
        cluster.center_lon = total_lon / count


def is_cache_valid(cluster: Cluster) -> bool:
    """Check if the cached response is still fresh."""
    return (
        cluster.fetched_at > 0
        and (time.time() - cluster.fetched_at) < CACHE_TTL_SECONDS
        and len(cluster.stations) > 0
    )


def filter_stations_for_user(
    stations: list[Station],
    user_lat: float,
    user_lon: float,
    fuel_type: str,
    radius_km: float = 10.0,
) -> list[Station]:
    """Filter cached stations: only those within user's radius and matching fuel type."""
    from checker import fuel_matches

    result = []
    for s in stations:
        # Check distance
        d = _haversine_km(user_lat, user_lon, s["lat"], s["lon"])
        if d > radius_km:
            continue
        # Check fuel
        if not fuel_matches(s, fuel_type):
            continue
        # Add computed distance
        s = dict(s)  # shallow copy so we don't mutate cache
        s["distance_km"] = round(d, 1)
        result.append(s)

    result.sort(key=lambda s: s.get("distance_km", 999))
    return result
