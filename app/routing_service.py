from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.stats_service import TASHKENT, local_datetime


DEFAULT_ROUTING_URL = (
    "https://routing.openstreetmap.de/routed-car/route/v1/driving"
)
ROUTING_USER_AGENT = "TEXNIKACH-Delivery/1.0 (texnikach@gmail.com)"
CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
FAILURE_COOLDOWN_SECONDS = 60
MIN_REQUEST_INTERVAL_SECONDS = 1.05


def _haversine_m(start: list[float], finish: list[float]) -> float:
    latitude_1, longitude_1 = map(math.radians, start)
    latitude_2, longitude_2 = map(math.radians, finish)
    delta_latitude = latitude_2 - latitude_1
    delta_longitude = longitude_2 - longitude_1
    value = (
        math.sin(delta_latitude / 2) ** 2
        + math.cos(latitude_1)
        * math.cos(latitude_2)
        * math.sin(delta_longitude / 2) ** 2
    )
    return 2 * 6_371_000 * math.asin(math.sqrt(value))


def _fallback(points: list[list[float]]) -> dict[str, Any]:
    legs = []
    total_distance = 0.0
    total_duration = 0.0
    for start, finish in zip(points, points[1:]):
        # Urban roads are typically longer than the straight line. The factor
        # is only a resilient fallback while the road router is unavailable.
        distance = _haversine_m(start, finish) * 1.28
        duration = distance / (24_000 / 3_600)
        total_distance += distance
        total_duration += duration
        legs.append({
            "geometry": [start, finish],
            "distance_m": round(distance),
            "duration_s": round(duration),
            "summary": "Приближённый прямой расчёт",
        })
    return {
        "provider": "fallback",
        "approximate": True,
        "geometry": points,
        "legs": legs,
        "distance_m": round(total_distance),
        "duration_s": round(total_duration),
    }


def _deduplicate(points: list[list[float]]) -> list[list[float]]:
    result: list[list[float]] = []
    for point in points:
        normalized = [round(float(point[0]), 6), round(float(point[1]), 6)]
        if not result or result[-1] != normalized:
            result.append(normalized)
    return result


class RoutingService:
    """Road routes with a persistent cache and public-service rate limiting."""

    def __init__(self, cache_path: Path, base_url: str | None = None):
        self.cache_path = cache_path
        self.base_url = (
            base_url
            or os.getenv("DELIVERY_ROUTING_URL")
            or DEFAULT_ROUTING_URL
        ).rstrip("/")
        self._request_lock = threading.Lock()
        self._last_request_at = 0.0
        self._failed_until = 0.0
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.cache_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """CREATE TABLE IF NOT EXISTS road_routes (
                       cache_key TEXT PRIMARY KEY,
                       payload TEXT NOT NULL,
                       created_at REAL NOT NULL
                   )"""
            )

    def _key(self, points: list[list[float]]) -> str:
        raw = json.dumps(
            {"base_url": self.base_url, "points": points},
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    def _cached(self, key: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload,created_at FROM road_routes WHERE cache_key=?",
                (key,),
            ).fetchone()
        if not row or time.time() - row["created_at"] > CACHE_TTL_SECONDS:
            return None
        try:
            return json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            return None

    def _store(self, key: str, payload: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO road_routes(cache_key,payload,created_at)
                   VALUES(?,?,?)
                   ON CONFLICT(cache_key) DO UPDATE SET
                     payload=excluded.payload,
                     created_at=excluded.created_at""",
                (key, json.dumps(payload, separators=(",", ":")), time.time()),
            )

    @staticmethod
    def _leg_geometry(leg: dict[str, Any], start: list[float], finish: list[float]) -> list[list[float]]:
        result: list[list[float]] = []
        for step in leg.get("steps") or []:
            coordinates = (step.get("geometry") or {}).get("coordinates") or []
            for longitude, latitude in coordinates:
                point = [round(float(latitude), 6), round(float(longitude), 6)]
                if not result or result[-1] != point:
                    result.append(point)
        return result if len(result) >= 2 else [start, finish]

    def _fetch(self, points: list[list[float]]) -> dict[str, Any]:
        coordinates = ";".join(
            f"{longitude:.6f},{latitude:.6f}"
            for latitude, longitude in points
        )
        url = (
            f"{self.base_url}/{coordinates}"
            "?overview=full&geometries=geojson&steps=true"
        )
        request = Request(
            url,
            headers={
                "User-Agent": ROUTING_USER_AGENT,
                "Accept": "application/json",
            },
        )
        with urlopen(request, timeout=8) as response:
            payload = json.load(response)
        if payload.get("code") != "Ok" or not payload.get("routes"):
            raise ValueError(payload.get("message") or "road route not found")
        route = payload["routes"][0]
        raw_legs = route.get("legs") or []
        if len(raw_legs) != len(points) - 1:
            raise ValueError("road router returned an incomplete route")
        legs = []
        geometry: list[list[float]] = []
        for index, leg in enumerate(raw_legs):
            leg_geometry = self._leg_geometry(leg, points[index], points[index + 1])
            if geometry and leg_geometry and geometry[-1] == leg_geometry[0]:
                geometry.extend(leg_geometry[1:])
            else:
                geometry.extend(leg_geometry)
            legs.append({
                "geometry": leg_geometry,
                "distance_m": round(float(leg.get("distance") or 0)),
                "duration_s": round(float(leg.get("duration") or 0)),
                "summary": (leg.get("summary") or "").strip(),
            })
        return {
            "provider": "osrm",
            "approximate": True,
            "geometry": geometry,
            "legs": legs,
            "distance_m": round(float(route.get("distance") or 0)),
            "duration_s": round(float(route.get("duration") or 0)),
        }

    def _route_sync(self, points: list[list[float]]) -> dict[str, Any]:
        normalized = _deduplicate(points)
        if len(normalized) < 2:
            return _fallback(normalized)
        key = self._key(normalized)
        cached = self._cached(key)
        if cached:
            return cached
        if time.monotonic() < self._failed_until:
            return _fallback(normalized)
        with self._request_lock:
            cached = self._cached(key)
            if cached:
                return cached
            wait = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            try:
                result = self._fetch(normalized)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
                self._failed_until = time.monotonic() + FAILURE_COOLDOWN_SECONDS
                return _fallback(normalized)
            finally:
                self._last_request_at = time.monotonic()
            self._store(key, result)
            return result

    async def route(self, points: list[list[float]]) -> dict[str, Any]:
        return await asyncio.to_thread(self._route_sync, points)


def _progress(started_at: str | None, duration_s: int) -> float:
    started = local_datetime(started_at)
    if not started or duration_s <= 0:
        return 0.0
    elapsed = (datetime.now(TASHKENT) - started).total_seconds()
    return max(0.0, min(1.0, elapsed / duration_s))


def _eta(started_at: str | None, duration_s: int) -> str | None:
    started = local_datetime(started_at)
    if not started or duration_s <= 0:
        return None
    return (started + timedelta(seconds=duration_s)).isoformat(timespec="seconds")


async def enrich_monitor_routes(
    state: dict[str, Any],
    routing: RoutingService,
) -> dict[str, Any]:
    total_driven = 0.0
    for route in state["routes"]:
        dark_results = []
        for path in route.get("dark_paths") or []:
            if len(path) >= 2:
                dark_results.append(await routing.route(path))
        route["completed_road_segments"] = [
            {
                "geometry": item["geometry"],
                "distance_km": round(item["distance_m"] / 1_000, 1),
                "duration_minutes": max(1, round(item["duration_s"] / 60)),
            }
            for item in dark_results
        ]
        route["dark_road_paths"] = [item["geometry"] for item in dark_results]
        completed_distance = sum(item["distance_m"] for item in dark_results)

        movement_path = route.get("current_path") or route.get("return_path") or []
        movement = await routing.route(movement_path) if len(movement_path) >= 2 else None
        movement_progress = (
            _progress(route.get("movement_started_at"), movement["duration_s"])
            if movement
            else 0.0
        )
        movement_distance = (movement["distance_m"] * movement_progress) if movement else 0
        route["current_road_path"] = (
            movement["geometry"] if movement and route.get("movement_kind") == "delivery" else []
        )
        route["return_road_path"] = (
            movement["geometry"] if movement and route.get("movement_kind") == "return" else []
        )
        route["movement"] = (
            {
                "kind": route.get("movement_kind"),
                "started_at": route.get("movement_started_at"),
                "eta_at": _eta(route.get("movement_started_at"), movement["duration_s"]),
                "duration_seconds": movement["duration_s"],
                "duration_minutes": max(1, round(movement["duration_s"] / 60)),
                "distance_km": round(movement["distance_m"] / 1_000, 1),
                "progress": round(movement_progress, 4),
                "geometry": movement["geometry"],
                "provider": movement["provider"],
            }
            if movement
            else None
        )
        route["distance_today_km"] = round(
            (completed_distance + movement_distance) / 1_000,
            1,
        )
        route["planned_remaining_km"] = round(
            ((movement["distance_m"] - movement_distance) if movement else 0) / 1_000,
            1,
        )
        total_driven += completed_distance + movement_distance

    route_by_courier = {route["courier_id"]: route for route in state["routes"]}
    for courier in state["couriers"]:
        route = route_by_courier[courier["id"]]
        courier["distance_today_km"] = route["distance_today_km"]
        courier["planned_remaining_km"] = route["planned_remaining_km"]
        courier["movement"] = route["movement"]
    state["summary"]["distance_today_km"] = round(total_driven / 1_000, 1)
    state["routing_attribution"] = "Маршруты: OSRM / © OpenStreetMap contributors"
    return state


async def enrich_stats_routes(
    report: dict[str, Any],
    routing: RoutingService,
) -> dict[str, Any]:
    total_distance = 0.0
    total_planned = 0.0
    for route in report["routes"]:
        completed_results = []
        for path in route.get("completed_paths") or []:
            if len(path) >= 2:
                completed_results.append(await routing.route(path))
        current = (
            await routing.route(route["current_path"])
            if len(route.get("current_path") or []) >= 2
            else None
        )
        returning = (
            await routing.route(route["return_path"])
            if len(route.get("return_path") or []) >= 2
            else None
        )
        completed_distance = sum(item["distance_m"] for item in completed_results)
        current_distance = current["distance_m"] if current else 0
        return_distance = returning["distance_m"] if returning else 0
        route["completed_road_paths"] = [item["geometry"] for item in completed_results]
        route["completed_road_segments"] = [
            {
                "geometry": item["geometry"],
                "distance_km": round(item["distance_m"] / 1_000, 1),
                "duration_minutes": max(1, round(item["duration_s"] / 60)),
            }
            for item in completed_results
        ]
        route["current_road_path"] = current["geometry"] if current else []
        route["return_road_path"] = returning["geometry"] if returning else []
        route["distance_km"] = round((completed_distance + return_distance) / 1_000, 1)
        route["planned_distance_km"] = round(current_distance / 1_000, 1)
        route["completed_minutes"] = round(
            sum(item["duration_s"] for item in completed_results) / 60
        )
        route["return_minutes"] = (
            max(1, round(returning["duration_s"] / 60)) if returning else 0
        )
        route["estimated_minutes"] = (
            max(1, round(current["duration_s"] / 60)) if current else None
        )
        total_distance += completed_distance + return_distance
        total_planned += current_distance

    route_by_courier = {route["courier_id"]: route for route in report["routes"]}
    for courier in report["couriers"]:
        route = route_by_courier.get(courier["id"], {})
        courier["distance_km"] = route.get("distance_km", 0)
        courier["planned_distance_km"] = route.get("planned_distance_km", 0)
        courier["route_minutes"] = (
            route.get("completed_minutes", 0) + route.get("return_minutes", 0)
        )
    report["summary"]["distance_km"] = round(total_distance / 1_000, 1)
    report["summary"]["planned_distance_km"] = round(total_planned / 1_000, 1)
    return report
