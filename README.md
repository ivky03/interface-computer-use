# Computer-Use Automation System

A focused take-home implementation of the **record-once / replay-many** computer-use pattern described in the assignment: an LLM discovers a UI flow once, the system records a typed capability artifact, and production invocations replay that artifact deterministically without an LLM in the decision loop. The target is a deliberately old-fashioned local “bank core” UI so the demo is safe, reproducible, and independent of third-party websites.

## What is implemented

| Requirement | Implementation |
|---|---|
| Goal-driven LLM loop | `DiscoveryEngine` + `GeminiPlanner`: observe → one JSON decision → policy check → act, with max-step stopping and escalation. |
| Real UI interaction | Playwright drives the live browser surface: type, click, select, read, screenshots. |
| Structured artifact | Pydantic `CapabilityArtifact` with version, input/output contract, ordered steps, locator bundles, risk classes, and success checkpoint. |
| Deterministic replay | `ReplayEngine` executes saved steps only; no planner/model is available on the replay path. |
| Runtime outcomes | Separates business outcomes (`MEMBER_NOT_FOUND`), recoverable conditions (`SESSION_EXPIRED`), hard failures (`PERMISSION_DENIED`, step/checkpoint failures), and policy failures. |
| Safety | Configurable origin/path/action allowlist, risk classification, human gate for irreversible actions, parameter/secret redaction. |
| Evidence | JSONL event logs plus failure/intervention screenshots and structured result JSON. |
| Human handoff | Explicit control lease, same browser session, pause → human control → captured human events → resume. |
| Heterogeneity / scale | Surface-neutral artifact targeting model and vendor/variant identity; extension design is in `REPORT.md`. |

## Repository layout

```text
cua/
  models.py       # artifact/result/intervention contracts
  planner.py      # Gemini discovery planner + explicit test-only scripted planner
  surface.py      # Playwright perception, locator recording/resolution, screenshots
  policy.py       # allowlist, risk policy, redaction
  recorder.py     # converts discovery actions into parameterized artifacts
  handoff.py      # control lease + same-session human handoff
  engine.py       # discovery + deterministic replay engines
  cli.py          # discover/replay CLI
demo_app.html     # local hostile-ish legacy banking UI proxy
policy.json       # configurable guardrails
tests/            # core correctness/robustness tests
evidence/         # example artifacts, logs, screenshots, results
REPORT.md          # design write-up using the requested seven headings
```

## Setup

Python 3.11+ is recommended.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\\Scripts\\activate
pip install -e '.[dev]'
python3 -m playwright install chromium
```

For a genuine LLM discovery run, provide your own Gemini key as an environment variable. It is never written to the artifact or logs. The default model is `gemini-2.5-flash`; set `GEMINI_MODEL` to override it with a compatible Gemini model.

```bash
export GEMINI_API_KEY='...'
```

`PLAYWRIGHT_CHROMIUM=/path/to/chromium` may be set if you want to use a system Chromium instead of Playwright's bundled browser.

## Demo path

### 1. Run the required genuine LLM discovery

```bash
python3 -m cua.cli discover \
  --goal "Look up member 12345 and read their current savings balance" \
  --target 'demo://legacy-bank#home' \
  --inputs '{"member_id":"12345"}' \
  --out evidence/capability.json
```

This produces:

- `evidence/capability.json` — the reusable, parameterized capability.
- `evidence/discovery.jsonl` — the real Gemini-driven observe/decide/act trace.

The discovery UI element IDs are deliberately ephemeral. They never appear in the saved artifact; the recorder converts them to a locator bundle such as accessible label → control name → CSS fallback.

### 2. Replay the same artifact with a different invocation

```bash
python3 -m cua.cli replay \
  --artifact evidence/capability.json \
  --inputs '{"member_id":"24680"}'
```

Expected output includes:

```json
{
  "status": "success",
  "outputs": {"savings_balance": "$912.14"}
}
```

### 3. Exercise a legitimate business outcome

```bash
python3 -m cua.cli replay \
  --artifact evidence/capability.json \
  --inputs '{"member_id":"99999"}'
```

Expected contract: `status=business_outcome`, `outcome_code=MEMBER_NOT_FOUND`. This is intentionally **not** represented as a crash.

### 4. Exercise same-session human control transfer

A ready-to-run human-gated artifact is included:

```bash
python3 -m cua.cli replay \
  --headed \
  --artifact evidence/capability.human-gate.example.json \
  --inputs '{"member_id":"12345","product":"holiday","deposit":"25.00"}'
```

Automation prepares the confirmation page, then transfers the control lease to the human. Operate the **same visible browser window**, click the final commit, and press Enter in the terminal. The run records an intervention request, screenshot, human browser events, and then resumes checkpoint verification.

## Tests

```bash
python3 -m pytest -q
```

The suite currently contains **32 tests** covering the core and adversarial paths, including:

- deterministic successful replay and output extraction;
- parameterization by replaying a discovery artifact with a different member;
- strict input and output contract validation, unknown parameters, and schema-version rejection;
- expected `MEMBER_NOT_FOUND` business outcome;
- known session-expiry recovery, including recovery when a step has zero action retries;
- permission-denied hard failure with screenshot and expected/observed debug context;
- global origin/route/action policy enforcement plus the artifact's own origin contract;
- irreversible-action human handoff on the exact same page/session and fail-safe lease restoration;
- visible-only checkpoint verification and ordered locator fallback;
- sensitive parameter/output/secret redaction;
- proof that saved artifacts do not depend on discovery-only element IDs;
- proof that deterministic replay does not invoke the Gemini planner;
- discovery policy-block and planner-error escalation behavior;
- Gemini API-key transport via request header rather than URL.

## Evidence already in this repository

`evidence/` contains reproducible deterministic replay evidence, a not-found outcome, a hard-failure screenshot, and a same-session handoff example. `discovery-SCRIPTED-not-live.jsonl` is intentionally labeled as **test-only** and is not presented as the assignment's required real LLM discovery evidence.

A genuine Gemini-driven discovery run is included in `evidence/discovery.jsonl`, with its generated reusable capability in `evidence/capability.json`. That artifact was also replayed deterministically with a different synthetic member input to demonstrate parameterization and LLM-free production execution.

## Design principles

1. **The model discovers; it does not execute production capabilities.** Replay has no planner dependency and tests explicitly fail if a Gemini decision is invoked during replay.
2. **Artifacts are contracts, not transcripts.** Inputs, outputs, checkpoints, risk, and locators are explicit and reviewable.
3. **Runtime state is first-class.** Not-found is a business answer; session expiry is recoverable; permission denial is a hard stop.
4. **Semantic locators first, structural fallbacks second.** Discovery-only IDs and literal input values never become replay dependencies.
5. **Control ownership is explicit.** Human takeover changes the session's lease; automation cannot legally act while it does not own the session.
6. **Sensitive values are invocation data, not capability data.** Parameters become `{{placeholders}}`; logging applies parameter-aware and secret-pattern redaction.

## Notes on the local target

The demo surface is intentionally local and synthetic: no real credentials, PII, account records, or external service is touched. `demo://legacy-bank` is a portable logical entrypoint that the adapter resolves to the local HTML surface. This also keeps tests deterministic and avoids relying on public-site automation terms or uptime.
