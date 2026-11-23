"""Verification loop 1 — deterministic, no LLM.

For every record, fetch each evidence URL and check that the page (a) resolves and
(b) actually contains the words its claim depends on (e.g. an auth page that is
cited for "OAuth2" must mention OAuth). Produces a per-app flag list that routes
records to loop 2 (adversarial agent) or loop 3 (browser / human).

Usage:  python -m agent.verify_links data/pass1/results.json data/pass1/link_report.json
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import TypedDict

import httpx
from bs4 import BeautifulSoup

from agent.schema import Record, load_records

TIMEOUT = httpx.Timeout(20.0, connect=10.0)
CONCURRENCY = 16
# Generic UA: many vendor docs 403 the default python UA. No personal data sent.
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; toolkit-research-verifier/1.0)"}

AUTH_PATTERNS: dict[str, re.Pattern[str]] = {
    "OAuth2": re.compile(r"oauth", re.I),
    "API key": re.compile(r"api[\s_-]?key|apikey|x-api-key", re.I),
    "Bearer token": re.compile(r"bearer|access[\s_-]?token|personal[\s_-]?access|\btoken\b", re.I),
    "Basic": re.compile(r"basic[\s_-]?auth|authorization:\s*basic|\bbasic\b", re.I),
    "JWT/signed": re.compile(r"\bjwt\b|signature|sign(ed|ing)\b|hmac", re.I),
}
MCP_PATTERN = re.compile(r"\bmcp\b|model context protocol", re.I)


class UrlCheck(TypedDict):
    url: str
    status: int | None
    ok: bool
    error: str | None


class AppFlags(TypedDict):
    id: int
    app: str
    checks: dict[str, UrlCheck]
    flags: list[str]


async def fetch(client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str) -> tuple[UrlCheck, str]:
    """GET ``url``; return its check result and visible page text ('' on failure)."""
    async with sem:
        try:
            r = await client.get(url)
            text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True) if r.status_code < 400 else ""
            return {"url": url, "status": r.status_code, "ok": r.status_code < 400, "error": None}, text
        # Broad on purpose: the transport can raise non-httpx errors (e.g. anyio.EndOfStream on a
        # TLS reset). Any per-URL failure is recorded as unreachable evidence, never crashes the batch.
        except Exception as exc:  # noqa: BLE001
            return {"url": url, "status": None, "ok": False, "error": type(exc).__name__}, ""


def flags_for(rec: Record, checks: dict[str, UrlCheck], texts: dict[str, str]) -> list[str]:
    """Derive human-readable verification flags for one record."""
    flags: list[str] = []
    for field, chk in checks.items():
        if not chk["ok"]:
            flags.append(f"{field}-evidence-unreachable({chk['status'] or chk['error']})")
    auth_text = texts.get("auth", "")
    if auth_text:
        for m in rec["auth_methods"]:
            pat = AUTH_PATTERNS.get(m)
            if pat and not pat.search(auth_text):
                flags.append(f"auth-claim-not-on-page:{m}")
    if rec["mcp"] in ("official", "community"):
        mcp_url = (rec["evidence"].get("mcp") or rec.get("mcp_url")) or ""
        if not mcp_url:
            flags.append("mcp-claim-without-evidence")
        elif texts.get("mcp") and not MCP_PATTERN.search(texts["mcp"]):
            flags.append("mcp-not-mentioned-on-evidence")
    if rec.get("needs_human"):
        flags.append("agent-asked-for-human")
    if rec.get("confidence") == "low":
        flags.append("low-confidence")
    return flags


async def run(records: list[Record]) -> list[AppFlags]:
    """Check every evidence URL across all records concurrently."""
    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
        jobs: list[tuple[int, str, asyncio.Task[tuple[UrlCheck, str]]]] = []
        for i, rec in enumerate(records):
            for field, url in rec["evidence"].items():
                if isinstance(url, str) and url.startswith("http"):
                    jobs.append((i, field, asyncio.create_task(fetch(client, sem, url))))
        await asyncio.gather(*(t for _, _, t in jobs))
    out: list[AppFlags] = []
    for i, rec in enumerate(records):
        checks: dict[str, UrlCheck] = {}
        texts: dict[str, str] = {}
        for j, field, task in jobs:
            if j == i:
                checks[field], texts[field] = task.result()
        out.append({"id": rec["id"], "app": rec["app"], "checks": checks, "flags": flags_for(rec, checks, texts)})
    return out


def main(argv: list[str]) -> int:
    src, dst = Path(argv[0]), Path(argv[1])
    report = asyncio.run(run(load_records(src)))
    dst.write_text(json.dumps(report, indent=1), encoding="utf-8")
    urls = [c for a in report for c in a["checks"].values()]
    flagged = [a for a in report if a["flags"]]
    print(f"evidence URLs: {len(urls)}  resolving: {sum(c['ok'] for c in urls)} "
          f"({sum(c['ok'] for c in urls) / max(len(urls), 1):.0%})")
    print(f"apps flagged: {len(flagged)}/{len(report)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
