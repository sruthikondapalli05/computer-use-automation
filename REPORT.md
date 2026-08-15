# Computer-Use Automation System — Implementation Report

## 1. Architecture Overview

**Goal:** Build a system that learns how to automate UI tasks (by observing screenshots and deciding actions), records those tasks as replayable artifacts, and replays them deterministically without the LLM.

**Three-layer design:**
1. **Replay Engine** (`src/replay_engine.py`) — Reads an Artifact JSON, executes steps via Playwright, handles errors with taxonomy, verifies checkpoint
2. **Discovery Agent** (`src/discovery_agent.py`) — Claude observes screenshots, decides actions via tool-use, accumulates steps into an Artifact, auto-replays to verify before saving
3. **Safety & Escalation** (`src/safety.py`, `src/human_escalation.py`) — Allowlist enforcement, PII redaction, risk scoring, pause-for-human when high-risk actions detected

**Why this design?**
- Separation of concerns: learning (Discovery) vs. execution (Replay) vs. governance (Safety)
- Determinism guaranteed: Replay never calls LLM — only Playwright + stable locators
- Safety-first: High-risk actions pause before execution, humans decide

---

## 2. Artifact Schema & Design

**What is an Artifact?**
A JSON "recipe card" containing everything needed to replay a learned task:

```json
{
  "version": "1.0",
  "name": "login_and_add_to_cart",
  "description": "Log in with standard_user, find most expensive item, add to cart",
  "target_app": "https://www.saucedemo.com",
  "inputs": {"username": "standard_user", "password": "secret_sauce"},
  "steps": [
    {"step_number": 1, "action": "navigate", "locator": null, "description": "Navigate to Saucedemo"},
    {"step_number": 2, "action": "click", "locator": {"strategy": "id", "value": "user-name"}, "description": "Click username field"},
    {"step_number": 3, "action": "type", "locator": {...}, "text_input": "standard_user", "description": "Type username"},
    ...
  ],
  "checkpoint": {
    "element_locator": {"strategy": "class", "value": "cart_quantity"},
    "expected_content": "1",
    "description": "Verify 1 item in cart"
  },
  "outputs": {
    "item_added": "true",
    "item_name": "Sauce Labs Fleece Jacket",
    "cart_item_count": "1"
  },
  "created_at": "2026-08-13T19:25:00Z",
  "estimated_duration_seconds": 25
}
```

**Key design decisions:**
- **Locator strategy priority: `id > css > text > class > xpath`.** Each locator includes a `robustness_reason` explaining why it should survive UI changes. XPath is deliberately deprioritized to last resort rather than ranked second — it's the most brittle strategy (breaks on DOM restructuring), whereas text/class matches often survive routine UI tweaks better than a deep XPath expression. This ordering is enforced in the Discovery Agent's system prompt ([discovery_agent.py:133](src/discovery_agent.py#L133)), which is what actually constrains Claude's locator choices during learning.
- **Checkpoint**: Not just "task done" — verify a specific element contains expected content (proves task succeeded)
- **Inputs & outputs**: Parameterize the task — reuse across different login accounts, capture results for downstream automation
- **Versioning**: When Saucedemo changes, increment schema version and update replay engine

---

## 3. Determinism & Error Handling

**How do we ensure Replay always works?**

1. **Stable locators**: Every step targets an element via id > css > text > class > xpath (in order of robustness). CSS/id selectors are preferred; XPath is a last resort only when nothing else identifies the element.
2. **Retry + backoff**: On transient failures (timeout, element not visible), retry the same action for up to 3 total attempts (1 initial + 2 retries), with linear backoff between attempts (`0.5 × attempt_number` seconds — 0.5s, then 1.0s). Persistent failures abort with clear error diagnostics naming the step number and reason.
3. **Error taxonomy** — the `ErrorCategory` enum and the base `ReplayError`, plus `ExpectedOutcomeError`, `RecoverableError`, and `HardFailureError`, live in `src/error_handler.py`. Two more error types build on that same base class but live alongside the modules that raise them:
   - `ExpectedOutcomeError` (`error_handler.py`): Business logic issue (e.g., "member not found") — NOT a replay bug, expected behavior
   - `RecoverableError` (`error_handler.py`): Timeouts, transient waits — retry
   - `HardFailureError` (`error_handler.py`): Element not found after retries, locator strategy mismatch — abort with diagnostics
   - `SafetyViolation` (`src/safety.py`): Allowlist rejected a navigation — hard stop, non-retryable, non-overridable by a human, logged to the run log
   - `HumanAbortedError` (`src/human_escalation.py`): A human reviewing a high-risk action chose to abort the run — graceful stop, decision logged to the escalation audit trail

**Evidence:**
- `logs/replay_login_and_add_expensive_item_20260813T192318.json` — Successful replay, 9 steps, checkpoint verified
- `logs/replay_broken_test_20260813T192933.json` — Deliberate broken locator, 3 attempts, then hard failure with diagnostics

---

## 4. Discovery: Learning New Tasks

**How does the LLM learn tasks?**

1. **Initial screenshot**: A deterministic first `navigate` step (no LLM call needed) puts the browser on the target page; Playwright captures the screenshot
2. **Claude decides**: Sends screenshot + conversation history to Claude, forces a `decide_next_action` tool call
3. **Extract action**: Claude returns `action` (navigate/click/type/read/wait), `locator` (with strategy + value), optional `text_input`, `reasoning`, and `is_complete` boolean
4. **Execute**: The agent executes the action directly via Playwright (same locator-resolution logic as the Replay Engine), capturing success/error
5. **Feed back**: If the action failed, the error message plus a fresh screenshot go back to Claude so it can pick a different locator or approach on the next turn
6. **Accumulate**: Each successful step gets recorded to the emerging Artifact
7. **Stop**: When Claude says `is_complete=true` OR max steps reached OR an unrecoverable error occurs
8. **Auto-replay**: Immediately replay the saved Artifact using the Replay Engine to verify it actually works (Claude's say-so isn't enough) — if the replay fails, the run is reported as unsuccessful even though Claude reported completion

**Key insight:** Discovery builds an Artifact, then Replay validates it. If Replay fails, the run's overall `success` flag is `False` — only artifacts that have been proven to replay deterministically are treated as usable.

**Safety integration:** Before executing any non-navigate action, it's risk-scored:
- Risk level: `categorize_risk()` + `score_action()` determine if the action touches sensitive data; a score above the threshold (70) pauses for human approval
- Redaction: Before logging, mask credit cards, SSNs, emails, API keys, and (by field name) passwords/tokens

---

## 5. Escalation & Handoff

**When does automation pause for a human?**

1. **Risk threshold (score > 70/100)**: Typing a credit-card- or SSN-shaped value, or navigating to a genuinely external site while carrying sensitive data, cross the threshold. A bare password field on the allowed domain does *not* — the score for a routine login field lands at 65, deliberately kept just under the threshold so ordinary logins don't need a human every run, while real PII does.
2. **Pause for review**: The live human reviewer sees the unredacted action (they need the real data to judge safety); what gets *persisted* to the audit trail on disk is redacted, same as every other log in the system.
3. **Human decides**: Approve, modify (override the text/locator/action), or abort. Any unrecognized response fails closed to abort.
4. **Handoff state**: Every decision — approved, modified, or aborted — is written immediately to `logs/escalations_<run_id>.json`, along with the current URL, a screenshot path, and the variables collected so far, so a human could pick up the session manually if they chose to.
5. **Resume**: On approve/modify, automation continues with the (possibly modified) action; on abort, the discovery run stops gracefully and reports which step and reason.

**Allowlist enforcement, separately:** the target URL is checked once against the allowlist before the discovery loop starts. After every successfully executed action (not just explicit `navigate` steps), the resulting page URL is checked again — this is what catches a `click` that happens to navigate off-domain (e.g. an outbound link), which a pre-execution check alone couldn't, since the destination of a click isn't known until after it happens.

**Design:** Allowlist violations are **non-overridable** — no human decision can approve past them, they hard-stop the run. Risk escalations are **overridable** — a human can approve even a high-risk action, but every such decision is audited. This is the boundary that prevents security bypass: the human-in-the-loop path only ever applies to actions that already passed the allowlist.

---

## 6. Safety & Guardrails

**Three layers of safety:**

1. **Allowlist enforcement** (`src/safety.py`):
   - Define allowed domains/URL patterns via config; empty allowlist fails closed (permits nothing)
   - The discovery target URL is checked once up front; every action's resulting page URL is re-checked after execution
   - Blocked patterns (regex) override even allowed domains — e.g. an allowed domain's `/admin` path can still be blocked
   - Default allowlist derives from `target_app`'s own domain, so Saucedemo-focused tasks stay on Saucedemo unless a caller explicitly widens it

2. **Data redaction** (`src/safety.py`, used by both `replay_engine.py` and `discovery_agent.py`):
   - **Pattern-based**: Regex for credit cards (`\d{4}[- ]?\d{4}...`), SSNs, emails, phone numbers, API keys (`sk-`, `pk-`, `rk-`, `xox*-` prefixes)
   - **Field-name-based**: If the field name contains "password", "ssn", "credit_card", "token", etc. → redact fully, regardless of whether the value matches a regex (needed because a password like `secret_sauce` has no distinctive shape a pattern could catch)
   - **Applied to logs only**: Artifacts keep plaintext (needed for replay), but disk logs and the escalation audit trail show `***REDACTED***` / `***REDACTED_<TYPE>***`

3. **Risk scoring** (`src/safety.py`):
   - `categorize_risk()` returns a qualitative label: **LOW** (ordinary actions), **MEDIUM** (touches a sensitive field name or PII-shaped input), **HIGH** (navigating to a non-allowlisted target while carrying sensitive data)
   - `score_action()` converts that into a 0–100 number: each category has a base score (LOW=15, MEDIUM=65, HIGH=90), then additive signals apply on top — +10 per distinct PII pattern matched in the input, +30 if a navigate target isn't allowlisted, +20 for a risky keyword (delete/transfer/withdraw/wire/pay) in the locator — capped at 100
   - Actions scoring **above 70** trigger human escalation. This is tuned so a bare password field (score 65) doesn't need review every run, but a credit-card-shaped value (65 + 10 = 75) does

**Tests:** 25 tests in `test_safety.py` confirm:
- Allowlist blocks external navigations and fails closed when unconfigured
- Blocked patterns override an otherwise-allowed domain
- Redaction masks all five PII/secret pattern types, plus field-name-based redaction for shapeless secrets
- Risk scoring crosses the threshold for real credit-card-shaped data while staying under it for routine logins
- Escalation pauses, logs every decision (approve/modify/abort) to the audit trail, and fails closed on an unrecognized response
- An end-to-end discovery run (mocked Claude client, no API key needed) actually pauses mid-run on a high-risk `type` action and logs both the risk assessment and the escalation decision

---

## 7. Evidence & Validation

**Replay Engine Validation:**
- ✅ `test_replay.py` — Loads hand-authored artifact, replays all 9 steps against live Saucedemo, verifies checkpoint (cart shows "1"), outputs match expected
- ✅ Logs show multiple successful replay runs plus one deliberate-failure case (broken locator), all with correct exit codes and step-numbered diagnostics

**Discovery Agent Validation:**
- ✅ `test_discovery.py` — Live test against the real Claude API and Saucedemo (skips cleanly without `ANTHROPIC_API_KEY`); asserts an artifact is created, replay succeeds, and logs contain screenshots + step events
- ✅ Loop mechanics (execution, retry-then-replan-with-Claude, artifact assembly, redaction, auto-replay) additionally verified with a scripted fake Claude client — no API key needed to exercise this path

**Safety Validation:**
- ✅ `test_safety.py` — 25 tests covering allowlist, redaction patterns, risk scoring, and escalation flow, plus one full discovery-loop integration test
- ✅ Bug fix: the MEDIUM base risk score was raised from 50 to 65 after testing showed a credit-card-shaped input (50 + 10 = 60) never crossed the 70 review threshold — real PII now correctly triggers escalation while routine logins stay under it

---

## 8. Heterogeneity & Multi-Tenant Reuse

**How can artifacts work across different contexts (users, accounts, environments)?**

**Implemented — parameterized replay via `{{key}}` substitution.** A step's `text_input` can be a
placeholder like `"{{username}}"` instead of a literal value. `ReplayEngine.run(inputs=None)`
merges the caller-supplied `inputs` dict over the artifact's own baked-in `inputs`, then resolves
every `{{key}}` placeholder against the merged result immediately before each step executes:

```python
engine = ReplayEngine("artifacts/login_and_transfer_funds_v1.json")
engine.run(inputs={"username": "bob", "password": "..."})
```
```bash
python src/replay_engine.py artifacts/login_and_transfer_funds_v1.json \
  --inputs '{"username": "bob", "password": "..."}'
```

One recorded artifact can therefore serve multiple tenants/users without re-discovering it. A
placeholder key missing from `inputs` (or explicitly `None`) raises a `HardFailureError` naming
the missing key(s) *before* anything is typed — substitution never silently falls back to typing
the literal `{{key}}` text or an empty string, so a missing/misspelled parameter can't quietly
produce a "successful" replay that actually submitted garbage. Substitution itself is a single
pass over the original text (`re.sub` with a callback, not one `str.replace()` per key chained
over the growing result) specifically so that one input's value containing `{{other_key}}`-shaped
text can't get re-substituted on a later pass and leak into a different field. Redaction runs on
the *resolved* value, not the placeholder — `{{password}}` isn't sensitive text on its own, but
what it resolves to is — while the original template is preserved in the log
(`text_input_template`) so the evidence trail shows both what was recorded and what
actually ran. Verified with a negative control: replaying a templated artifact with a deliberately
wrong `{{password}}` value fails at the expected step, confirming substitution — not a silent
fallback to the artifact's own baked-in credentials — is what's actually happening.

**Locator stability across environments**: By prioritizing stable selectors (id > css > text >
class), artifacts are more resilient to routine styling changes or layout shifts between
environments. An `id="user-name"` selector survives a UI redesign; a deep XPath does not.

**Safety inheritance**: The artifact itself carries no allowlist — safety is enforced at replay
time by the caller, so a single artifact can be safely used in multiple trusted environments
(e.g., internal testing + production customer operations) with different allowlists, without
modifying the artifact.

**Design, not built — everything else below.** These are the honest next steps, not implemented:

- **Target app override.** `target_app` is currently a fixed field read once at artifact-save
  time; replaying the same artifact against a staging vs. production URL for the same app would
  need `ReplayEngine` to accept a `target_app` override the same way `inputs` now does. Small,
  same-shaped change — not done because no second environment exists to test it against.
- **Cross-tenant drift detection.** Two tenants running the same vendor product rarely have
  byte-identical DOMs (different branding, a moved button, an extra confirmation step). Nothing
  here detects when a locator that worked for tenant A silently stops matching on tenant B's
  variant — today that just surfaces as an ordinary replay failure, indistinguishable from the
  artifact having gone stale for everyone. A real version would track per-locator success rates
  per tenant and flag when one tenant's failure rate diverges from the rest.
- **Canonicalization of per-tenant overrides.** Rather than one artifact per tenant, a base
  artifact plus a small per-tenant override map (a handful of locators that differ, not a full
  re-record) is the shape that would actually scale to hundreds of tenants on ~20 shared apps —
  described here, not built.
- **Surface abstraction beyond the browser.** `_resolve_locator()` in both `replay_engine.py` and
  `discovery_agent.py` is Playwright-specific (CSS/id/xpath/text selectors against a DOM). The
  seam that would need to exist for a legacy frameset app or a desktop app is the same one that
  exists today between "how a step's `locator` is interpreted" and "the rest of the artifact
  schema" — `Step`, `Checkpoint`, and the error taxonomy don't know or care that Playwright is
  what's underneath. A desktop surface would implement the same `_resolve_locator` /
  `_execute_action_once` contract against an accessibility-tree API (e.g. pywinauto, AXUIElement)
  instead of a `Page`, with `Locator.strategy` gaining values like `automation_id` or
  `accessibility_label`. Nothing in the artifact schema itself is web-specific.

---

## 9. Limitations & Future Work

**What we didn't build (scope cuts):**

1. **Multi-step branching**: If an action fails mid-flow, Discovery re-plans from that point within the same run. Complex workflows with many decision trees might need multiple discovery runs to cover all branches.

2. **Visual matching**: Locators are text-based (id/css/xpath/text/class). We don't do image-based element detection — if UI changes significantly, manual artifact refresh needed.

3. **Dynamic waiting**: We retry on timeout but don't implement explicit "wait for animation to finish" — tasks that depend on loading spinners disappearing might be flaky.

4. **API integration**: No built-in support for APIs alongside UI. Artifacts assume Playwright can reach all state; workflows requiring backend calls would need custom steps.

5. **Distributed replay**: Single-machine only — no support for replaying across multiple browsers/accounts in parallel.

6. **Performance optimization**: No caching of locator lookups or screenshot diffs — each replay is a fresh pass. Could optimize for large artifact libraries.

7. **Single target_app assumption**: Both engines resolve every `navigate` step to the artifact's one `target_app`, ignoring any other URL a step might reference. This is fine for single-site tasks (Saucedemo) but would need generalizing for a multi-site workflow.

---

## 10. Conclusion

This system demonstrates a working pipeline: **Learn (Discovery) → Record (Artifact) → Verify (Replay) → Govern (Safety) → Escalate (Human)**.

The key insight is **separation of concerns**: Claude learns once, Replay runs deterministically forever, Safety & Escalation handle the messy human-in-the-loop questions. By auto-replaying every discovered task before reporting success, we ensure no unverified artifact is treated as production-ready.

For the assignment, this shows both understanding of the technical architecture (locators, retry logic, determinism) and the softer challenges (when to trust the LLM, when to pause for humans, how to keep logs safe but useful).
