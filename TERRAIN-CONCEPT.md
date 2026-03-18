# dgov Isometric Terrain Concept

This document outlines the visual and technical concept for the new pixel art terrain renderer for the `dgov` governor workspace. It replaces the current Unicode/Rich-based display with a high-fidelity isometric visualization powered by the Kitty graphics protocol.

## 1. Scene Composition

The scene is divided into three distinct depth layers to create a sense of scale and atmosphere while maintaining focus on the active erosion simulation.

### ASCII Layout Sketch

```text
+-----------------------------------------------------------------------+
| SKY: Deep Blue-Gray -> Lavender -> Salmon -> Peach Horizon            |
+-----------------------------------------------------------------------+
|                                                                       |
|   BACKGROUND: Distant Valley Floor & Main River                       |
|   (Atmospheric perspective, light dithering, cooler tones)            |
|                                                                       |
|         ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~         |
|                                                                       |
|      MIDGROUND: Live SPIM Terrain (The "Arena")                       |
|      - High-relief isometric tiles                                    |
|      - Active channel incision and knickpoint migration                |
|      - Tiny agent sprites working near active channels                |
|                                                                       |
|             \_______________________________________/                 |
|                                                                       |
+-----------------------------------------------------------------------+
|                                                                       |
|   FOREGROUND: Ridgetop Campsite (The "Command Center")                |
|   - Large detailed sprites for Governor and Agent types               |
|   - Campfire at center (Warm flicker source)                          |
|   - Geological details: Stone textures, sparse vegetation              |
|                                                                       |
+-----------------------------------------------------------------------+
```

## 2. Color Palette

The palette uses a natural but dramatic range to simulate the transition from campfire warmth to the cool clarity of a mountain dawn.

| Zone | Usage | Hex Values |
| :--- | :--- | :--- |
| **Sky / Dawn** | Peach Horizon | `#FFCCBC`, `#FFA07A` |
| | Lavender Transition | `#B39DDB`, `#9575CD` |
| | Upper Sky | `#37474F`, `#263238` |
| **Foreground** | Firelight / Embers | `#FFD54F`, `#FF8F00`, `#E64A19` |
| | Ridge Stone | `#5D4037`, `#8D6E63`, `#4E342E` |
| | Vegetation | `#33691E`, `#1B5E20` |
| **Midground** | Exposed Bedrock | `#78909C`, `#546E7A`, `#37474F` |
| | Alluvial Deposits | `#8B4513`, `#A0522D`, `#D2B48C` |
| | Shadowed Channels | `#1A237E`, `#0D47A1`, `#000000` |
| **Background** | Atmospheric Hills | `#5E35B1`, `#311B92` |
| | Valley River | `#1E88E5`, `#1565C0` |

## 3. Isometric Grid Specification

Following the *Slynyrd pixelblog-41* standard for clean pixel lines:

- **Angle**: 2:1 line pattern (approx. 26.5°). For every 2 pixels horizontal, 1 pixel vertical.
- **Tile Dimensions**: 32x16 pixels (base footprint). Even dimensions ensure math simplicity for height offsets.
- **Shading**: Every tile possesses three faces (Top, Left-facing, Right-facing).
  - **Top**: Values derived directly from the SPIM heightfield and local slope.
  - **Left/Right**: Fixed value offsets to emphasize the 3D form, modulated by the dual-source lighting model.
- **Z-Scaling**: Height values from the SPIM model (0.0 to 2.0) are mapped to a vertical pixel offset (0-64 pixels) to create pronounced relief.

## 4. Agent Sprite Design Briefs

Six distinct agent types, each with a unique silhouette and personality.

| Agent | Silhouette | Personality / Style | Color Key |
| :--- | :--- | :--- | :--- |
| **pi** | Small, rounded, scrappy | Quick, efficient, nature-themed | Earthy Greens / Browns |
| **claude** | Tall, slender, upright | Deliberate, composed, precise | Royal Purples / Indigo |
| **codex** | Angular, sharp edges | Scholarly, ancient, rigid | Golden Ochre / Deep Green |
| **gemini** | Ethereal, floating forms | Luminous, fast-changing, cosmic | Pale Cyan / White / Indigo |
| **hunter** | Broad-shouldered, low center | Rugged, practical, relentless | Olive Drab / Rust |
| **governor** | Seated, wide cloak/robe | Watchful, steady, commanding | Deep Reds / Gold Trim |

## 5. Animation States

Sprites cycle through these states based on the `dgov` lifecycle:

1.  **Idle**: Sitting or standing by the foreground fire. Subtle breathing (2-3 frames).
2.  **Dispatched**: Standing up, walking toward the ridge edge (3-4 frames).
3.  **Working**: A low-resolution (8x8) version of the sprite appears in the midground at the active erosion coordinates (2 frames).
4.  **Returning**: Climbing back up the ridge face into the foreground (3-4 frames).
5.  **Done**: Sitting back down, a satisfied pose (e.g., cleaning a tool or nodding).
6.  **Failed**: Slumped pose, soot-covered or dithered "broken" effect.

## 6. Lighting Model: Dual-Source Dawn

The scene uses two primary light sources that interact across the layers:

- **The Campfire (Point Source)**:
  - Affects foreground sprites and the nearest 10% of the midground.
  - Directional amber cast. Shadows cast *away* from the center of the ridge.
  - Flickers every 2-4 frames.
- **The Dawn Sky (Directional Source)**:
  - Originates from the Upper-Right (East).
  - Cool-to-warm gradient (Lavender to Peach).
  - Highlights the Eastern faces of rills and ridges in the midground.
  - Western faces remain in deep blue/black shadow, emphasizing geomorphological relief.

## 7. Time-of-Day Progression

The lighting evolves based on the session duration (elapsed time since `dgov` launch).

| Session Hour | Sky State | Fire State | Shadow State |
| :--- | :--- | :--- | :--- |
| **0.0 - 0.5h** | Pre-dawn Deep Blue | Bright orange flicker | Long, cool shadows |
| **0.5 - 2.0h** | Peach/Salmon Dawn | Glowing embers | High-contrast "Golden Hour" |
| **2.0 - 5.0h** | Clear Pale Blue | White smoke wisp | Short, sharp midday shadows |
| **5.0 - 8.0h** | Golden/Orange Dusk | Re-lit warm glow | Long shadows stretching East |
| **8.0h+** | Deep Indigo Night | Sharp firelight focus | Only fire-lit areas visible |

## 8. SPIM Integration Notes

The visual terrain is a direct manifestation of the underlying physical model:

- **Erosion Rates**: High $K \cdot A^m \cdot S^n$ values trigger "particle" animations (small falling pixels) on tile edges to show active incision.
- **Knickpoints**: Visible as sharp steps in the longitudinal profile of channels; they migrate upstream as the simulation ticks.
- **Sediment Fans**: Areas with negative erosion (deposition) change texture from "Bedrock" (stony/grey) to "Alluvium" (sandy/brown).
- **Drainage Divides**: Visible as the high-points between midground basins; they shift laterally as one basin captures area from another.

## 9. Open Questions & Tradeoffs

- **Performance vs. Detail**: PIL/Pillow rendering is CPU-bound. We may need to cache static background layers and only re-render the active midground/sprite "dirty" rects to maintain 10 FPS.
- **Sprite Scaling**: Large foreground sprites (24x24) vs. tiny midground sprites (8x8) requires maintaining two separate asset sets.
- **Kitty Support**: While Ghostty supports Kitty graphics, we must ensure proper fallback for terminals that do not (e.g., falling back to the existing Rich/Unicode renderer).

---
**Checklist**
- [x] Scene description and ASCII layout
- [x] Color palette (Hex)
- [x] Tile dimensions and rationale
- [x] Sprite design briefs
- [x] Animation descriptions
- [x] Lighting model (Fire + Dawn)
- [x] Time-of-day progression
- [x] SPIM integration details
