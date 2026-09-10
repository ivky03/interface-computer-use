from __future__ import annotations
import json, uuid
from pathlib import Path
from typing import Callable
from .models import InterventionRequest
from .surface import PlaywrightSurface


class ControlLease:
    def __init__(self): self.owner="automation"; self.generation=0
    def transfer(self, owner:str):
        self.owner=owner; self.generation += 1


class HumanHandoffManager:
    def __init__(self, evidence_dir: str | Path, mode: str="terminal", operator_callback: Callable | None=None):
        self.evidence_dir=Path(evidence_dir); self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.mode=mode; self.operator_callback=operator_callback; self.lease=ControlLease(); self.session_id=str(uuid.uuid4())[:8]

    def intervene(self, surface: PlaywrightSurface, goal: str, reason: str, capability_id: str|None=None, step_id: str|None=None, redactor=None):
        rid=str(uuid.uuid4())[:8]; sid=self.session_id
        shot=surface.screenshot(self.evidence_dir / "screenshots" / f"intervention-{rid}.png")
        clean = (lambda x: redactor.text(x)) if redactor else (lambda x: x)
        req=InterventionRequest(request_id=rid,session_id=sid,capability_id=capability_id,goal=clean(goal),step_id=step_id,
                                reason=clean(reason),current_url=clean(surface.page.url),screenshot_path=shot)
        (self.evidence_dir / f"intervention-{rid}.json").write_text(req.model_dump_json(indent=2))
        self.lease.transfer("human"); surface.begin_human_capture()
        error = None
        actions = []
        try:
            if self.operator_callback:
                self.operator_callback(surface)
            elif self.mode == "terminal":
                print(f"\nHUMAN CONTROL REQUIRED [{rid}]\nReason: {reason}\nUse the SAME visible browser window, then press Enter here to return control.")
                input()
        except Exception as exc:
            error = exc
        finally:
            actions = surface.end_human_capture()
            with (self.evidence_dir / "human-actions.jsonl").open("a") as f:
                for a in actions:
                    f.write(json.dumps({"request_id":rid,**a})+"\n")
            # Never strand a session in human ownership because the operator surface failed.
            self.lease.transfer("automation")
        if error is not None:
            raise error
        return req, actions
