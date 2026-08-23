import asyncio
from io import BytesIO
import math
import os
from pathlib import Path
import time

import httpx
from PIL import Image, ImageDraw, ImageFont

from app.models import Order


TILE_SIZE = 256
MAP_WIDTH = 900
MAP_HEIGHT = 600
TILE_CACHE_SECONDS = 7 * 24 * 60 * 60
DEFAULT_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_USER_AGENT = "TEXNIKACH-DeliveryBot/1.0 (contact: texnikach@gmail.com)"


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
    for zoom in range(15, 5, -1):
        pixels = [_world_pixel(latitude, longitude, zoom) for _, _, latitude, longitude in points]
        xs = [pixel[0] for pixel in pixels]
        ys = [pixel[1] for pixel in pixels]
        if max(xs) - min(xs) <= MAP_WIDTH - 180 and max(ys) - min(ys) <= MAP_HEIGHT - 180:
            return zoom, (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    pixels = [_world_pixel(latitude, longitude, 5) for _, _, latitude, longitude in points]
    return (
        5,
        (min(pixel[0] for pixel in pixels) + max(pixel[0] for pixel in pixels)) / 2,
        (min(pixel[1] for pixel in pixels) + max(pixel[1] for pixel in pixels)) / 2,
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


async def render_active_orders_map(
    orders: list[Order],
    *,
    cache_dir: Path,
    tile_url: str | None = None,
) -> BytesIO | None:
    """Render the current active delivery viewport as a Telegram-ready PNG."""
    points = _order_points(orders)
    if not points:
        return None

    zoom, center_x, center_y = _viewport(points)
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

    draw = ImageDraw.Draw(image, "RGBA")
    marker_font = ImageFont.load_default(size=18)
    small_font = ImageFont.load_default(size=14)
    for order, location_number, latitude, longitude in points:
        world_x, world_y = _world_pixel(latitude, longitude, zoom)
        x = round(world_x - left)
        y = round(world_y - top)
        radius = 19 if location_number == 1 else 15
        fill = "#dc2626" if location_number == 1 else "#2563eb"
        draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            fill=fill,
            outline="white",
            width=3,
        )
        label = str(order.order_number)
        font = marker_font if len(label) <= 2 else small_font
        bounds = draw.textbbox((0, 0), label, font=font)
        draw.text(
            (x - (bounds[2] - bounds[0]) / 2, y - (bounds[3] - bounds[1]) / 2 - 1),
            label,
            fill="white",
            font=font,
        )

    attribution = "© OpenStreetMap contributors"
    attribution_font = ImageFont.load_default(size=14)
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

    output = BytesIO()
    output.name = "active-deliveries.png"
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output
