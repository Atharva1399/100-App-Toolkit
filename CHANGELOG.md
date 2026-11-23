# Changelog

All notable changes to this project. Format: [Keep a Changelog](https://keepachangelog.com); date-based.

## 2026-11-23

### Added
- `agent/prompt.md` research method and enum vocabulary; `agent/schema.py` validation.
- Pass-1 research over 100 apps (5 parallel agents) → `data/pass1/results.json`.
- `agent/research.py` headless LLM research agent (Anthropic SDK) (web search/fetch, JSON repair retry, fail-closed, atomic per-app cache).
- Loop 1 `agent/verify_links.py` (evidence URL + claim-on-page checks), loop 2 adversarial verifier corrections (`data/pass2/`), loop 3 browser overrides (`data/browser_overrides.json`).
- Blind 20-app gold sample (`data/gold.json`) with logged adjudication (`data/gold_adjudication.json`); `agent/score.py`.
- `agent/build.py` and `site/template.html` rendering the case-study page plus `results.json` / `results.csv`.
- GitHub Pages deploy workflow (`.github/workflows/pages.yml`) publishing `site/`.
- Author byline and footer on the page.
- 21 offline tests in `tests/test_pipeline.py`.

### Fixed
- `verify_links.fetch` crashed the whole run on a TLS reset (`anyio.EndOfStream` is not an `httpx.HTTPError`); per-URL failures are now recorded as unreachable evidence. Regression test added.
