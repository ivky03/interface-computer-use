from __future__ import annotations
import re, uuid
from typing import Any
from .models import *
from .surface import PlaywrightSurface


def infer_type(v: Any) -> str:
    if isinstance(v,bool): return "boolean"
    if isinstance(v,int): return "integer"
    if isinstance(v,float): return "number"
    return "string"


def parameterize(value: Any, inputs: dict[str, Any]) -> Any:
    # Exact matches are parameterized even when the planner emits a JSON number/bool.
    for name, raw in sorted(inputs.items(), key=lambda item: len(str(item[1])), reverse=True):
        if value == raw:
            return "{{" + name + "}}"
    if not isinstance(value, str):
        return value
    out = value
    for name, raw in sorted(inputs.items(), key=lambda item: len(str(item[1])), reverse=True):
        if raw is not None and str(raw) and str(raw) in out:
            out = out.replace(str(raw), "{{" + name + "}}")
    return out


class ArtifactRecorder:
    def __init__(self, name: str, goal: str, app_id: str, vendor_product: str, entrypoint: str, inputs: dict[str,Any], allowed_origins: list[str]):
        self.name=name; self.goal=parameterize(goal, inputs); self.app_id=app_id; self.vendor_product=vendor_product; self.entrypoint=entrypoint
        self.inputs=inputs; self.allowed_origins=allowed_origins; self.steps=[]; self.output_values={}

    def record(self, decision: dict, surface: PlaywrightSurface):
        action=decision["action"]
        if action in {"finish","escalate"}: return
        target=surface.target_from_eid(decision["element_id"]) if decision.get("element_id") else None
        risk=RiskClass(decision.get("risk","safe"))
        if action == "read" and target is not None:
            # Never serialize the observed output value as a locator description/strategy.
            output_name = decision.get("output") or "value"
            target.description = f"output:{output_name}"
            target.strategies = [s for s in target.strategies if s.kind not in {"text"}]
            if not target.strategies:
                raise RuntimeError("read target has no non-value locator strategy")
        self.steps.append(Step(id=f"s{len(self.steps)+1:02d}", action=ActionType(action), target=target,
                               value=parameterize(decision.get("value"), self.inputs), output=decision.get("output"), risk=risk,
                               notes=""))

    def build(self, outputs: dict[str,Any], success_checkpoint: Checkpoint) -> CapabilityArtifact:
        params=[ParameterSpec(name=k,type=infer_type(v),description=f"Invocation value for {k}",sensitive=("member" in k or "account" in k)) for k,v in self.inputs.items()]
        outspec=[OutputSpec(name=k,type=infer_type(v),description=f"Value extracted as {k}",sensitive=("balance" in k or "member" in k)) for k,v in outputs.items()]
        return CapabilityArtifact(capability_id=f"cap_{uuid.uuid4().hex[:10]}",name=self.name,description=self.goal,app_id=self.app_id,
            vendor_product=self.vendor_product,entrypoint=self.entrypoint,allowed_origins=self.allowed_origins,parameters=params,outputs=outspec,
            steps=self.steps,success_checkpoint=success_checkpoint,metadata={"recording":"llm_discovery","locator_policy":"ordered semantic locator bundle; no ephemeral IDs"})
