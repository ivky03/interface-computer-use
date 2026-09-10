from __future__ import annotations
import json, re
from pathlib import Path
from urllib.parse import urlparse
from .models import ActionType, RiskClass, Step


class PolicyViolation(RuntimeError):
    pass


class PolicyEngine:
    def __init__(self, config: dict):
        self.config = config

    @classmethod
    def from_file(cls, path: str | Path) -> "PolicyEngine":
        return cls(json.loads(Path(path).read_text()))

    def check_url(self, url: str) -> None:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        allowed = self.config.get("allowed_origins", [])
        if origin not in allowed:
            raise PolicyViolation(f"origin_not_allowed:{origin}")
        allowed_prefixes = self.config.get("allowed_path_prefixes", ["/"])
        if not any(parsed.path.startswith(p) for p in allowed_prefixes):
            raise PolicyViolation(f"path_not_allowed:{parsed.path}")
        allowed_fragment_routes = self.config.get("allowed_fragment_routes")
        if allowed_fragment_routes is not None and parsed.fragment:
            route = parsed.fragment.split("?", 1)[0]
            if route not in set(allowed_fragment_routes):
                raise PolicyViolation(f"route_not_allowed:{route}")

    def check_step(self, step: Step) -> str:
        allowed_actions = set(self.config.get("allowed_actions", []))
        if step.action.value not in allowed_actions:
            raise PolicyViolation(f"action_not_allowed:{step.action.value}")
        mode = self.config.get("irreversible_action_policy", "human")
        if step.risk == RiskClass.IRREVERSIBLE:
            if mode == "block":
                raise PolicyViolation("irreversible_action_blocked")
            if mode == "human":
                return "human_required"
        return "allow"


class Redactor:
    SECRET_PATTERNS = [
        re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+"),
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
        re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?"),
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ]

    def __init__(self, parameter_values: dict[str, object] | None = None):
        self.parameter_values = parameter_values or {}

    def text(self, value: object) -> str:
        s = str(value)
        for name, raw in sorted(self.parameter_values.items(), key=lambda x: len(str(x[1])), reverse=True):
            if raw is not None and str(raw):
                s = s.replace(str(raw), f"[PARAM:{name}]")
        for pat in self.SECRET_PATTERNS:
            s = pat.sub("[REDACTED]", s)
        return s
