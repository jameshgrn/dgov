"""Isometric terrain rendering for Kitty graphics protocol."""

from __future__ import annotations

import base64
import io
import os

from PIL import Image, ImageDraw

from dgov.terrain import ErosionModel

# Tile specification from TERRAIN-CONCEPT.md
TILE_W = 16
TILE_H = 8
Z_SCALE = 8  # 1.0 height = 8 pixels vertical offset

# Colors from TERRAIN-CONCEPT.md
PALETTE = {
    "sky_upper": "#37474F",
    "sky_dawn": "#FFCCBC",
    "bedrock": "#78909C",
    "alluvium": "#8B4513",
    "shadow": "#1A237E",
}


def _wrap_kitty_payload(payload: str) -> str:
    """Wrap a Kitty graphics payload, using tmux passthrough when needed."""
    esc = "\x1b"
    seq = f"{esc}{payload}{esc}\\"
    if "TMUX" not in os.environ:
        return seq

    escaped = seq.replace(esc, f"{esc}{esc}")
    return f"{esc}Ptmux;{escaped}{esc}\\"


def _encode_kitty(img: Image.Image, cols: int, rows: int) -> str:
    """Encode a PIL Image to a Kitty graphics protocol escape sequence.

    Handles tmux wrapping using DCS passthrough.
    Tmux requires: ESC Ptmux; ESC <seq> ESC \\
    And ALL internal ESC characters must be doubled: ESC -> ESC ESC

    Adds c={cols},r={rows} to the first chunk's metadata to tell Kitty to
    scale the image to fit those terminal cell dimensions.
    """
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64_data = base64.standard_b64encode(buf.getvalue()).decode("ascii")

    # Chunking for kitty (max 4096 bytes payload per chunk)
    chunk_size = 4096
    chunks = [b64_data[i : i + chunk_size] for i in range(0, len(b64_data), chunk_size)]

    if not chunks:
        return ""

    out = []
    first_chunk = chunks[0]
    more = "1" if len(chunks) > 1 else "0"
    out.append(
        _wrap_kitty_payload(
            f"_Gf=100,a=T,c={max(cols, 1)},r={max(rows, 1)},m={more};{first_chunk}"
        )
    )

    for i, chunk in enumerate(chunks[1:], 1):
        m = "1" if i < len(chunks) - 1 else "0"
        out.append(_wrap_kitty_payload(f"_Gm={m};{chunk}"))

    return "".join(out)


def render_isometric(model: ErosionModel, cols: int, rows: int) -> str:
    """Render an ErosionModel using isometric projection."""
    model_rows = model.height_count
    model_cols = model.width

    if model_rows <= 0 or model_cols <= 0 or cols <= 0 or rows <= 0:
        return ""

    if not model.height or len(model.height) != model_rows:
        return ""

    # Calculate image size - constrained to fit terminal pane
    width = (model_rows + model_cols) * (TILE_W // 2)
    height = (model_rows + model_cols) * (TILE_H // 2) + int(2.0 * Z_SCALE)

    img = Image.new("RGB", (width, height), PALETTE["sky_upper"])
    draw = ImageDraw.Draw(img)

    # Offset to center - leave room for height visualization
    cx, cy = width // 2, int(2.0 * Z_SCALE)

    # Painter's Algorithm: Draw from back (row 0, col 0) to front
    for r in range(model_rows):
        for c in range(model_cols):
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

    return _encode_kitty(img, cols=cols, rows=rows)
