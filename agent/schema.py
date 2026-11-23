"""Record schema, enum vocabulary, and validation for toolkit-research results.

Single source of truth for the enum values in ``agent/prompt.md``; every stage
(research, verify, score, report) validates through :func:`validate_record`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, TypedDict

AUTH = {"OAuth2", "API key", "Bearer token", "Basic", "JWT/signed", "Other", "None"}
ACCESS = {"self-serve-free", "self-serve-trial", "paid-plan", "approval", "partner-gated", "n/a"}
API_TYPES = {"REST", "GraphQL", "SOAP", "gRPC", "WebSocket", "SDK-only", "CLI", "None"}
BREADTH = {"broad", "moderate", "narrow", "none"}
MCP = {"official", "community", "none"}
BUILD = {"ready", "ready-with-friction", "blocked"}
CONFIDENCE = {"high", "medium", "low"}

# Tolerated spelling drift from LLM output -> canonical enum value.
_ALIASES = {
    "oauth": "OAuth2", "oauth 2.0": "OAuth2", "oauth2.0": "OAuth2",
    "api key": "API key", "apikey": "API key", "api_key": "API key",
    "bearer": "Bearer token", "token": "Bearer token", "personal access token": "Bearer token",
    "basic auth": "Basic", "basic": "Basic", "jwt": "JWT/signed",
}


class Evidence(TypedDict):
    auth: str | None
    access: str | None
    api: str | None
    mcp: str | None


class Record(TypedDict, total=False):
    id: int
    app: str
    category: str
    one_liner: str
    auth_methods: list[str]
    auth_notes: str
    access: str
    access_notes: str
    api_types: list[str]
    api_breadth: str
    api_notes: str
    mcp: str
    mcp_url: str | None
    buildability: Literal["ready", "ready-with-friction", "blocked"]
    blocker: str
    evidence: Evidence
    confidence: str
    needs_human: bool
    human_reason: str | None
    verification: list[str]  # appended by verify stages: what changed and why


def normalize_auth(methods: list[str]) -> list[str]:
    """Map loosely-spelled auth labels onto the canonical vocabulary, de-duplicated."""
    out: list[str] = []
    for m in methods:
        canon = m if m in AUTH else _ALIASES.get(m.strip().lower(), m)
        if canon not in out:
            out.append(canon)
    return out


def validate_record(rec: Record) -> list[str]:
    """Return a list of schema violations (empty list == valid)."""
    errs: list[str] = []
    for key in ("id", "app", "category", "auth_methods", "access", "api_types", "mcp", "buildability", "evidence"):
        if key not in rec:
            errs.append(f"missing {key}")
    if errs:
        return errs
    bad_auth = set(rec["auth_methods"]) - AUTH
    if bad_auth or not rec["auth_methods"]:
        errs.append(f"auth_methods invalid: {sorted(bad_auth) or 'empty'}")
    if rec["access"] not in ACCESS:
        errs.append(f"access invalid: {rec['access']!r}")
    if set(rec["api_types"]) - API_TYPES:
        errs.append(f"api_types invalid: {sorted(set(rec['api_types']) - API_TYPES)}")
    if rec.get("api_breadth") not in BREADTH:
        errs.append(f"api_breadth invalid: {rec.get('api_breadth')!r}")
    if rec["mcp"] not in MCP:
        errs.append(f"mcp invalid: {rec['mcp']!r}")
    if rec["buildability"] not in BUILD:
        errs.append(f"buildability invalid: {rec['buildability']!r}")
    if rec.get("confidence") not in CONFIDENCE:
        errs.append(f"confidence invalid: {rec.get('confidence')!r}")
    ev = rec["evidence"]
    if not isinstance(ev, dict) or not any(isinstance(v, str) and v.startswith("http") for v in ev.values()):
        errs.append("evidence has no URL")
    return errs


def load_records(path: Path) -> list[Record]:
    """Load a JSON array of records and normalize auth labels."""
    recs: list[Record] = json.loads(path.read_text(encoding="utf-8"))
    for r in recs:
        r["auth_methods"] = normalize_auth(r.get("auth_methods", []))
    return recs
