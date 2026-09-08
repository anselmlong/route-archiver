"""Quick smoke test for the caption parser."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from storage import GradeError, parse_caption

for cap in ["Crack Line / V4+ / left vertical",
            "Balancy Slab V6 on middle overhang",
            "Overhang Dyno / V8+ / right slab",
            "Warmup V2/V3 left",
            "Project #1 / V7 / Cave",
            "Too Hard V9 right",        # above V8+ -> rejected
            "no grade here"]:
    try:
        print(repr(cap), "->", parse_caption(cap))
    except GradeError as e:
        print(repr(cap), "-> GradeError:", e)
