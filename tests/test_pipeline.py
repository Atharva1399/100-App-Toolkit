"""Offline tests for the research pipeline: schema, scoring, link flags, research runner."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent import research
from agent.schema import normalize_auth, validate_record
from agent.score import field_correct, score
from agent.verify_links import flags_for

GOOD: dict[str, Any] = {
    "id": 1, "app": "X", "category": "CRM", "one_liner": "x",
    "auth_methods": ["OAuth2", "API key"], "access": "self-serve-free",
    "api_types": ["REST"], "api_breadth": "broad", "mcp": "official", "mcp_url": "https://x/mcp",
    "buildability": "ready", "blocker": "none",
    "evidence": {"auth": "https://x/auth", "access": "https://x/pricing", "api": "https://x/api", "mcp": "https://x/mcp"},
    "confidence": "high", "needs_human": False, "human_reason": None,
}
GOLD = {"id": 1, "auth_required": ["OAuth2"], "auth_allowed": ["API key"],
        "access": ["self-serve-free"], "mcp": ["official"], "buildability": ["ready"]}


# --- schema -----------------------------------------------------------------
def test_valid_record_has_no_errors() -> None:
    assert validate_record(GOOD) == []  # type: ignore[arg-type]


@pytest.mark.parametrize("field,value", [("access", "free"), ("mcp", "yes"), ("buildability", "maybe")])
def test_out_of_vocabulary_enums_are_rejected(field: str, value: str) -> None:
    assert any(field in e for e in validate_record({**GOOD, field: value}))  # type: ignore[arg-type]


def test_record_without_any_evidence_url_is_rejected() -> None:
    rec = {**GOOD, "evidence": {"auth": None, "access": "", "api": None, "mcp": None}}
    assert "evidence has no URL" in validate_record(rec)  # type: ignore[arg-type]


def test_auth_aliases_normalize_and_dedupe() -> None:
    assert normalize_auth(["oauth 2.0", "OAuth2", "Personal Access Token"]) == ["OAuth2", "Bearer token"]


# --- scoring ----------------------------------------------------------------
def test_auth_correct_when_required_present_and_extras_allowed() -> None:
    assert field_correct("auth", GOOD, GOLD)  # type: ignore[arg-type]


def test_auth_wrong_when_required_method_missing() -> None:
    assert not field_correct("auth", {**GOOD, "auth_methods": ["API key"]}, GOLD)  # type: ignore[arg-type]


def test_auth_wrong_when_extra_method_not_allowed() -> None:
    assert not field_correct("auth", {**GOOD, "auth_methods": ["OAuth2", "Basic"]}, GOLD)  # type: ignore[arg-type]


def test_score_counts_misses_per_field() -> None:
    s = score([{**GOOD, "access": "paid-plan"}], [GOLD])  # type: ignore[list-item]
    assert s["per_field"]["access"] == 0.0 and s["per_field"]["mcp"] == 1.0
    assert s["all_fields_right"] == 0 and s["misses"][0]["field"] == "access"


def test_score_raises_when_gold_app_missing_from_results() -> None:
    with pytest.raises(KeyError):
        score([], [GOLD])  # type: ignore[list-item]


# --- deterministic verifier -------------------------------------------------
def _ok(url: str) -> dict[str, Any]:
    return {"url": url, "status": 200, "ok": True, "error": None}


def test_flags_auth_method_missing_from_cited_page() -> None:
    checks = {"auth": _ok("u"), "mcp": _ok("m")}
    flags = flags_for(GOOD, checks, {"auth": "Use OAuth to connect", "mcp": "MCP server"})  # type: ignore[arg-type]
    assert flags == ["auth-claim-not-on-page:API key"]


def test_flags_unreachable_evidence_and_mcp_without_url() -> None:
    rec = {**GOOD, "evidence": {**GOOD["evidence"], "mcp": None}, "mcp_url": None}
    checks = {"auth": {"url": "u", "status": 403, "ok": False, "error": None}}
    flags = flags_for(rec, checks, {})  # type: ignore[arg-type]
    assert "auth-evidence-unreachable(403)" in flags and "mcp-claim-without-evidence" in flags


def test_clean_record_raises_no_flags() -> None:
    checks = {k: _ok(k) for k in ("auth", "mcp")}
    assert flags_for(GOOD, checks, {"auth": "OAuth and API key", "mcp": "our MCP"}) == []  # type: ignore[arg-type]


# --- research runner (fake client: no network, no key) ----------------------
class FakeMessages:
    """Returns queued replies; records every request for assertions."""

    def __init__(self, replies: list[tuple[str, str]]) -> None:
        self.replies, self.calls = replies, []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        stop, text = self.replies.pop(0)
        return SimpleNamespace(stop_reason=stop, stop_details=None, content=[SimpleNamespace(type="text", text=text)])


APP = {"id": 1, "app": "X", "hint": "x.com"}


def test_extract_json_handles_fenced_reply() -> None:
    assert research.extract_json('notes\n```json\n{"a": 1}\n```') == {"a": 1}


def test_research_returns_valid_record_first_try() -> None:
    fake = FakeMessages([("end_turn", json.dumps(GOOD))])
    rec = asyncio.run(research.research_app(fake, APP))
    assert validate_record(rec) == [] and len(fake.calls) == 1
    assert fake.calls[0]["tools"][0]["type"] == "web_search_20260209"


def test_research_repairs_invalid_json_once() -> None:
    fake = FakeMessages([("end_turn", json.dumps({**GOOD, "access": "free"})), ("end_turn", json.dumps(GOOD))])
    rec = asyncio.run(research.research_app(fake, APP))
    assert rec["access"] == "self-serve-free" and len(fake.calls) == 2


def test_research_fails_closed_to_needs_human() -> None:
    fake = FakeMessages([("end_turn", "no json here"), ("end_turn", "still none")])
    rec = asyncio.run(research.research_app(fake, APP))
    assert rec["needs_human"] is True and rec["buildability"] == "blocked"


def test_research_resumes_pause_turn() -> None:
    fake = FakeMessages([("pause_turn", ""), ("end_turn", json.dumps(GOOD))])
    asyncio.run(research.research_app(fake, APP))
    assert len(fake.calls) == 2


def test_run_reuses_cache_and_skips_api(tmp_path: Path) -> None:
    (tmp_path / "1.json").write_text(json.dumps(GOOD), encoding="utf-8")
    fake = FakeMessages([])
    out = asyncio.run(research.run([APP], fake, runs_dir=tmp_path))
    assert out[0]["app"] == "X" and fake.calls == []


def test_fetch_records_non_httpx_transport_errors_instead_of_crashing() -> None:
    # Regression: a TLS handshake reset surfaced as anyio.EndOfStream (not an httpx.HTTPError)
    # and aborted the whole verify run. One bad host must not kill the batch.
    import httpx

    from agent.verify_links import fetch

    class Boom(Exception):
        pass

    def handler(request: httpx.Request) -> httpx.Response:
        raise Boom("handshake reset")

    async def go() -> tuple[Any, str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch(client, asyncio.Semaphore(1), "https://example.invalid/")

    check, text = asyncio.run(go())
    assert check["ok"] is False and check["error"] == "Boom" and text == ""
