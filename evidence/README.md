# Evidence

- `capability.example.json`: parameterized capability produced by the discovery recorder using the test-only scripted planner.
- `discovery-SCRIPTED-not-live.jsonl`: validates the discovery/recording pipeline but **must not be represented as the assignment's required genuine LLM run**.
- `replay-success*`: deterministic replay of that capability using a different synthetic member input.
- `replay-not-found*`: expected business outcome (`MEMBER_NOT_FOUND`).
- `replay-recoverable*`: known session-expiry condition is detected, recovered, and the original action still executes (with zero action retries).
- `replay-hard-failure-result.json` + `screenshots/failure-permission.png`: permission-denied hard failure with expected/observed debug context and richer screenshot evidence.
- `capability.human-gate.example.json`, `replay-human-handoff.jsonl`, `intervention-*.json`, `human-actions.jsonl`, and `screenshots/intervention-*.png`: same-session human control-transfer path.

All records are synthetic. Artifact/log redaction is still enforced so the examples exercise the same data-handling path intended for regulated environments.

`discovery.jsonl` and `capability.json` are the genuine Gemini-driven discovery evidence required by the assignment. The older scripted trace remains explicitly labeled `SCRIPTED-not-live` so the distinction between test evidence and real model-driven evidence is unambiguous.
