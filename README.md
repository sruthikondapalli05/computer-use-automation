# computer-use-automation

An AI system that learns UI tasks once by watching an LLM operate a browser, then replays them
deterministically forever — with no LLM in the loop at replay time. Built for environments where
the only interface is a UI with no API, like legacy banking systems. Safety matters here as much
as capability: every action is risk-scored before it runs, high-risk actions pause for a human
decision, and PII never touches disk logs unredacted (though it stays in the artifact itself,
since replay needs real data to work).

## Architecture

```
   Goal + URL
       |
       v
 ┌─────────────────┐        ┌──────────────────┐
 │ Discovery Agent  │──────▶│  Safety Layer      │
 │ (Claude + vision) │       │  allowlist          │
 │ decides 1 action   │◀─────│  risk scoring        │
 │ at a time           │      │  redaction            │
 └────────┬─────────────┘     └──────────┬────────────┘
          │ risk > 70                    │
          v                              v
 ┌──────────────────┐          ┌──────────────────┐
 │ Human Escalation  │          │   Artifact JSON   │
 │ approve/modify/    │          │  (recipe card:     │
 │ abort, audited      │          │   steps, checkpoint,│
 └──────────────────────┘         │   inputs, outputs)   │
                                   └──────────┬────────────┘
                                              │ auto-verify
                                              v
                                   ┌──────────────────┐
                                   │  Replay Engine     │
                                   │  Playwright only,   │
                                   │  NO LLM — deterministic│
                                   └──────────────────────┘
```

## Quick Start

```bash
# 1. Clone and enter the repo
git clone <repo-url> && cd computer-use-automation

# 2. Create and activate a virtual environment
python -m venv .venv && source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt
python -m playwright install chromium

# 4. Run the tests (46 pass, 1 skips without an API key; all 47 pass with one)
pytest src/ -v

# 5. Run the replay example (deterministic, no LLM, no API key)
python src/replay_engine.py artifacts/saucedemo_login_and_add_to_cart_v1.json
```

## Live Discovery (optional)

Learning a *new* task requires Claude, so it needs an API key:

```bash
cp .env.example .env          # then fill in ANTHROPIC_API_KEY

python src/discovery_agent.py https://www.saucedemo.com \
  "Log in and add the most expensive item to the cart" --headed
```

Drop `--headed` to run headless. The learned task is saved to
`artifacts/discovery_<TIMESTAMP>.json` and auto-replayed before being reported as successful.

## File Structure

```
src/
  replay_engine.py        deterministic replay — Playwright only, no LLM
  discovery_agent.py      LLM-based learning loop + CLI
  safety.py               allowlist, redaction, risk scoring
  human_escalation.py     pause-for-human, audit trail, handoff state
  error_handler.py        shared error taxonomy
  test_*.py               47 tests (46 need no API key; 1 live discovery test skips without one)
artifacts/
  saucedemo_login_and_add_to_cart_v1.json   hand-authored example
  discovery_<TIMESTAMP>.json                learned by the discovery agent, auto-verified by replay
logs/
  replay_*.json            evidence of successful/failed runs (regenerated on every run)
  escalations_*.json       audit trail of human decisions
evidence/
  discovery/               a curated real discovery run (genuine Claude API call, not mocked)
  replay/                  curated replay runs: one success, one deliberate failure
REPORT.md                  full architecture & design write-up
```

## Key Features

- ✅ Deterministic replay — no LLM at runtime, only Playwright + stable locators
- ✅ Locator priority: `id > css > text > class > xpath`
- ✅ Error taxonomy — `ExpectedOutcome`, `Recoverable`, `HardFailure`, `SafetyViolation`, `HumanAborted`
- ✅ Allowlist enforcement — fail-closed, hard-rejects off-domain navigation
- ✅ PII redaction — credit cards, SSNs, emails, phone numbers, API keys masked in logs
- ✅ Risk scoring — 0–100, actions scoring above 70 escalate to a human
- ✅ Human-in-the-loop — approve / modify / abort high-risk actions, every decision audited
- ✅ Auto-replay verification — a discovered task isn't "done" until it replays deterministically
- ✅ Comprehensive testing — 47 tests; only one (a live discovery-agent run) requires an API key, and skips cleanly without one

## Testing

```bash
pytest src/ -v                    # everything (46 pass + 1 skip without ANTHROPIC_API_KEY, 47 pass with one)
pytest src/test_replay.py -v      # replay engine, live against Saucedemo
pytest src/test_safety.py -v      # allowlist, redaction, risk scoring, escalation
pytest src/test_discovery.py -v   # full discovery loop (needs ANTHROPIC_API_KEY)
```

## Evidence

- `evidence/discovery/` — a genuine discovery run: a real Claude API call against live Saucedemo, auto-verified by replay before being saved
- `evidence/replay/replay_login_and_add_expensive_item_20260813T192318.json` — successful 9-step replay, checkpoint verified
- `evidence/replay/replay_broken_test_20260813T192933.json` — deliberate broken locator: 3 attempts, then a clean hard failure
- `logs/` and `artifacts/` also accumulate a full run of these on every `pytest`/CLI invocation — `evidence/` is the curated, stable subset kept for review
- `REPORT.md` — full architecture, design rationale, and known limitations

## For Interface AI Reviewers

1. Start with [REPORT.md](REPORT.md) for architecture and design rationale.
2. Run `pytest src/ -v` — 46 of 47 tests pass without any API key; the one exception (a live
   discovery-agent run against the real Claude API) skips cleanly rather than failing.
3. Check `evidence/` for a curated discovery run and two replay runs (one success, one
   intentional failure); `logs/` and `artifacts/` hold the same kind of evidence but regenerate
   on every run.
4. Inspect `src/safety.py` and `src/human_escalation.py` for the allowlist, redaction, and
   human-in-the-loop mechanisms — in particular, note that allowlist violations are
   non-overridable while risk escalations are, which is what keeps the safety boundary from
   having a bypass path.
