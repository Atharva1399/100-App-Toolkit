"""Pass-1 research agent — headless, LLM API (Anthropic SDK) with server-side web search + fetch.

Runs ``agent/prompt.md`` once per app, validates the JSON against ``agent/schema.py``,
and retries once with the validation errors if the output is malformed. Results are
cached per app (``data/runs/<id>.json``) so a crashed or rate-limited run resumes
where it stopped instead of re-paying for finished apps (idempotent).

Usage:
    ANTHROPIC_API_KEY=... python -m agent.research data/apps.json data/pass1/results.json [--limit N]

Needs credentials resolvable by the Anthropic SDK (ANTHROPIC_API_KEY, or an
``ant auth login`` profile). Never put a key in this file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Protocol

from agent.schema import Record, normalize_auth, validate_record

ROOT = Path(__file__).resolve().parent.parent
PROMPT = (ROOT / "agent" / "prompt.md").read_text(encoding="utf-8")
RUNS = ROOT / "data" / "runs"
MODEL = "claude-opus-5-5"
CONCURRENCY = 4          # parallel apps; keep low to respect rate limits
MAX_CONTINUATIONS = 5    # brake on server-tool pause_turn loops (cost cap)
WEB_TOOLS: list[dict[str, Any]] = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 6},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 8},
]

log = logging.getLogger("research")


class MessagesClient(Protocol):
    """The slice of ``anthropic.AsyncAnthropic().beta.messages`` this module uses (injectable for tests)."""

    async def create(self, **kwargs: Any) -> Any: ...


def extract_json(text: str) -> dict[str, Any]:
    """Pull the last JSON object out of a model reply (tolerates ```json fences)."""
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced[-1] if fenced else text[text.find("{"): text.rfind("}") + 1]
    obj = json.loads(candidate)
    if not isinstance(obj, dict):
        raise ValueError("reply is not a JSON object")
    return obj


async def _call(client: MessagesClient, messages: list[dict[str, Any]]) -> Any:
    """One logical model turn, resuming server-side pause_turn up to a cap."""
    resp = None
    for _ in range(MAX_CONTINUATIONS + 1):
        resp = await client.create(
            model=MODEL,
            max_tokens=16000,
            system=PROMPT,
            messages=messages,
            tools=WEB_TOOLS,
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # server routes to another model on a safety refusal
        )
        if resp.stop_reason != "pause_turn":
            break
        messages = [*messages, {"role": "assistant", "content": resp.content}]
    if resp is None or resp.stop_reason == "refusal":
        raise RuntimeError(f"model refused or returned nothing: {getattr(resp, 'stop_details', None)}")
    return resp


async def research_app(client: MessagesClient, app: dict[str, Any]) -> Record:
    """Research one app; returns a schema-valid record or a needs_human stub."""
    ask = (f"Research this app and return ONLY the JSON object.\n"
           f"id={app['id']} app={app['app']!r} hint={app['hint']!r}")
    messages: list[dict[str, Any]] = [{"role": "user", "content": ask}]
    for attempt in range(2):
        resp = await _call(client, messages)
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        try:
            rec: Record = extract_json(text)  # type: ignore[assignment]
            rec["id"], rec["app"] = app["id"], app["app"]
            rec["auth_methods"] = normalize_auth(rec.get("auth_methods", []))
            errs = validate_record(rec)
        except (ValueError, json.JSONDecodeError) as exc:
            errs = [f"unparseable JSON: {exc}"]
        if not errs:
            return rec
        log.warning("app %s attempt %d invalid: %s", app["id"], attempt, errs)
        messages = [*messages, {"role": "assistant", "content": resp.content},
                    {"role": "user", "content": f"Your JSON failed validation: {errs}. Return the corrected JSON object only."}]
    # Fail closed: never invent a record — route to a human instead.
    return {"id": app["id"], "app": app["app"], "category": "unknown", "auth_methods": ["Other"],
            "access": "n/a", "api_types": ["None"], "api_breadth": "none", "mcp": "none",
            "buildability": "blocked", "blocker": "research failed validation",
            "evidence": {"auth": None, "access": None, "api": None, "mcp": None},
            "confidence": "low", "needs_human": True, "human_reason": f"agent output invalid: {errs}"}


async def run(apps: list[dict[str, Any]], client: MessagesClient, runs_dir: Path = RUNS) -> list[Record]:
    """Research every app concurrently, reusing cached per-app results."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(app: dict[str, Any]) -> Record:
        cache = runs_dir / f"{app['id']}.json"
        if cache.exists():
            cached: Record = json.loads(cache.read_text(encoding="utf-8"))
            return cached
        async with sem:
            rec = await research_app(client, app)
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(cache)  # atomic: a crash never leaves a half-written cache file
        log.info("done %s %s", app["id"], app["app"])
        return rec

    return list(await asyncio.gather(*(one(a) for a in apps)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("apps", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--limit", type=int, default=None, help="only the first N apps (smoke test)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='{"level":"%(levelname)s","msg":"%(message)s"}')

    import anthropic  # imported here so tests and offline stages don't need the SDK

    apps = json.loads(args.apps.read_text(encoding="utf-8"))[: args.limit]
    client = anthropic.AsyncAnthropic()
    results = asyncio.run(run(apps, client.beta.messages))
    args.out.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
    log.info("wrote %d records to %s", len(results), args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
