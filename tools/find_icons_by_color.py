"""
find_icons_by_color.py
----------------------
Scans Icons/talents/ and prints files whose dominant hue matches a target color.

Usage:
    python tools/find_icons_by_color.py purple
    python tools/find_icons_by_color.py blue
    python tools/find_icons_by_color.py red
    python tools/find_icons_by_color.py green
    python tools/find_icons_by_color.py yellow
    python tools/find_icons_by_color.py orange
    python tools/find_icons_by_color.py white
    python tools/find_icons_by_color.py grey

Requires: pip install Pillow
"""

from __future__ import annotations

import sys
from pathlib import Path

# ── Hue ranges (degrees, 0-360) ───────────────────────────────────────────────
_HUE_RANGES: dict[str, list[tuple[int, int]]] = {
    "red":    [(0, 15), (345, 360)],
    "orange": [(15, 40)],
    "yellow": [(40, 70)],
    "green":  [(70, 165)],
    "blue":   [(165, 260)],
    "purple": [(260, 320)],
    "pink":   [(320, 345)],
    "grey":   [],   # handled separately via saturation
    "gray":   [],
    "white":  [],   # handled separately via lightness
}


def _hue_matches(hue_deg: float, color: str) -> bool:
    for lo, hi in _HUE_RANGES.get(color, []):
        if lo <= hue_deg <= hi:
            return True
    return False


def dominant_color_score(path: Path, target: str) -> float:
    """Return fraction of opaque pixels whose hue matches *target* (0.0–1.0)."""
    from PIL import Image

    img = Image.open(path).convert("RGBA")
    pixels = list(img.getdata())

    total = 0
    matches = 0

    for r, g, b, a in pixels:
        if a < 30:          # skip near-transparent pixels
            continue
        total += 1

        # Convert RGB → HSV
        rf, gf, bf = r / 255, g / 255, b / 255
        cmax = max(rf, gf, bf)
        cmin = min(rf, gf, bf)
        delta = cmax - cmin
        sat = (delta / cmax) if cmax else 0
        lit = cmax

        if target in ("grey", "gray"):
            if sat < 0.15:
                matches += 1
            continue

        if target == "white":
            if lit > 0.85 and sat < 0.15:
                matches += 1
            continue

        if delta == 0:      # achromatic — no hue
            continue
        if sat < 0.20:      # too desaturated to call a color
            continue

        if cmax == rf:
            hue = 60 * (((gf - bf) / delta) % 6)
        elif cmax == gf:
            hue = 60 * (((bf - rf) / delta) + 2)
        else:
            hue = 60 * (((rf - gf) / delta) + 4)

        if _hue_matches(hue, target):
            matches += 1

    return matches / total if total else 0.0


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    target = sys.argv[1].lower()
    if target not in _HUE_RANGES:
        print(f"Unknown color '{target}'. Choose from: {', '.join(_HUE_RANGES)}")
        sys.exit(1)

    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        print("Pillow is not installed. Run:  pip install Pillow")
        sys.exit(1)

    threshold = float(sys.argv[2]) if len(sys.argv) > 2 else 0.25
    icons_dir = Path(__file__).parent.parent / "Icons" / "talents"

    print(f"Scanning {icons_dir} for icons with >{threshold*100:.0f}% {target} pixels...\n")

    results: list[tuple[float, str]] = []
    files = sorted(icons_dir.glob("*.png"))
    for i, f in enumerate(files, 1):
        if i % 100 == 0:
            print(f"  {i}/{len(files)}...", flush=True)
        score = dominant_color_score(f, target)
        if score >= threshold:
            results.append((score, f.stem))

    results.sort(reverse=True)
    print(f"\n{len(results)} icons found:\n")
    for score, stem in results:
        print(f"  {score*100:5.1f}%  {stem}")


if __name__ == "__main__":
    main()
