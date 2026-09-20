# Computer-Use Automation contributor rules

## Architecture invariants

- Keep `DiscoveryEngine`, `CapabilityCompiler`, and `ReplayEngine` separate.
- Domain/application code depends on the async `SurfaceDriver` protocol, never on Playwright.
- Playwright objects stay inside `computer_use.surfaces` and the live handoff boundary.
- `Condition` is the single predicate model for waits, checkpoints, business outcomes, and final success.
- The compiler is pure and deterministic. It never imports or calls an LLM client.
- Replay is deterministic and must have no import or call path to `computer_use.discovery.llm`.
- Both discovery and replay call the same `PolicyEngine` before every action.
- Resolution is semantic and strict: zero and multiple matches are distinct failures.
- Detect known business outcomes before classifying a missing checkpoint as a hard failure.
- `ReplayStatus` has exactly four terminal values: success, business_outcome, hard_failure, escalated.
- Redact before persistence. Never write credentials, browser storage state, cookies, or raw member IDs.
- Keep handoff ownership explicit; automation cannot act while a human owns the browser session.

## Validation commands

```bash
uv sync --all-extras
uv run playwright install chromium
uv run ruff format --check .
uv run ruff check .
uv run mypy src/computer_use
uv run pytest
scripts/verify_submission.sh
```

Use the smallest relevant test during implementation, then run the complete verification script.
Browser tests require the local Chromium installed by Playwright. Provider-marked tests require a real
`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`; never substitute the scripted test client for submission
evidence from genuine discovery.

## Data and evidence

- Demo records are deterministic synthetic fixtures only.
- Evidence is JSON/JSONL plus bounded observations and screenshots.
- Do not commit `.env`, `runtime/`, browser profiles, storage state, caches, or credentials.
- Generated capabilities remain drafts until the explicit approval command updates approval metadata.
