import asyncio
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
import math
import os
from pathlib import Path
import time

import httpx
from PIL import Image, ImageDraw, ImageFont

from app.models import Order


TILE_SIZE = 256
MAP_WIDTH = 1280
MAP_HEIGHT = 960
TILE_CACHE_SECONDS = 7 * 24 * 60 * 60
DEFAULT_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_USER_AGENT = "TEXNIKACH-DeliveryBot/1.0 (contact: texnikach@gmail.com)"

# Keep the whole city visible even when the current orders are concentrated in
# one neighbourhood. Points outside these bounds are still included by the
# viewport calculation.
TASHKENT_BOUNDS = (
    (41.200000, 69.100000),
    (41.400000, 69.450000),
)
WAREHOUSE_LATITUDE = 41.337420
WAREHOUSE_LONGITUDE = 69.272104


@dataclass(frozen=True, slots=True)
class DeliverySequenceStop:
    sequence: int
    order_number: int
    latitude: float
    longitude: float
    courier_id: int | None
    courier_name: str
    color: str


def _spread_marker_positions(
    points: list[tuple[int, int, int]],
    *,
    obstacle: tuple[int, int] | None = None,
    min_distance: float = 72,
) -> dict[int, tuple[int, int]]:
    """Spread dense numbered markers while keeping a leader to the true point."""
    placed: dict[int, tuple[int, int]] = {}
    horizontal_margin = 40
    top_margin = 90
    bottom_margin = 72
    for sequence, actual_x, actual_y in points:
        candidates = [(actual_x, actual_y)]
        phase = sequence * 2.399963229728653
        for radius in (52, 84, 118, 154, 192, 232):
            for step in range(24):
                angle = phase + (math.tau * step / 24)
                candidates.append((
                    round(actual_x + math.cos(angle) * radius),
                    round(actual_y + math.sin(angle) * radius),
                ))

        valid = [
            (
                min(max(x, horizontal_margin), MAP_WIDTH - horizontal_margin),
                min(max(y, top_margin), MAP_HEIGHT - bottom_margin),
            )
            for x, y in candidates
        ]

        def clearance(candidate: tuple[int, int]) -> float:
            distances = [
                math.hypot(candidate[0] - x, candidate[1] - y)
                for x, y in placed.values()
            ]
            if obstacle:
                distances.append(
                    math.hypot(
                        candidate[0] - obstacle[0],
                        candidate[1] - obstacle[1],
                    ) - 10
                )
            return min(distances, default=float("inf"))

        selected = next(
            (candidate for candidate in valid if clearance(candidate) >= min_distance),
            None,
        )
        if selected is None:
            selected = max(
                valid,
                key=lambda candidate: (
                    clearance(candidate),
                    -math.hypot(candidate[0] - actual_x, candidate[1] - actual_y),
                ),
            )
        placed[sequence] = selected
    return placed


@lru_cache(maxsize=16)
def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    configured = os.getenv("DELIVERY_MAP_FONT_PATH", "").strip()
    candidates = [
        configured,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
        if bold
        else "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default(size=size)


def _world_pixel(latitude: float, longitude: float, zoom: int) -> tuple[float, float]:
    latitude = max(-85.05112878, min(85.05112878, latitude))
    scale = TILE_SIZE * (2**zoom)
    x = (longitude + 180.0) / 360.0 * scale
    radians = math.radians(latitude)
    y = (
        1.0
        - math.asinh(math.tan(radians)) / math.pi
    ) / 2.0 * scale
    return x, y


def _order_points(orders: list[Order]) -> list[tuple[Order, int, float, float]]:
    points: list[tuple[Order, int, float, float]] = []
    for order in orders:
        if order.latitude is not None and order.longitude is not None:
            points.append((order, 1, order.latitude, order.longitude))
        if order.second_latitude is not None and order.second_longitude is not None:
            points.append((order, 2, order.second_latitude, order.second_longitude))
    return points


def _viewport(
    points: list[tuple[Order, int, float, float]],
) -> tuple[int, float, float]:
    return _viewport_coordinates([
        (latitude, longitude)
        for _, _, latitude, longitude in points
    ])


def _viewport_coordinates(
    extra_coordinates: list[tuple[float, float]],
) -> tuple[int, float, float]:
    coordinates = [
        TASHKENT_BOUNDS[0],
        TASHKENT_BOUNDS[1],
        (WAREHOUSE_LATITUDE, WAREHOUSE_LONGITUDE),
        *extra_coordinates,
    ]
    for zoom in range(15, 4, -1):
        pixels = [_world_pixel(latitude, longitude, zoom) for latitude, longitude in coordinates]
        xs = [pixel[0] for pixel in pixels]
        ys = [pixel[1] for pixel in pixels]
        if max(xs) - min(xs) <= MAP_WIDTH - 220 and max(ys) - min(ys) <= MAP_HEIGHT - 180:
            return zoom, (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    pixels = [_world_pixel(latitude, longitude, 4) for latitude, longitude in coordinates]
    return (
        4,
        (min(pixel[0] for pixel in pixels) + max(pixel[0] for pixel in pixels)) / 2,
        (min(pixel[1] for pixel in pixels) + max(pixel[1] for pixel in pixels)) / 2,
    )


def _label_box(
    draw: ImageDraw.ImageDraw,
    *,
    x: int,
    y: int,
    text: str,
    font: ImageFont.ImageFont,
    fill: str,
) -> None:
    bounds = draw.textbbox((0, 0), text, font=font)
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    left = max(8, min(MAP_WIDTH - width - 24, x - width / 2 - 10))
    top = max(8, min(MAP_HEIGHT - height - 18, y))
    box = (left, top, left + width + 20, top + height + 10)
    draw.rounded_rectangle(
        box,
        radius=8,
        fill=(255, 255, 255, 242),
        outline=(17, 24, 39, 180),
        width=2,
    )
    draw.text((left + 10, top + 4), text, fill=fill, font=font)


def _draw_order_marker(
    draw: ImageDraw.ImageDraw,
    *,
    x: int,
    y: int,
    order_number: int,
    location_number: int,
    marker_font: ImageFont.ImageFont,
) -> None:
    radius = 29 if location_number == 1 else 25
    fill = "#dc2626" if location_number == 1 else "#2563eb"

    # A large white halo and pin tail stay visible over both light and dark map
    # tiles, including in Telegram's reduced channel preview.
    draw.ellipse(
        (x - radius - 6, y - radius - 6, x + radius + 6, y + radius + 6),
        fill=(255, 255, 255, 230),
        outline=(17, 24, 39, 150),
        width=2,
    )
    draw.polygon(
        ((x - 13, y + radius - 3), (x + 13, y + radius - 3), (x, y + radius + 24)),
        fill=fill,
        outline="white",
    )
    draw.ellipse(
        (x - radius, y - radius, x + radius, y + radius),
        fill=fill,
        outline="white",
        width=4,
    )
    label = str(order_number)
    bounds = draw.textbbox((0, 0), label, font=marker_font)
    draw.text(
        (x - (bounds[2] - bounds[0]) / 2, y - (bounds[3] - bounds[1]) / 2 - 2),
        label,
        fill="white",
        stroke_width=1,
        stroke_fill="#7f1d1d" if location_number == 1 else "#1e3a8a",
        font=marker_font,
    )
def _draw_warehouse_marker(
    draw: ImageDraw.ImageDraw,
    *,
    x: int,
    y: int,
) -> None:
    radius = 28
    fill = "#f59e0b"
    draw.ellipse(
        (x - radius - 6, y - radius - 6, x + radius + 6, y + radius + 6),
        fill=(255, 255, 255, 235),
        outline=(17, 24, 39, 160),
        width=2,
    )
    draw.ellipse(
        (x - radius, y - radius, x + radius, y + radius),
        fill=fill,
        outline="white",
        width=4,
    )
    # Simple warehouse pictogram that does not depend on an emoji font.
    draw.polygon(
        ((x - 17, y - 5), (x, y - 17), (x + 17, y - 5)),
        fill="white",
    )
    draw.rectangle((x - 15, y - 5, x + 15, y + 15), fill="white")
    draw.rectangle((x - 6, y + 3, x + 6, y + 15), fill=fill)
    _label_box(
        draw,
        x=x,
        y=y + radius + 10,
        text="СКЛАД",
        font=_font(18, bold=True),
        fill="#92400e",
    )


async def _load_tile(
    client: httpx.AsyncClient,
    cache_dir: Path,
    template: str,
    zoom: int,
    tile_x: int,
    tile_y: int,
) -> Image.Image:
    count = 2**zoom
    wrapped_x = tile_x % count
    clamped_y = max(0, min(count - 1, tile_y))
    cache_path = cache_dir / str(zoom) / str(wrapped_x) / f"{clamped_y}.png"
    if cache_path.is_file() and time.time() - cache_path.stat().st_mtime < TILE_CACHE_SECONDS:
        with Image.open(cache_path) as cached:
            return cached.convert("RGB")

    response = await client.get(
        template.format(z=zoom, x=wrapped_x, y=clamped_y),
    )
    response.raise_for_status()
    with Image.open(BytesIO(response.content)) as downloaded:
        tile = downloaded.convert("RGB")
    if tile.size != (TILE_SIZE, TILE_SIZE):
        raise ValueError("Unexpected OpenStreetMap tile dimensions")

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_suffix(f".{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tile.save(temporary, format="PNG")
        os.replace(temporary, cache_path)
    finally:
        temporary.unlink(missing_ok=True)
    return tile


async def _render_base_map(
    coordinates: list[tuple[float, float]],
    *,
    cache_dir: Path,
    tile_url: str | None,
) -> tuple[Image.Image, int, float, float]:
    zoom, center_x, center_y = _viewport_coordinates(coordinates)
    left = center_x - MAP_WIDTH / 2
    top = center_y - MAP_HEIGHT / 2
    first_x = math.floor(left / TILE_SIZE)
    last_x = math.floor((left + MAP_WIDTH - 1) / TILE_SIZE)
    first_y = math.floor(top / TILE_SIZE)
    last_y = math.floor((top + MAP_HEIGHT - 1) / TILE_SIZE)
    tile_positions = [
        (tile_x, tile_y)
        for tile_y in range(first_y, last_y + 1)
        for tile_x in range(first_x, last_x + 1)
    ]
    template = tile_url or os.getenv("DELIVERY_MAP_TILE_URL", DEFAULT_TILE_URL)
    image = Image.new("RGB", (MAP_WIDTH, MAP_HEIGHT), "#e5e7eb")
    semaphore = asyncio.Semaphore(4)

    async with httpx.AsyncClient(
        timeout=10,
        headers={"User-Agent": TILE_USER_AGENT},
    ) as client:
        async def fetch(position: tuple[int, int]):
            async with semaphore:
                tile_x, tile_y = position
                return position, await _load_tile(
                    client,
                    cache_dir,
                    template,
                    zoom,
                    tile_x,
                    tile_y,
                )

        loaded = await asyncio.gather(
            *(fetch(position) for position in tile_positions),
            return_exceptions=True,
        )

    successful_tiles = 0
    for result in loaded:
        if isinstance(result, BaseException):
            continue
        (tile_x, tile_y), tile = result
        successful_tiles += 1
        image.paste(
            tile,
            (
                round(tile_x * TILE_SIZE - left),
                round(tile_y * TILE_SIZE - top),
            ),
        )
    if successful_tiles == 0:
        raise RuntimeError("No map tiles could be downloaded")
    return image, zoom, left, top


def _draw_attribution(draw: ImageDraw.ImageDraw) -> None:
    attribution = "© OpenStreetMap contributors"
    attribution_font = _font(14)
    bounds = draw.textbbox((0, 0), attribution, font=attribution_font)
    padding = 6
    box = (
        MAP_WIDTH - (bounds[2] - bounds[0]) - padding * 2 - 5,
        MAP_HEIGHT - (bounds[3] - bounds[1]) - padding * 2 - 5,
        MAP_WIDTH - 5,
        MAP_HEIGHT - 5,
    )
    draw.rounded_rectangle(box, radius=4, fill=(255, 255, 255, 220))
    draw.text(
        (box[0] + padding, box[1] + padding),
        attribution,
        fill="#111827",
        font=attribution_font,
    )


async def render_active_orders_map(
    orders: list[Order],
    *,
    cache_dir: Path,
    tile_url: str | None = None,
) -> BytesIO | None:
    """Render Tashkent, the warehouse and current active orders as a PNG."""
    points = _order_points(orders)
    image, zoom, left, top = await _render_base_map(
        [(latitude, longitude) for _, _, latitude, longitude in points],
        cache_dir=cache_dir,
        tile_url=tile_url,
    )

    draw = ImageDraw.Draw(image, "RGBA")
    marker_font = _font(23, bold=True)

    warehouse_world_x, warehouse_world_y = _world_pixel(
        WAREHOUSE_LATITUDE,
        WAREHOUSE_LONGITUDE,
        zoom,
    )
    _draw_warehouse_marker(
        draw,
        x=round(warehouse_world_x - left),
        y=round(warehouse_world_y - top),
    )

    for order, location_number, latitude, longitude in points:
        world_x, world_y = _world_pixel(latitude, longitude, zoom)
        x = round(world_x - left)
        y = round(world_y - top)
        _draw_order_marker(
            draw,
            x=x,
            y=y,
            order_number=order.order_number,
            location_number=location_number,
            marker_font=marker_font,
        )

    legend_font = _font(18, bold=True)
    legend = f"Ташкент · активных точек: {len(points)} · красные — заказы · синие — доп. адреса"
    legend_bounds = draw.textbbox((0, 0), legend, font=legend_font)
    legend_box = (10, 10, legend_bounds[2] - legend_bounds[0] + 30, 48)
    draw.rounded_rectangle(legend_box, radius=8, fill=(255, 255, 255, 235))
    draw.text((20, 18), legend, fill="#111827", font=legend_font)

    _draw_attribution(draw)

    output = BytesIO()
    output.name = "active-deliveries.png"
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


async def render_delivery_sequence_map(
    stops: list[DeliverySequenceStop],
    *,
    report_day_label: str,
    cache_dir: Path,
    tile_url: str | None = None,
) -> BytesIO:
    """Render numbered delivery order and per-courier warehouse routes."""
    image, zoom, left, top = await _render_base_map(
        [(stop.latitude, stop.longitude) for stop in stops],
        cache_dir=cache_dir,
        tile_url=tile_url,
    )
    draw = ImageDraw.Draw(image, "RGBA")

    def pixel(latitude: float, longitude: float) -> tuple[int, int]:
        world_x, world_y = _world_pixel(latitude, longitude, zoom)
        return round(world_x - left), round(world_y - top)

    warehouse = pixel(WAREHOUSE_LATITUDE, WAREHOUSE_LONGITUDE)
    grouped: dict[int | None, list[DeliverySequenceStop]] = {}
    for stop in stops:
        grouped.setdefault(stop.courier_id, []).append(stop)
    for route_stops in grouped.values():
        route_stops.sort(key=lambda stop: stop.sequence)
        route_points = [
            warehouse,
            *(pixel(stop.latitude, stop.longitude) for stop in route_stops),
            warehouse,
        ]
        route_color = route_stops[0].color
        if len(route_points) >= 3:
            draw.line(route_points, fill=(255, 255, 255, 220), width=13, joint="curve")
            draw.line(route_points, fill=route_color, width=7, joint="curve")

    ordered_stops = sorted(stops, key=lambda item: item.sequence)
    actual_pixels = {
        stop.sequence: pixel(stop.latitude, stop.longitude)
        for stop in ordered_stops
    }
    marker_pixels = _spread_marker_positions(
        [
            (stop.sequence, *actual_pixels[stop.sequence])
            for stop in ordered_stops
        ],
        obstacle=warehouse,
    )

    for stop in ordered_stops:
        actual = actual_pixels[stop.sequence]
        displayed = marker_pixels[stop.sequence]
        if math.hypot(displayed[0] - actual[0], displayed[1] - actual[1]) > 12:
            draw.line((actual, displayed), fill=(255, 255, 255, 235), width=7)
            draw.line((actual, displayed), fill=stop.color, width=3)
            draw.ellipse(
                (actual[0] - 5, actual[1] - 5, actual[0] + 5, actual[1] + 5),
                fill=stop.color,
                outline="white",
                width=2,
            )

    _draw_warehouse_marker(draw, x=warehouse[0], y=warehouse[1])
    number_font = _font(23, bold=True)
    for stop in ordered_stops:
        x, y = marker_pixels[stop.sequence]
        radius = 28
        draw.ellipse(
            (x - radius - 6, y - radius - 6, x + radius + 6, y + radius + 6),
            fill=(255, 255, 255, 235),
            outline=(17, 24, 39, 180),
            width=2,
        )
        draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            fill=stop.color,
            outline="white",
            width=4,
        )
        label = str(stop.sequence)
        bounds = draw.textbbox((0, 0), label, font=number_font)
        draw.text(
            (x - (bounds[2] - bounds[0]) / 2, y - (bounds[3] - bounds[1]) / 2 - 2),
            label,
            fill="white",
            stroke_width=1,
            stroke_fill="#111827",
            font=number_font,
        )

    legend_font = _font(18, bold=True)
    legend = (
        f"Очередность доставок · {report_day_label} · "
        f"остановок: {len(stops)} · склад → заказы → склад"
    )
    bounds = draw.textbbox((0, 0), legend, font=legend_font)
    legend_box = (10, 10, min(MAP_WIDTH - 10, bounds[2] - bounds[0] + 30), 48)
    draw.rounded_rectangle(legend_box, radius=8, fill=(255, 255, 255, 238))
    draw.text((20, 18), legend, fill="#111827", font=legend_font)
    _draw_attribution(draw)

    output = BytesIO()
    output.name = f"delivery-sequence-{report_day_label}.png"
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output
