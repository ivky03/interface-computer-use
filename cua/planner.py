from __future__ import annotations
import json, os, time
from dataclasses import dataclass
from typing import Protocol, Any
import httpx


class Planner(Protocol):
    def decide(self, goal: str, observation: dict[str, Any], history: list[dict]) -> dict[str, Any]: ...


SYSTEM = """You are a cautious computer-use planner. Given a goal and an observation of a live UI,
choose exactly ONE next action. Only use element IDs present in observation.elements. Never invent IDs.
Do not quote or repeat PII, balances, credentials, or other observed data in the reason field. Return JSON only with one of:
{"action":"click","element_id":"eN","reason":"...","risk":"safe|reversible|irreversible"}
{"action":"type","element_id":"eN","value":"...","reason":"..."}
{"action":"select","element_id":"eN","value":"...","reason":"..."}
{"action":"read","element_id":"eN","output":"name","reason":"..."}
{"action":"finish","outputs":{"key":"value"},"reason":"goal met"}
{"action":"escalate","reason":"..."}
If the goal asks to return/extract a value, use a read action on the specific output element before finish; do not merely copy it from visible_text into finish. Do not perform a final irreversible commit if the stated goal only asks to reach review/confirmation."""


@dataclass
class GeminiPlanner:
    api_key: str
    model: str = "gemini-2.5-flash"
    timeout: float = 120.0
    max_attempts: int = 3

    @classmethod
    def from_env(cls, model: str = "gemini-2.5-flash"):
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError("Set GEMINI_API_KEY (or GOOGLE_API_KEY) for a genuine live discovery run")
        timeout = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "120"))
        attempts = int(os.getenv("GEMINI_MAX_ATTEMPTS", "3"))
        return cls(key, os.getenv("GEMINI_MODEL") or model, timeout, attempts)

    def decide(self, goal: str, observation: dict[str, Any], history: list[dict]) -> dict[str, Any]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": SYSTEM + "\nGOAL:\n" + goal + "\nOBSERVATION:\n" + json.dumps(observation) + "\nRECENT HISTORY:\n" + json.dumps(history[-6:])}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
            },
        }
        timeout = httpx.Timeout(connect=10.0, read=self.timeout, write=30.0, pool=10.0)
        last_exc: Exception | None = None
        for attempt in range(1, max(1, self.max_attempts) + 1):
            try:
                r = httpx.post(
                    url,
                    json=payload,
                    headers={"x-goog-api-key": self.api_key},
                    timeout=timeout,
                )
                # Retry only transient service/rate-limit failures. Other 4xx errors are actionable.
                status = getattr(r, "status_code", 200)
                if status == 429 or 500 <= status < 600:
                    if attempt < self.max_attempts:
                        time.sleep(min(2 ** (attempt - 1), 4))
                        continue
                r.raise_for_status()
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt < self.max_attempts:
                    time.sleep(min(2 ** (attempt - 1), 4))
                    continue
                raise RuntimeError(f"Gemini planner unavailable after {attempt} attempts: {exc}") from exc
            except (KeyError, IndexError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Gemini returned an unusable planner response: {exc}") from exc
        raise RuntimeError(f"Gemini planner failed: {last_exc}")


class ScriptedPlanner:
    """Offline test planner. Explicitly not valid as the assignment's required live-LLM evidence."""
    def __init__(self, decisions: list[dict]): self.decisions=iter(decisions)
    def decide(self, goal, observation, history): return next(self.decisions)
