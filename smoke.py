"""Quick smoke test for the caption parser (incl. clamping)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from storage import GradeError, parse_caption

for cap in ["Crack Line / V4+ / left vertical",
            "Balancy Slab V6 on middle overhang",
            "Overhang Dyno / V8+ / right slab",
            "Warmup V2/V3 left",
            "Project #1 / V7 / Cave",
            "Sick Boulders / V9 / middle overhang",    # clamped -> V8+
            "Mega Line / V12+ / right slab",           # clamped -> V8+
            "no grade here"]:
    try:
        print(f"{cap!r} -> {parse_caption(cap)}")
    except GradeError as e:
        print(f"{cap!r} -> GradeError: {e}")
