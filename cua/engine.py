from __future__ import annotations
import json, time, traceback, uuid
from pathlib import Path
from urllib.parse import urlparse
from typing import Any
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from .models import *
from .policy import PolicyEngine, PolicyViolation, Redactor
from .surface import PlaywrightSurface
from .recorder import ArtifactRecorder
from .handoff import HumanHandoffManager



ROOT = Path(__file__).resolve().parents[1]

def open_entrypoint(page, entrypoint: str) -> None:
    if entrypoint.startswith("demo://legacy-bank"):
        suffix = entrypoint[len("demo://legacy-bank"):]
        html = (ROOT / "demo_app.html").read_text()
        page.set_content(html, wait_until="load")
        if suffix.startswith("#"):
            page.evaluate("h => { location.hash = h; if (window.render) window.render(); }", suffix)
        return
    page.goto(entrypoint)

def render(v: Any, params: dict[str,Any]):
    if not isinstance(v,str): return v
    for k,val in params.items(): v=v.replace("{{"+k+"}}",str(val))
    return v


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _value_matches_type(value: Any, declared: str) -> bool:
    if declared == "string":
        return isinstance(value, str)
    if declared == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if declared == "boolean":
        return isinstance(value, bool)
    if declared == "object":
        return isinstance(value, dict)
    return False


class EvidenceLogger:
    def __init__(self,path: str|Path, redactor: Redactor):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True); self.redactor=redactor
    def log(self,event: str, **data):
        def scrub(v):
            if isinstance(v, dict): return {k:scrub(x) for k,x in v.items()}
            if isinstance(v, list): return [scrub(x) for x in v]
            if isinstance(v, tuple): return [scrub(x) for x in v]
            if isinstance(v, str): return self.redactor.text(v)
            return v
        safe=scrub(data)
        with self.path.open("a") as f: f.write(json.dumps({"ts":time.time(),"event":event,**safe},default=str)+"\n")


class RuntimeDetector:
    def inspect(self, surface: PlaywrightSurface) -> tuple[str,str] | None:
        text=surface.page.locator("body").inner_text(timeout=2000)
        if "No member found" in text: return ("business","MEMBER_NOT_FOUND")
        if "Permission denied" in text: return ("hard","PERMISSION_DENIED")
        if "Session expired" in text: return ("recoverable","SESSION_EXPIRED")
        if "Application error" in text: return ("hard","APP_ERROR")
        return None

    def recover(self, code: str, surface: PlaywrightSurface) -> bool:
        if code == "SESSION_EXPIRED":
            btn=surface.page.get_by_role("button",name="Continue session",exact=True)
            if btn.count()==1:
                btn.click(); return True
        return False


class DiscoveryEngine:
    def __init__(self, planner, policy: PolicyEngine, handoff: HumanHandoffManager, evidence_dir: str|Path):
        self.planner=planner; self.policy=policy; self.handoff=handoff; self.evidence_dir=Path(evidence_dir)

    def run(self, page, goal:str, target_url:str, inputs:dict[str,Any], name="lookup_member_balance", max_steps=15):
        redactor=Redactor(inputs); log=EvidenceLogger(self.evidence_dir/"discovery.jsonl",redactor); surface=PlaywrightSurface(page)
        self.policy.check_url(target_url); open_entrypoint(page,target_url)
        recorder=ArtifactRecorder(name,goal,"legacy-bank-demo","Acme Legacy Core",target_url,inputs,[f"{urlparse(target_url).scheme}://{urlparse(target_url).netloc}"])
        history=[]; outputs={}
        for i in range(max_steps):
            if self.handoff.lease.owner != "automation": raise RuntimeError("automation attempted action without control lease")
            obs=surface.observe()
            # Evidence logs keep structure needed for debugging, not raw screen contents/PII.
            elmeta=[{k:e.get(k) for k in ("eid","tag","type","name","role","aria_label","labels") if e.get(k) not in (None,[],"")} for e in obs["elements"]]
            log.log("observe",step=i,url=obs["url"],title=obs["title"],element_count=len(elmeta),elements=elmeta)
            try:
                decision=self.planner.decide(goal,obs,history)
            except Exception as exc:
                log.log("planner_error",step=i,error=str(exc))
                req,_=self.handoff.intervene(surface,goal,f"planner error: {exc}",redactor=redactor)
                return None, RunResult(status=OutcomeKind.ESCALATED,message="planner error required human intervention",evidence={"intervention":req.request_id})
            log.log("decision",step=i,decision=decision)
            if decision["action"]=="finish":
                outputs.update(decision.get("outputs",{}))
                if target_url.startswith("demo://legacy-bank") and page.url.startswith("about:blank#"):
                    route = page.url.split("#",1)[1].split("?",1)[0]
                    cp=Checkpoint(kind="url_contains",value=f"#{route}")
                else:
                    current=urlparse(page.url)
                    cp=Checkpoint(kind="url_contains",value=current.path or "/")
                # Prefer an explicit stable completion signal for the implemented lookup capability.
                if "Savings balance" in page.locator("body").inner_text(): cp=Checkpoint(kind="text_visible",value="Savings balance")
                artifact=recorder.build(outputs,cp); log.log("finish",outputs=outputs,artifact_id=artifact.capability_id)
                return artifact, RunResult(status=OutcomeKind.SUCCESS,capability_id=artifact.capability_id,outputs=outputs,message="discovery goal completed")
            if decision["action"]=="escalate":
                self.handoff.intervene(surface,goal,decision.get("reason","planner stuck"),redactor=redactor); history.append(decision); continue
            recorder.record(decision,surface)
            target=surface.page.locator(f'[data-cua-eid="{decision.get("element_id")}"]') if decision.get("element_id") else None
            risk=RiskClass(decision.get("risk","safe")); temp_step=Step(id=f"d{i}",action=ActionType(decision["action"]),target=recorder.steps[-1].target if recorder.steps else None,risk=risk)
            try:
                gate=self.policy.check_step(temp_step)
            except PolicyViolation as exc:
                shot=surface.screenshot(self.evidence_dir/"screenshots"/f"discovery-policy-{uuid.uuid4().hex[:8]}.png")
                log.log("policy_block",step=i,error=str(exc))
                return None, RunResult(status=OutcomeKind.FAILURE,outcome_code="POLICY_BLOCK",message=str(exc),failure_class=FailureClass.POLICY,step_id=temp_step.id,evidence={"screenshot":shot})
            if gate=="human_required":
                self.handoff.intervene(surface,goal,"irreversible action requires human",step_id=temp_step.id,redactor=redactor); history.append(decision); continue
            if decision["action"]=="click": target.click(); page.wait_for_timeout(50)
            elif decision["action"]=="type": target.fill(str(decision.get("value","")))
            elif decision["action"]=="select": target.select_option(str(decision.get("value","")))
            elif decision["action"]=="read": outputs[decision["output"]]=(target.input_value() if target.evaluate("e=>['INPUT','TEXTAREA','SELECT'].includes(e.tagName)") else target.inner_text()).strip()
            history.append(decision); log.log("acted",step=i,action=decision["action"],url=page.url)
        req,_=self.handoff.intervene(surface,goal,"max_steps exceeded",redactor=redactor)
        return None, RunResult(status=OutcomeKind.ESCALATED,message="max_steps exceeded",evidence={"intervention":req.request_id})


class ReplayEngine:
    def __init__(self, policy:PolicyEngine,handoff:HumanHandoffManager,evidence_dir:str|Path,detector:RuntimeDetector|None=None):
        self.policy=policy; self.handoff=handoff; self.evidence_dir=Path(evidence_dir); self.detector=detector or RuntimeDetector()

    def _failure(self,surface,artifact,step,code,msg,cls,expected=None,observed=None):
        shot=surface.screenshot(self.evidence_dir/"screenshots"/f"failure-{uuid.uuid4().hex[:8]}.png")
        return RunResult(status=OutcomeKind.FAILURE,capability_id=artifact.capability_id,outcome_code=code,message=msg,failure_class=cls,
                         step_id=step.id if step else None,expected=expected,observed=observed,evidence={"screenshot":shot})

    def run(self,page,artifact:CapabilityArtifact,params:dict[str,Any],goal:str|None=None):
        redactor=Redactor(params); log=EvidenceLogger(self.evidence_dir/"replay.jsonl",redactor); surface=PlaywrightSurface(page)
        declared = {p.name: p for p in artifact.parameters}
        missing=[p.name for p in artifact.parameters if p.required and p.name not in params]
        if missing:
            return RunResult(status=OutcomeKind.FAILURE,capability_id=artifact.capability_id,outcome_code="INVALID_INPUT",message=f"missing params: {missing}",failure_class=FailureClass.HARD,expected="required parameters",observed=str(sorted(params)))
        unknown = sorted(set(params) - set(declared))
        if unknown:
            return RunResult(status=OutcomeKind.FAILURE,capability_id=artifact.capability_id,outcome_code="INVALID_INPUT",message=f"unknown params: {unknown}",failure_class=FailureClass.HARD,expected=str(sorted(declared)),observed=str(sorted(params)))
        bad_types = [f"{name}: expected {declared[name].type}, got {type(value).__name__}" for name,value in params.items() if not _value_matches_type(value,declared[name].type)]
        if bad_types:
            return RunResult(status=OutcomeKind.FAILURE,capability_id=artifact.capability_id,outcome_code="INVALID_INPUT",message="; ".join(bad_types),failure_class=FailureClass.HARD,expected="typed capability input contract",observed="; ".join(bad_types))
        if _origin(artifact.entrypoint) not in set(artifact.allowed_origins):
            return RunResult(status=OutcomeKind.FAILURE,capability_id=artifact.capability_id,outcome_code="ARTIFACT_POLICY_MISMATCH",message="entrypoint origin is outside artifact allowed_origins",failure_class=FailureClass.POLICY,expected=str(artifact.allowed_origins),observed=_origin(artifact.entrypoint))
        try:
            self.policy.check_url(artifact.entrypoint); open_entrypoint(page,artifact.entrypoint)
        except PolicyViolation as e:
            return RunResult(status=OutcomeKind.FAILURE,capability_id=artifact.capability_id,outcome_code="POLICY_BLOCK",message=str(e),failure_class=FailureClass.POLICY)
        outputs={}
        for step in artifact.steps:
            if self.handoff.lease.owner != "automation": return self._failure(surface,artifact,step,"CONTROL_LEASE_VIOLATION","automation does not own session",FailureClass.HARD)
            log.log("step_start",step_id=step.id,action=step.action.value,target=step.target.description if step.target else None)
            try:
                gate=self.policy.check_step(step)
                if gate=="human_required" or step.action==ActionType.HUMAN_GATE:
                    req,actions=self.handoff.intervene(surface,goal or artifact.description,"human approval/manual action required",artifact.capability_id,step.id,redactor=redactor)
                    log.log("human_handoff",step_id=step.id,request_id=req.request_id,actions=actions); continue
                recoveries = 0
                while True:
                    cond=self.detector.inspect(surface)
                    if not cond:
                        break
                    cls,code=cond
                    if cls=="business":
                        log.log("business_outcome",step_id=step.id,code=code)
                        return RunResult(status=OutcomeKind.BUSINESS_OUTCOME,capability_id=artifact.capability_id,outcome_code=code,message=code,failure_class=FailureClass.BUSINESS,step_id=step.id)
                    if cls=="recoverable" and recoveries < 2 and self.detector.recover(code,surface):
                        recoveries += 1
                        log.log("recovered",step_id=step.id,code=code,recovery_count=recoveries)
                        continue
                    return self._failure(surface,artifact,step,code,code,FailureClass.HARD,observed=str(cond))
                for attempt in range(step.retries+1):
                    loc=surface.locator(step.target) if step.target else None
                    try:
                        if step.action==ActionType.CLICK: loc.click(timeout=step.timeout_ms); page.wait_for_timeout(50)
                        elif step.action==ActionType.TYPE: loc.fill(str(render(step.value,params)),timeout=step.timeout_ms)
                        elif step.action==ActionType.SELECT: loc.select_option(str(render(step.value,params)),timeout=step.timeout_ms)
                        elif step.action==ActionType.READ:
                            outputs[step.output]=(loc.input_value() if loc.evaluate("e=>['INPUT','TEXTAREA','SELECT'].includes(e.tagName)") else loc.inner_text()).strip()
                        elif step.action==ActionType.WAIT: page.wait_for_timeout(int(step.value or 250))
                        if step.checkpoint and not surface.verify(step.checkpoint): raise RuntimeError("step checkpoint failed")
                        break
                    except (PlaywrightTimeoutError,RuntimeError):
                        if attempt>=step.retries: raise
                        page.wait_for_timeout(250*(attempt+1))
                log.log("step_ok",step_id=step.id,url=page.url)
            except PolicyViolation as e:
                return self._failure(surface,artifact,step,"POLICY_BLOCK",str(e),FailureClass.POLICY)
            except Exception as e:
                cond=self.detector.inspect(surface)
                if cond and cond[0]=="business":
                    return RunResult(status=OutcomeKind.BUSINESS_OUTCOME,capability_id=artifact.capability_id,outcome_code=cond[1],message=cond[1],failure_class=FailureClass.BUSINESS,step_id=step.id)
                return self._failure(surface,artifact,step,"STEP_FAILED",str(e),FailureClass.HARD,expected=step.target.description if step.target else step.action.value,observed=surface.page.locator("body").inner_text()[:500])
        cond=self.detector.inspect(surface)
        if cond:
            cls,code=cond
            if cls=="business": return RunResult(status=OutcomeKind.BUSINESS_OUTCOME,capability_id=artifact.capability_id,outcome_code=code,message=code,failure_class=FailureClass.BUSINESS)
            if cls=="recoverable" and self.detector.recover(code,surface): pass
            else: return self._failure(surface,artifact,None,code,code,FailureClass.HARD,expected=str(artifact.success_checkpoint),observed=str(cond))
        if not surface.verify(artifact.success_checkpoint):
            return self._failure(surface,artifact,None,"CHECKPOINT_FAILED","final success checkpoint not met",FailureClass.HARD,expected=str(artifact.success_checkpoint),observed=page.url)
        output_contract = {o.name: o for o in artifact.outputs}
        missing_outputs = [name for name in output_contract if name not in outputs]
        bad_output_types = [f"{name}: expected {spec.type}, got {type(outputs[name]).__name__}" for name,spec in output_contract.items() if name in outputs and not _value_matches_type(outputs[name],spec.type)]
        if missing_outputs or bad_output_types:
            detail = []
            if missing_outputs: detail.append(f"missing outputs: {missing_outputs}")
            if bad_output_types: detail.append("; ".join(bad_output_types))
            return self._failure(surface,artifact,None,"OUTPUT_CONTRACT_VIOLATION","; ".join(detail),FailureClass.HARD,expected=str(sorted(output_contract)),observed=str(sorted(outputs)))
        log.log("finish",outputs=outputs)
        return RunResult(status=OutcomeKind.SUCCESS,capability_id=artifact.capability_id,outputs=outputs,message="deterministic replay completed")
