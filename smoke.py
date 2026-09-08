"""Smoke test for the caption parser — real-world setter captions."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from storage import GradeError, parse_caption

cases = [
    # structured
    "Crack Line / V4+ / left vertical",
    "Overhang Dyno / V8+ / right slab",
    "Balancy Slab V6 on middle overhang",
    "Project #1 / V7 / Cave",
    # grade only — no name
    "V4",
    # dash separators
    "juggy one - V4",
    # parens + range + trailing note
    "fun route (V3-4) (dont break pls)",
    # range variants
    "Warmup V2/V3 left",
    "Beta Zone V4-5 / middle overhang",
    # clamping
    "Sick Boulders / V9 / middle overhang",
    "Mega Line / V12+ / right slab",
    # no grade
    "nice send everyone!",
]

for cap in cases:
    try:
        print(f"{cap!r}\n  -> {parse_caption(cap)}\n")
    except GradeError as e:
        print(f"{cap!r}\n  -> SKIP: {e}\n")
