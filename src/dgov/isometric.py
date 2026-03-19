"""Isometric terrain rendering for Kitty graphics protocol."""

from __future__ import annotations

import base64
import io
import os

from PIL import Image, ImageDraw

from dgov.terrain import ErosionModel

# Tile specification from TERRAIN-CONCEPT.md
TILE_W = 32
TILE_H = 16
Z_SCALE = 32  # 1.0 height = 32 pixels vertical offset

# Colors from TERRAIN-CONCEPT.md
PALETTE = {
    "sky_upper": "#37474F",
    "sky_dawn": "#FFCCBC",
    "bedrock": "#78909C",
    "alluvium": "#8B4513",
    "shadow": "#1A237E",
}


def _encode_kitty(img: Image.Image) -> str:
    """Encode a PIL Image to a Kitty graphics protocol escape sequence.

    Handles tmux wrapping using DCS passthrough.
    Tmux requires: ESC Ptmux; ESC <seq> ESC \\
    And ALL internal ESC characters must be doubled: ESC -> ESC ESC
    """
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64_data = base64.standard_b64encode(buf.getvalue()).decode("ascii")

    # Chunking for kitty (max 4096 bytes payload per chunk)
    chunk_size = 4096
    chunks = [b64_data[i : i + chunk_size] for i in range(0, len(b64_data), chunk_size)]

    if not chunks:
        return ""

    is_tmux = "TMUX" in os.environ
    ESC = "\x1b"

    def wrap_payload(payload: str) -> str:
        # Standard Kitty sequence
        seq = f"{ESC}{payload}{ESC}\\"
        if not is_tmux:
            return seq
        # Tmux passthrough: ESC Ptmux; <escaped_seq> ESC \
        # ESC in <escaped_seq> must be doubled
        escaped = seq.replace(ESC, f"{ESC}{ESC}")
        return f"{ESC}Ptmux;{escaped}{ESC}\\"

    out = []
    # Send first chunk with a=T (transmit and display), f=100 (PNG format)
    out.append(wrap_payload(f"_Gf=100,a=T,m={'1' if len(chunks) > 1 else '0'};{chunks[0]}"))

    # Send remaining chunks
    for i, chunk in enumerate(chunks[1:], 1):
        m = "1" if i < len(chunks) - 1 else "0"
        out.append(wrap_payload(f"_Gm={m};{chunk}"))

    return "".join(out)


def render_isometric(model: ErosionModel) -> str:
    """Render an ErosionModel using isometric projection."""
    rows = model.height_count
    cols = model.width

    if rows <= 0 or cols <= 0:
        return ""

    if not model.height or len(model.height) != rows:
        return ""

    # Calculate image size
    width = (rows + cols) * (TILE_W // 2) + 100
    height = (rows + cols) * (TILE_H // 2) + 100

    img = Image.new("RGB", (width, height), PALETTE["sky_upper"])
    draw = ImageDraw.Draw(img)

    # Offset to center
    cx, cy = width // 2, 50

    # Painter's Algorithm: Draw from back (row 0, col 0) to front
    for r in range(rows):
        for c in range(cols):
            h_val = model.height[r][c]
            z_off = int(h_val * Z_SCALE)

            sx = cx + (c - r) * (TILE_W // 2)
            sy = cy + (c + r) * (TILE_H // 2) - z_off

            points = [
                (sx, sy),  # Top
                (sx + TILE_W // 2, sy + TILE_H // 2),  # Right
                (sx, sy + TILE_H),  # Bottom
                (sx - TILE_W // 2, sy + TILE_H // 2),  # Left
            ]

            color = PALETTE["bedrock"] if h_val > 0.5 else PALETTE["alluvium"]
            draw.polygon(points, fill=color, outline="black")

            # Left face
            draw.polygon(
                [
                    (sx - TILE_W // 2, sy + TILE_H // 2),
                    (sx, sy + TILE_H),
                    (sx, sy + TILE_H + z_off),
                    (sx - TILE_W // 2, sy + TILE_H // 2 + z_off),
                ],
                fill="#37474F",
            )
            # Right face
            draw.polygon(
                [
                    (sx + TILE_W // 2, sy + TILE_H // 2),
                    (sx, sy + TILE_H),
                    (sx, sy + TILE_H + z_off),
                    (sx + TILE_W // 2, sy + TILE_H // 2 + z_off),
                ],
                fill="#546E7A",
            )

    return _encode_kitty(img)
