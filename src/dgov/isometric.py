"""Isometric terrain rendering for Kitty graphics protocol."""

from __future__ import annotations

import base64
import io

from PIL import Image

from dgov.terrain import ErosionModel


def _encode_kitty(img: Image.Image) -> str:
    """Encode a PIL Image to a Kitty graphics protocol escape sequence.

    See: https://sw.kovidgoyal.net/kitty/graphics-protocol/
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
    # Send first chunk with a=T (transmit and display), f=100 (PNG format)
    out.append(f"\\033_Gf=100,a=T,m={'1' if len(chunks) > 1 else '0'};{chunks[0]}\\033\\\\")

    # Send remaining chunks
    for i, chunk in enumerate(chunks[1:], 1):
        m = "1" if i < len(chunks) - 1 else "0"
        out.append(f"\\033_Gm={m};{chunk}\\033\\\\")

    return "".join(out)


def render_isometric(model: ErosionModel) -> str:
    """Render an ErosionModel to a Kitty graphics sequence."""
    # TBD: Full isometric projection.
    # For now, just render a flat 2D top-down heatmap to test the protocol.
    rows = model.height_count
    cols = model.width

    img = Image.new("RGB", (cols * 4, rows * 4), "black")
    pixels = img.load()

    if not pixels:
        return ""

    for r in range(rows):
        for c in range(cols):
            h = model.height[r][c]
            # Simple colormap: low=blue, mid=green, high=white
            val = int(max(0, min(1.0, h)) * 255)
            color = (val, val, val)

            for dr in range(4):
                for dc in range(4):
                    pixels[c * 4 + dc, r * 4 + dr] = color

    return _encode_kitty(img)
