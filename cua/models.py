from __future__ import annotations
from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator


class RiskClass(str, Enum):
    SAFE = "safe"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    READ = "read"
    WAIT = "wait"
    HUMAN_GATE = "human_gate"


class LocatorStrategy(BaseModel):
    kind: Literal["role", "label", "text", "name", "css", "accessibility", "visual_text", "coordinate"]
    value: str
    role: str | None = None
    exact: bool = True


class TargetRef(BaseModel):
    description: str
    strategies: list[LocatorStrategy] = Field(min_length=1)


class ParameterSpec(BaseModel):
    name: str
    type: Literal["string", "integer", "number", "boolean"]
    description: str = ""
    sensitive: bool = False
    required: bool = True


class OutputSpec(BaseModel):
    name: str
    type: Literal["string", "integer", "number", "boolean", "object"]
    description: str = ""
    sensitive: bool = False


class Checkpoint(BaseModel):
    kind: Literal["url_contains", "text_visible", "element_visible"]
    value: str | None = None
    target: TargetRef | None = None

    @model_validator(mode="after")
    def validate_shape(self):
        if self.kind == "element_visible" and not self.target:
            raise ValueError("element_visible checkpoint requires target")
        if self.kind != "element_visible" and self.value is None:
            raise ValueError(f"{self.kind} checkpoint requires value")
        return self


class Step(BaseModel):
    id: str
    action: ActionType
    target: TargetRef | None = None
    value: Any | None = None
    output: str | None = None
    risk: RiskClass = RiskClass.SAFE
    checkpoint: Checkpoint | None = None
    timeout_ms: int = 5000
    retries: int = 1
    notes: str = ""


class CapabilityArtifact(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    capability_id: str
    capability_version: str = "1.0.0"
    name: str
    description: str
    app_id: str
    vendor_product: str
    tenant_variant: str = "base"
    entrypoint: str
    allowed_origins: list[str]
    parameters: list[ParameterSpec]
    outputs: list[OutputSpec]
    steps: list[Step]
    success_checkpoint: Checkpoint
    approved_for_unattended_replay: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_contract(self):
        parameter_names = [p.name for p in self.parameters]
        output_names = [o.name for o in self.outputs]
        step_ids = [s.id for s in self.steps]
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError("parameter names must be unique")
        if len(output_names) != len(set(output_names)):
            raise ValueError("output names must be unique")
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("step ids must be unique")
        declared_outputs = set(output_names)
        for step in self.steps:
            if step.action in {ActionType.CLICK, ActionType.TYPE, ActionType.SELECT, ActionType.READ, ActionType.HUMAN_GATE} and step.target is None:
                raise ValueError(f"{step.action.value} step {step.id} requires a target")
            if step.action in {ActionType.TYPE, ActionType.SELECT} and step.value is None:
                raise ValueError(f"{step.action.value} step {step.id} requires a value")
            if step.action == ActionType.READ:
                if not step.output:
                    raise ValueError(f"read step {step.id} requires an output binding")
                if step.output not in declared_outputs:
                    raise ValueError(f"read step {step.id} references undeclared output {step.output}")
            if isinstance(step.value, str):
                import re
                refs = set(re.findall(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}", step.value))
                unknown = refs - set(parameter_names)
                if unknown:
                    raise ValueError(f"step {step.id} references unknown parameters: {sorted(unknown)}")
        return self


class OutcomeKind(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    FAILURE = "failure"
    ESCALATED = "escalated"


class FailureClass(str, Enum):
    BUSINESS = "business"
    RECOVERABLE = "recoverable"
    HARD = "hard"
    POLICY = "policy"


class RunResult(BaseModel):
    status: OutcomeKind
    capability_id: str | None = None
    outputs: dict[str, Any] = Field(default_factory=dict)
    outcome_code: str | None = None
    message: str = ""
    failure_class: FailureClass | None = None
    step_id: str | None = None
    expected: str | None = None
    observed: str | None = None
    evidence: dict[str, str] = Field(default_factory=dict)


class InterventionRequest(BaseModel):
    request_id: str
    session_id: str
    capability_id: str | None = None
    goal: str
    step_id: str | None = None
    reason: str
    current_url: str
    screenshot_path: str | None = None
    control_owner: Literal["automation", "human"] = "human"
