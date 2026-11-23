"""Score a results file against the blind gold sample.

Usage:  python -m agent.score data/pass1/results.json [data/pass2/results.json ...]

Auth is set-valued: correct iff required ⊆ predicted ⊆ required ∪ allowed.
Single-valued fields are correct iff the prediction is in the gold accept-list
(gold lists >1 value only where the vendor docs are genuinely ambiguous).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TypedDict

from agent.schema import Record, load_records

FIELDS = ("auth", "access", "mcp", "buildability")
GOLD_PATH = Path(__file__).resolve().parent.parent / "data" / "gold.json"


class Miss(TypedDict):
    id: int
    app: str
    field: str
    predicted: object
    expected: object


class Score(TypedDict):
    n_apps: int
    per_field: dict[str, float]
    overall: float
    all_fields_right: int
    misses: list[Miss]


def field_correct(field: str, pred: Record, gold: dict[str, object]) -> bool:
    """Return True if ``pred`` matches ``gold`` for one scored field."""
    if field == "auth":
        req = set(gold["auth_required"])  # type: ignore[arg-type]
        allowed = req | set(gold["auth_allowed"])  # type: ignore[arg-type]
        got = set(pred.get("auth_methods", []))
        return req <= got <= allowed
    accept = gold[field]
    return accept is None or pred.get(field) in accept  # type: ignore[operator]


def score(results: list[Record], gold_rows: list[dict[str, object]]) -> Score:
    """Compute per-field and overall accuracy of ``results`` on the gold sample."""
    by_id = {r["id"]: r for r in results}
    hits = {f: 0 for f in FIELDS}
    misses: list[Miss] = []
    perfect = 0
    for g in gold_rows:
        pred = by_id.get(int(g["id"]))  # type: ignore[call-overload]
        if pred is None:
            raise KeyError(f"gold id {g['id']} missing from results")
        row_ok = True
        for f in FIELDS:
            if field_correct(f, pred, g):
                hits[f] += 1
            else:
                row_ok = False
                misses.append({
                    "id": pred["id"], "app": pred["app"], "field": f,
                    "predicted": pred.get("auth_methods") if f == "auth" else pred.get(f),
                    "expected": (g["auth_required"], g["auth_allowed"]) if f == "auth" else g[f],
                })
        perfect += row_ok
    n = len(gold_rows)
    per_field = {f: hits[f] / n for f in FIELDS}
    return {
        "n_apps": n,
        "per_field": per_field,
        "overall": sum(hits.values()) / (n * len(FIELDS)),
        "all_fields_right": perfect,
        "misses": misses,
    }


def main(argv: list[str]) -> int:
    gold_rows = json.loads(GOLD_PATH.read_text(encoding="utf-8"))["rows"]
    for path in argv:
        s = score(load_records(Path(path)), gold_rows)
        print(f"\n== {path}: overall {s['overall']:.1%}  "
              f"({s['all_fields_right']}/{s['n_apps']} apps fully right)")
        for f, v in s["per_field"].items():
            print(f"   {f:13s} {v:.0%}")
        for m in s["misses"]:
            print(f"   MISS #{m['id']} {m['app']}: {m['field']} got {m['predicted']} want {m['expected']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
