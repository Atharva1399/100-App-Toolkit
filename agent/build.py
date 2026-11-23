"""Merge verification loops into the final dataset, compute metrics, render the case-study page.

Stages consumed (all JSON on disk, each re-runnable alone):
  data/pass1/results.json          pass-1 agent output
  data/pass2/verify*.json          loop 2: adversarial verifier corrections
  data/browser_overrides.json      loop 3: browser/human corrections (pages agents could not load)
  data/gold.json                   blind hand-labelled sample (+ data/gold_adjudication.json)
Outputs:
  data/final/results.json, data/metrics.json
  site/index.html (standalone page for GitHub Pages), site/results.json, site/results.csv
  build/artifact.html (same page as a head-less fragment for hosts that wrap it)

Usage: python -m agent.build
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from agent.schema import Record, normalize_auth, validate_record
from agent.score import FIELDS, score

ROOT = Path(__file__).resolve().parent.parent
DATA, SITE = ROOT / "data", ROOT / "site"

# Fields whose change alters a finding; everything else (notes, evidence URLs, confidence) is an evidence upgrade.
SUBSTANTIVE = {"auth_methods", "access", "api_types", "api_breadth", "mcp", "buildability"}

SEGMENTS = ["CRM", "Customer support", "Messaging & voice", "Marketing & ads", "E-commerce",
            "SEO, scraping & enrichment", "Dev & data infra", "Productivity & PM",
            "Finance & payments", "AI & niche tools"]

# Ordered: first matching bucket wins. Keyword rules keep bucketing deterministic and auditable.
BLOCKER_BUCKETS: list[tuple[str, re.Pattern[str]]] = [
    ("Not a hosted service / no public API", re.compile(r"not a hosted|no (documented )?public api|no public|unidentified|cli/mcp only|local", re.I)),
    ("Enterprise / sales / partner gate", re.compile(r"enterprise|sales|partner|contract|customer account|tenant|book a call", re.I)),
    ("App review / approval", re.compile(r"review|approv|verification|program|developer token|access level", re.I)),
    ("Paid plan required", re.compile(r"paid|subscription|plan|trial|pricing", re.I)),
    ("Narrow API", re.compile(r"narrow|limited", re.I)),
    ("Per-instance / self-hosted setup", re.compile(r"self-hosted|per-instance|per-store|per-account|instance", re.I)),
]


def bucket(blocker: str) -> str:
    """Map a free-text blocker onto one fixed bucket."""
    for name, pat in BLOCKER_BUCKETS:
        if pat.search(blocker or ""):
            return name
    return "Other"


def load_json(path: Path, default: Any) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def apply_loops(pass1: list[Record]) -> tuple[list[Record], list[dict[str, Any]]]:
    """Apply loop-2 verifier records then loop-3 browser overrides; return final records + change log."""
    final = {r["id"]: json.loads(json.dumps(r)) for r in pass1}
    changes: list[dict[str, Any]] = []
    for vf in sorted((DATA / "pass2").glob("verify[0-9].json")):
        for item in load_json(vf, []):
            new: Record = item["record"]
            new["auth_methods"] = normalize_auth(new.get("auth_methods", []))
            errs = validate_record(new)
            if errs:  # fail closed: an invalid correction is discarded, not merged
                raise ValueError(f"verifier produced invalid record {item['id']}: {errs}")
            final[item["id"]] = new
            for c in item.get("changes", []):
                changes.append({**c, "id": item["id"], "app": item["app"], "loop": "adversarial agent"})
    for ov in load_json(DATA / "browser_overrides.json", []):
        rec = final[ov["id"]]
        for field, value in ov["set"].items():
            old = rec.get(field)
            if old != value:
                changes.append({"id": ov["id"], "app": rec["app"], "field": field, "old": old, "new": value,
                                "reason": ov["reason"], "evidence_url": ov.get("evidence_url"), "loop": "browser / human"})
            rec[field] = value  # type: ignore[literal-required]
        if ov.get("evidence_url"):
            rec["evidence"][ov.get("evidence_field", "access")] = ov["evidence_url"]
        rec["needs_human"], rec["human_reason"] = ov.get("needs_human", False), ov.get("human_reason")
        if ov.get("confidence"):
            rec["confidence"] = ov["confidence"]
    out = [final[i] for i in sorted(final)]
    for r in out:
        errs = validate_record(r)
        if errs:
            raise ValueError(f"final record {r['id']} invalid: {errs}")
    return out, changes


def fmt(v: Any) -> str:
    return ", ".join(v) if isinstance(v, list) else str(v)


def main() -> int:
    pass1: list[Record] = load_json(DATA / "pass1" / "results.json", [])
    for r in pass1:
        r["auth_methods"] = normalize_auth(r["auth_methods"])
    final, changes = apply_loops(pass1)
    for i, r in enumerate(final):
        r["segment"] = SEGMENTS[i // 10]  # type: ignore[typeddict-unknown-key]
        r["blocker_bucket"] = bucket(r["blocker"]) if r["buildability"] != "ready" else "none"  # type: ignore[typeddict-unknown-key]

    gold = load_json(DATA / "gold.json", {})["rows"]
    s1, s2 = score(pass1, gold), score(final, gold)
    s1_raw_path = DATA / "pass1" / "score_vs_initial_gold.json"
    s1_raw = load_json(s1_raw_path, None)
    p2_by_id = {r["id"]: r for r in final}
    p1_by_id = {r["id"]: r for r in pass1}
    gold_by_id = {g["id"]: g for g in gold}
    miss_keys = {(m["id"], m["field"]) for m in s1["misses"] + s2["misses"]}
    misses = []
    for i, f in sorted(miss_keys):
        g = gold_by_id[i]
        get = (lambda r: fmt(r["auth_methods"])) if f == "auth" else (lambda r, f=f: r[f])
        truth = f"needs {fmt(g['auth_required'])} (may add {fmt(g['auth_allowed'])})" if f == "auth" else " or ".join(g[f])
        fixed = (i, f) not in {(m["id"], m["field"]) for m in s2["misses"]}
        by = next((c["loop"] for c in changes if c["id"] == i and c["field"] in (f, "auth_methods" if f == "auth" else f)), None)
        misses.append({"app": p1_by_id[i]["app"], "field": f, "p1": get(p1_by_id[i]), "final": get(p2_by_id[i]),
                       "truth": truth, "fixed_by": (by or "loop") if fixed else "NOT FIXED"})

    link1 = load_json(DATA / "pass1" / "link_report.json", [])
    link2 = load_json(DATA / "final" / "link_report.json", [])
    urls = lambda rep: [c for a in rep for c in a["checks"].values()]
    stats: dict[str, Any] = {
        "n": len(final),
        "ready": sum(r["buildability"] == "ready" for r in final),
        "friction": sum(r["buildability"] == "ready-with-friction" for r in final),
        "blocked": sum(r["buildability"] == "blocked" for r in final),
        "selfserve": sum(r["access"] in ("self-serve-free", "self-serve-trial") for r in final),
        "free": sum(r["access"] == "self-serve-free" for r in final),
        "gated": sum(r["access"] in ("partner-gated", "approval") for r in final),
        "oauth": sum("OAuth2" in r["auth_methods"] for r in final),
        "static": sum(bool({"API key", "Bearer token", "Basic"} & set(r["auth_methods"])) for r in final),
        "both": sum("OAuth2" in r["auth_methods"] and bool({"API key", "Bearer token", "Basic"} & set(r["auth_methods"])) for r in final),
        "oauth_only": sum(r["auth_methods"] == ["OAuth2"] for r in final),
        "mcp_official": sum(r["mcp"] == "official" for r in final),
        "mcp_none": sum(r["mcp"] == "none" for r in final),
        "needs_human": sum(bool(r.get("needs_human")) for r in final),
        "acc1": round(s1["overall"] * 100, 1), "acc2": round(s2["overall"] * 100, 1),
        "acc1_raw": round(s1_raw["overall"] * 100, 1) if s1_raw else None,
        "perfect1": s1["all_fields_right"], "perfect2": s2["all_fields_right"],
        "flagged1": sum(bool(a["flags"]) for a in link1), "flagged2": sum(bool(a["flags"]) for a in link2) if link2 else None,
        "urls1_ok": sum(c["ok"] for c in urls(link1)), "urls1": len(urls(link1)),
        "urls2_ok": sum(c["ok"] for c in urls(link2)), "urls2": len(urls(link2)),
        "changes": len(changes), "changed_apps": len({c["id"] for c in changes}),
        "changes_agent": sum(c["loop"] == "adversarial agent" and c["field"] in SUBSTANTIVE for c in changes),
        "changes_browser": sum(c["loop"] == "browser / human" and c["field"] in SUBSTANTIVE for c in changes),
        "browser_checked": len(load_json(DATA / "browser_overrides.json", [])),
        "evidence_upgrades": sum(c["field"] not in SUBSTANTIVE for c in changes),
        "substantive_apps": len({c["id"] for c in changes if c["field"] in SUBSTANTIVE}),
        "low1": sum(r.get("confidence") == "low" for r in pass1), "low2": sum(r.get("confidence") == "low" for r in final),
        "nh1": sum(bool(r.get("needs_human")) for r in pass1),
    }
    nonready = [r for r in final if r["buildability"] != "ready"]
    stats["nonready"] = len(nonready)
    stats["human_gate"] = sum(r["blocker_bucket"] in ("Enterprise / sales / partner gate", "App review / approval") for r in nonready)  # type: ignore[typeddict-item]
    stats["paid_gate"] = sum(r["blocker_bucket"] == "Paid plan required" for r in nonready)  # type: ignore[typeddict-item]
    stats["no_api"] = sum(r["blocker_bucket"] == "Not a hosted service / no public API" for r in nonready)  # type: ignore[typeddict-item]
    stats["support_trial"] = sum(r["access"] == "self-serve-trial" for r in final[10:20])
    stats["ai_paid"] = sum(r["access"] == "paid-plan" for r in final[90:100])
    stats["devprod_free"] = sum(r["access"] == "self-serve-free" for r in final[60:80])
    seg_info = []
    for s in SEGMENTS:
        rows = [r for r in final if r["segment"] == s]  # type: ignore[typeddict-item]
        bc = Counter(r["blocker_bucket"] for r in rows if r["blocker_bucket"] != "none")  # type: ignore[typeddict-item]
        seg_info.append({"name": s, "blocker": f"{bc.most_common(1)[0][0]} ({bc.most_common(1)[0][1]})" if bc else "none"})
        stats[f"seg_ready::{s}"] = sum(r["buildability"] == "ready" for r in rows)

    insights = load_json(DATA / "insights.json", {})
    def fill(o: Any) -> Any:
        if isinstance(o, str):
            return o.format(**{k.replace("::", "_").replace(" ", "_").replace("&", "and").replace(",", ""): v for k, v in stats.items()})
        if isinstance(o, list):
            return [fill(x) for x in o]
        if isinstance(o, dict):
            return {k: fill(v) for k, v in o.items()}
        return o
    insights = fill(insights)
    # queues are derived from data, never hand-typed
    q_easy = [r["app"] for r in final if r["buildability"] == "ready" and r["access"] == "self-serve-free"]
    q_trial = [r["app"] for r in final if r["buildability"] == "ready" and r["access"] != "self-serve-free"]
    q_out = [r["app"] for r in final if r["access"] in ("partner-gated", "approval")]
    q_skip = [r["app"] for r in final if r["access"] == "n/a" or r["blocker_bucket"] == "Not a hosted service / no public API"]  # type: ignore[typeddict-item]
    insights["queue"] = [
        {"title": "Build now: free self-serve credentials", "cls": "ok", "items": q_easy, "note": "Ready verdict and a free tier/sandbox. A toolkit engineer can start today with no outside help."},
        {"title": "Build now, budget a trial or plan", "cls": "ok", "items": q_trial, "note": "Ready, but test credentials come from a trial or a cheap paid plan. Ops cost, not a blocker."},
        {"title": "Needs outreach: approval or partnership", "cls": "warn", "items": q_out, "note": "App review, developer-token approval, partner program or contact-sales. Start BD/partner conversations in parallel."},
        {"title": "Not a toolkit target (yet)", "cls": "bad", "items": q_skip, "note": "No hosted API or product could not be identified. Local CLIs could ship as local tools instead."},
    ]

    gold_fixes = load_json(DATA / "gold_adjudication.json", [])
    page_data = {
        "results": final, "segments": seg_info, "insights": insights, "stats": stats,
        "auth_counts": Counter(m for r in final for m in r["auth_methods"]).most_common(),
        "blocker_buckets": Counter(r["blocker_bucket"] for r in final if r["blocker_bucket"] != "none").most_common(),  # type: ignore[typeddict-item]
        "pipeline": insights.pop("pipeline", []),
        "run": insights.pop("run", {}),
        "verification": {
            "lede": insights.pop("ver_lede", ""), "acc_note": insights.pop("acc_note", ""),
            "funnel": insights.pop("funnel", []),
            "fields": [{"field": f, "p1": s1["per_field"][f], "p2": s2["per_field"][f]} for f in FIELDS],
            "gold_fixes": gold_fixes, "misses": misses,
            "changes": [{k: (fmt(v) if k in ("old", "new") else v) for k, v in c.items()}
                        for c in changes if c["field"] in SUBSTANTIVE],
        },
    }
    (DATA / "final").mkdir(exist_ok=True)
    (DATA / "final" / "results.json").write_text(json.dumps(final, indent=1, ensure_ascii=False), encoding="utf-8")
    (DATA / "metrics.json").write_text(json.dumps({"stats": stats, "score_pass1": s1, "score_final": s2}, indent=1), encoding="utf-8")
    SITE.mkdir(exist_ok=True)
    (SITE / "results.json").write_text(json.dumps(final, indent=1, ensure_ascii=False), encoding="utf-8")
    cols = ["id", "app", "segment", "category", "one_liner", "auth_methods", "access", "api_types", "api_breadth",
            "mcp", "mcp_url", "buildability", "blocker", "confidence", "needs_human", "evidence_auth", "evidence_access", "evidence_api", "evidence_mcp"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in final:
        w.writerow([fmt(r.get(c)) if not c.startswith("evidence_") else (r["evidence"].get(c[9:]) or "") for c in cols])
    (SITE / "results.csv").write_text(buf.getvalue(), encoding="utf-8")
    blob = json.dumps(page_data, ensure_ascii=False).replace("</", "<\\/")  # keep </script> out of the JSON island
    fragment = (ROOT / "templates" / "page.html").read_text(encoding="utf-8").replace("__DATA__", blob)
    # Fragment (no <html>/<head>) for hosts that wrap the page in their own document shell.
    (ROOT / "build").mkdir(exist_ok=True)
    (ROOT / "build" / "artifact.html").write_text(fragment, encoding="utf-8")
    # Full standalone document for GitHub Pages / any static host.
    head, body = fragment.split("</style>", 1)
    nl = chr(10)
    doc = nl.join([
        "<!doctype html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">',
        head.replace('<meta name="claude:viewport-fit-sides" content="cover">' + nl, "") + "</style>", "</head>", "<body>", body, "</body>", "</html>", "",
    ])
    (SITE / "index.html").write_text(doc, encoding="utf-8")
    print(json.dumps({k: v for k, v in stats.items() if "::" not in k}, indent=0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
