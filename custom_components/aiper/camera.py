"""Camera platform — renders the device's saved map (region polygons) as PNG.

The Aiper app fetches the same JSON we do (presigned S3 URL from
`/wr/getMapList`) and draws polygons in canvas. We do the same with PIL,
returning PNG bytes via the standard HA camera contract.

Live spray-direction (the cone you see in the app while a run is in
progress) requires MQTT shadow subscriptions — that's Phase 3. For now
the map is static between coordinator polls.
"""

from __future__ import annotations

import io
import logging
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import AiperCoordinator
from .entity import AiperEntity

_LOGGER = logging.getLogger(__name__)

CANVAS_PX = 720  # output is square; map autoscales to fit
PADDING_PX = 40
BG = (24, 27, 33, 255)         # match HA dark-theme card bg
GRID = (52, 56, 64, 255)
REGION_FILL = (76, 175, 80, 80)        # translucent green
REGION_STROKE = (76, 175, 80, 220)
REGION_FILL_ACTIVE = (33, 150, 243, 130)   # translucent blue when running
REGION_STROKE_ACTIVE = (33, 150, 243, 240)
LABEL = (220, 224, 232, 255)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AiperCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[AiperMapCamera] = []
    for sn, dev in coordinator.data.items():
        if dev.get("regions"):
            entities.append(AiperMapCamera(coordinator, sn))
    async_add_entities(entities)


class AiperMapCamera(AiperEntity, Camera):
    _attr_translation_key = "map"
    _attr_brand = "Aiper"

    def __init__(self, coordinator: AiperCoordinator, serial: str) -> None:
        AiperEntity.__init__(self, coordinator, serial)
        Camera.__init__(self)
        self._attr_unique_id = f"{serial}_map"
        self._cached_png: bytes | None = None
        self._cached_signature: tuple[Any, ...] | None = None

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        regions = self.device.get("regions") or []
        # We don't have live sample points yet (Phase 3 = MQTT). For now we
        # mark a region as "active" if its name matches the currently
        # selected zone — a small UX win until shadows land.
        active_name = self._currently_selected_region_name()
        signature = (self.device.get("map_id"), active_name, len(regions))
        if signature == self._cached_signature and self._cached_png:
            return self._cached_png

        png = await self.hass.async_add_executor_job(
            _render_map, regions, active_name
        )
        self._cached_png = png
        self._cached_signature = signature
        return png

    def _currently_selected_region_name(self) -> str | None:
        from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

        registry = er.async_get(self.hass)
        ent_id = registry.async_get_entity_id("select", DOMAIN, f"{self._serial}_region")
        if not ent_id:
            return None
        state = self.hass.states.get(ent_id)
        if state is None or state.state in (None, "unknown", "unavailable"):
            return None
        return state.state


def _collect_polygon(region: dict[str, Any]) -> list[tuple[float, float]]:
    """Pull (x, y) tuples for a region.

    Prefers `appX`/`appY` (the app's render coords), which are ~1:1 with
    what the app displays. Falls back to raw device `x`/`y` (millimetres
    relative to the device base) if appX isn't present — older firmwares
    omit it.
    """
    pts: list[tuple[float, float]] = []
    for p in region.get("points") or []:
        if not isinstance(p, dict):
            continue
        if "appX" in p and "appY" in p:
            pts.append((float(p["appX"]), float(p["appY"])))
        elif "x" in p and "y" in p:
            pts.append((float(p["x"]), float(p["y"])))
    return pts


def _render_map(
    regions: list[dict[str, Any]],
    active_name: str | None,
) -> bytes:
    """Pure-CPU PNG render of the device's map. Run in executor."""
    img = Image.new("RGBA", (CANVAS_PX, CANVAS_PX), BG)
    draw = ImageDraw.Draw(img)

    # Subtle grid so the map doesn't look unmoored
    for i in range(0, CANVAS_PX, 60):
        draw.line([(i, 0), (i, CANVAS_PX)], fill=GRID, width=1)
        draw.line([(0, i), (CANVAS_PX, i)], fill=GRID, width=1)

    polys = [_collect_polygon(r) for r in regions]
    points_only = [p for poly in polys for p in poly]
    if not points_only:
        draw.text(
            (CANVAS_PX // 2 - 80, CANVAS_PX // 2),
            "no map points yet",
            fill=LABEL,
        )
        return _to_png(img)

    min_x = min(p[0] for p in points_only)
    max_x = max(p[0] for p in points_only)
    min_y = min(p[1] for p in points_only)
    max_y = max(p[1] for p in points_only)
    span_x = max(max_x - min_x, 1.0)
    span_y = max(max_y - min_y, 1.0)
    span = max(span_x, span_y)
    scale = (CANVAS_PX - 2 * PADDING_PX) / span
    # Centre the map within the canvas
    offset_x = (CANVAS_PX - span_x * scale) / 2 - min_x * scale
    offset_y = (CANVAS_PX - span_y * scale) / 2 - min_y * scale

    def project(pt: tuple[float, float]) -> tuple[float, float]:
        return (pt[0] * scale + offset_x, pt[1] * scale + offset_y)

    try:
        font = ImageFont.load_default()
    except Exception:  # noqa: BLE001
        font = None

    for region, poly in zip(regions, polys, strict=True):
        if not poly:
            continue
        screen = [project(p) for p in poly]
        is_active = active_name is not None and region.get("name") == active_name
        fill = REGION_FILL_ACTIVE if is_active else REGION_FILL
        stroke = REGION_STROKE_ACTIVE if is_active else REGION_STROKE
        draw.polygon(screen, fill=fill, outline=stroke)
        # Re-draw the perimeter thicker so it reads at low zoom
        for i in range(len(screen)):
            a = screen[i]
            b = screen[(i + 1) % len(screen)]
            draw.line([a, b], fill=stroke, width=3)
        # Label
        cx = sum(p[0] for p in screen) / len(screen)
        cy = sum(p[1] for p in screen) / len(screen)
        label = str(region.get("name") or f"Region {region.get('id')}")
        if font is not None:
            draw.text((cx - 4 * len(label), cy - 6), label, fill=LABEL, font=font)
        else:
            draw.text((cx, cy), label, fill=LABEL)

    return _to_png(img)


def _to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()
