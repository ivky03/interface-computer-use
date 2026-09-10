from __future__ import annotations
import json
import os
from pathlib import Path
import pytest
from playwright.sync_api import sync_playwright
from cua.models import *
from cua.policy import PolicyEngine, PolicyViolation, Redactor
from cua.surface import PlaywrightSurface
from cua.engine import ReplayEngine, DiscoveryEngine
from cua.handoff import HumanHandoffManager
from cua.planner import ScriptedPlanner

ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture()
def page():
    with sync_playwright() as p:
        # Prefer an explicit override when supplied (useful in CI/containers).
        # Otherwise use a known Linux system Chromium only if it actually exists;
        # on macOS/Windows Playwright falls back to its bundled browser.
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM")
        if not executable and Path("/usr/bin/chromium").exists():
            executable = "/usr/bin/chromium"
        launch_kwargs = {"headless": True}
        if executable:
            launch_kwargs["executable_path"] = executable
        b = p.chromium.launch(**launch_kwargs)
        pg = b.new_page()
        yield pg
        b.close()

def target(desc,*strategies): return TargetRef(description=desc,strategies=list(strategies))
def role(role_,name): return LocatorStrategy(kind="role",role=role_,value=name)
def name(n): return LocatorStrategy(kind="name",value=n)

def artifact(entry="demo://legacy-bank#home"):
    return CapabilityArtifact(capability_id="cap_test",name="lookup_member_balance",description="Look up a member and return savings balance",app_id="legacy-bank-demo",vendor_product="Acme Legacy Core",entrypoint=entry,allowed_origins=["demo://legacy-bank"],
      parameters=[ParameterSpec(name="member_id",type="string",sensitive=True)],outputs=[OutputSpec(name="savings_balance",type="string",sensitive=True)],
      steps=[
        Step(id="s01",action=ActionType.TYPE,target=target("Member Number",LocatorStrategy(kind="label",value="Member Number"),name("member")),value="{{member_id}}"),
        Step(id="s02",action=ActionType.CLICK,target=target("Find Member",role("button","Find Member"))),
        Step(id="s03",action=ActionType.READ,target=target("Savings balance",LocatorStrategy(kind="css",value="span.balance")),output="savings_balance")],
      success_checkpoint=Checkpoint(kind="text_visible",value="Savings balance"))

def engine(tmp_path, callback=None):
    return ReplayEngine(PolicyEngine.from_file(ROOT/"policy.json"),HumanHandoffManager(tmp_path,mode="terminal",operator_callback=callback),tmp_path)

def test_deterministic_replay_success(page,tmp_path):
    r=engine(tmp_path).run(page,artifact(),{"member_id":"12345"})
    assert r.status==OutcomeKind.SUCCESS and r.outputs["savings_balance"]=="$4,281.73"

def test_business_outcome_not_found(page,tmp_path):
    r=engine(tmp_path).run(page,artifact(),{"member_id":"99999"})
    assert r.status==OutcomeKind.BUSINESS_OUTCOME and r.outcome_code=="MEMBER_NOT_FOUND" and r.failure_class==FailureClass.BUSINESS

def test_recoverable_session_expiry(page,tmp_path):
    a=artifact("demo://legacy-bank#member?member=12345&scenario=expired")
    a.steps=[Step(id="s01",action=ActionType.READ,target=target("Savings balance",LocatorStrategy(kind="css",value="span.balance")),output="savings_balance")]
    r=engine(tmp_path).run(page,a,{"member_id":"12345"})
    assert r.status==OutcomeKind.SUCCESS and r.outputs["savings_balance"]=="$4,281.73"
    assert "recovered" in (tmp_path/"replay.jsonl").read_text()

def test_hard_failure_captures_screenshot(page,tmp_path):
    a=artifact("demo://legacy-bank#member?member=12345&scenario=permission"); a.steps=[]
    r=engine(tmp_path).run(page,a,{"member_id":"12345"})
    assert r.status==OutcomeKind.FAILURE and r.outcome_code=="PERMISSION_DENIED" and Path(r.evidence["screenshot"]).exists()

def test_policy_blocks_disallowed_origin(page,tmp_path):
    a=artifact("https://example.com/")
    r=engine(tmp_path).run(page,a,{"member_id":"12345"})
    assert r.status==OutcomeKind.FAILURE and r.failure_class==FailureClass.POLICY

def test_irreversible_step_handoff_same_session(page,tmp_path):
    def human(surface):
        assert "#review" in surface.page.url
        surface.page.get_by_role("button",name="Submit and create account").click()
    a=artifact("demo://legacy-bank#subaccount?member=12345")
    a.outputs=[]
    a.steps=[
      Step(id="s01",action=ActionType.CLICK,target=target("Review account",role("button","Review account"))),
      Step(id="s02",action=ActionType.HUMAN_GATE,target=target("Submit and create account",role("button","Submit and create account")),risk=RiskClass.IRREVERSIBLE)]
    a.success_checkpoint=Checkpoint(kind="text_visible",value="Account created")
    r=engine(tmp_path,human).run(page,a,{"member_id":"12345"})
    assert r.status==OutcomeKind.SUCCESS
    assert list(tmp_path.glob("intervention-*.json"))

def test_redaction_parameter_and_secret():
    red=Redactor({"member_id":"12345"})
    s=red.text("member 12345 api_key=abcdef password=hunter2")
    assert "12345" not in s and "abcdef" not in s and "hunter2" not in s

def test_artifact_has_no_concrete_sensitive_parameter():
    raw=artifact().model_dump_json()
    assert "12345" not in raw and "{{member_id}}" in raw


def test_discovery_records_replayable_artifact(page,tmp_path):
    planner=ScriptedPlanner([
      {"action":"type","element_id":"e1","value":"12345","reason":"enter member"},
      {"action":"click","element_id":"e2","reason":"search"},
      {"action":"read","element_id":"e1","output":"savings_balance","reason":"capture output"},
      {"action":"finish","outputs":{},"reason":"goal met"}
    ])
    pol=PolicyEngine.from_file(ROOT/"policy.json"); hand=HumanHandoffManager(tmp_path,operator_callback=lambda s: None)
    art,res=DiscoveryEngine(planner,pol,hand,tmp_path).run(page,"Look up member 12345 and read savings balance","demo://legacy-bank#home",{"member_id":"12345"})
    assert res.status==OutcomeKind.SUCCESS
    assert art.steps[0].value=="{{member_id}}"
    assert any(st.action==ActionType.READ and st.output=="savings_balance" for st in art.steps)
    # The artifact is independent of discovery-only ephemeral IDs.
    assert "data-cua-eid" not in art.model_dump_json()
    assert "$4,281.73" not in art.model_dump_json()
    r=ReplayEngine(pol,hand,tmp_path).run(page,art,{"member_id":"24680"})
    assert r.status==OutcomeKind.SUCCESS and r.outputs["savings_balance"]=="$912.14"


def test_rejects_wrong_parameter_type(page,tmp_path):
    r=engine(tmp_path).run(page,artifact(),{"member_id":12345})
    assert r.status==OutcomeKind.FAILURE and r.outcome_code=="INVALID_INPUT"
    assert "expected string" in r.message


def test_rejects_unknown_parameter(page,tmp_path):
    r=engine(tmp_path).run(page,artifact(),{"member_id":"12345","typo":"x"})
    assert r.status==OutcomeKind.FAILURE and r.outcome_code=="INVALID_INPUT"


def test_recoverable_condition_does_not_consume_step_retry(page,tmp_path):
    a=artifact("demo://legacy-bank#member?member=12345&scenario=expired")
    a.steps=[Step(id="s01",action=ActionType.READ,target=target("Savings balance",LocatorStrategy(kind="css",value="span.balance")),output="savings_balance",retries=0)]
    r=engine(tmp_path).run(page,a,{"member_id":"12345"})
    assert r.status==OutcomeKind.SUCCESS and r.outputs["savings_balance"]=="$4,281.73"


def test_artifact_origin_contract_is_enforced(page,tmp_path):
    a=artifact(); a.allowed_origins=["https://not-the-artifact-origin.example"]
    r=engine(tmp_path).run(page,a,{"member_id":"12345"})
    assert r.status==OutcomeKind.FAILURE and r.outcome_code=="ARTIFACT_POLICY_MISMATCH"
    assert r.failure_class==FailureClass.POLICY


def test_policy_blocks_disallowed_logical_route():
    pol=PolicyEngine.from_file(ROOT/"policy.json")
    with pytest.raises(PolicyViolation,match="route_not_allowed"):
        pol.check_url("demo://legacy-bank#admin")


def test_hidden_text_does_not_satisfy_checkpoint(page):
    page.set_content('<div style="display:none">Savings balance</div><div>Other</div>')
    assert not PlaywrightSurface(page).verify(Checkpoint(kind="text_visible",value="Savings balance"))


def test_output_contract_missing_output_fails(page,tmp_path):
    a=artifact("demo://legacy-bank#member?member=12345")
    a.steps=[]
    r=engine(tmp_path).run(page,a,{"member_id":"12345"})
    assert r.status==OutcomeKind.FAILURE and r.outcome_code=="OUTPUT_CONTRACT_VIOLATION"


def test_handoff_exception_restores_automation_lease(page,tmp_path):
    def crash(_surface):
        raise RuntimeError("operator surface failed")
    h=HumanHandoffManager(tmp_path,operator_callback=crash)
    page.set_content('<button>Manual action</button>')
    with pytest.raises(RuntimeError,match="operator surface failed"):
        h.intervene(PlaywrightSurface(page),"goal","reason")
    assert h.lease.owner=="automation"


def test_locator_fallback_uses_second_unique_strategy(page):
    page.set_content('<label>Member Number <input name="member"></label>')
    ref=TargetRef(description="member",strategies=[LocatorStrategy(kind="css",value=".does-not-exist"),LocatorStrategy(kind="name",value="member")])
    loc=PlaywrightSurface(page).locator(ref)
    assert loc.get_attribute("name")=="member"


def test_schema_rejects_undeclared_read_output():
    with pytest.raises(Exception):
        CapabilityArtifact(capability_id="bad",name="bad",description="bad",app_id="a",vendor_product="v",entrypoint="demo://legacy-bank#home",allowed_origins=["demo://legacy-bank"],parameters=[],outputs=[],steps=[Step(id="s",action=ActionType.READ,target=target("x",LocatorStrategy(kind="css",value="span")),output="missing")],success_checkpoint=Checkpoint(kind="text_visible",value="done"))


def test_schema_rejects_unknown_placeholder():
    with pytest.raises(Exception):
        CapabilityArtifact(capability_id="bad",name="bad",description="bad",app_id="a",vendor_product="v",entrypoint="demo://legacy-bank#home",allowed_origins=["demo://legacy-bank"],parameters=[],outputs=[],steps=[Step(id="s",action=ActionType.TYPE,target=target("x",LocatorStrategy(kind="css",value="input")),value="{{missing}}")],success_checkpoint=Checkpoint(kind="text_visible",value="done"))


def test_numeric_input_is_parameterized_not_baked(page,tmp_path):
    from cua.recorder import ArtifactRecorder
    rec=ArtifactRecorder("numeric","Set deposit 25","legacy-bank-demo","Acme Legacy Core","demo://legacy-bank#subaccount",{"deposit":25},["demo://legacy-bank"])
    page.set_content('<input name="deposit" value="">')
    surface=PlaywrightSurface(page); surface.observe()
    rec.record({"action":"type","element_id":"e1","value":25,"reason":"set amount"},surface)
    assert rec.steps[0].value=="{{deposit}}"


def test_replay_does_not_invoke_gemini(page,tmp_path,monkeypatch):
    from cua import planner
    monkeypatch.setattr(planner.GeminiPlanner,"decide",lambda *a,**k: (_ for _ in ()).throw(AssertionError("LLM called during replay")))
    r=engine(tmp_path).run(page,artifact(),{"member_id":"24680"})
    assert r.status==OutcomeKind.SUCCESS and r.outputs["savings_balance"]=="$912.14"


def test_schema_version_mismatch_rejected():
    raw=json.loads(artifact().model_dump_json()); raw["schema_version"]="2.0"
    with pytest.raises(Exception): CapabilityArtifact.model_validate(raw)


def test_discovery_policy_block_returns_structured_failure(page,tmp_path):
    planner=ScriptedPlanner([{"action":"click","element_id":"e2","reason":"try click"}])
    cfg=json.loads((ROOT/"policy.json").read_text()); cfg["allowed_actions"]=["type","read"]
    pol=PolicyEngine(cfg); hand=HumanHandoffManager(tmp_path,operator_callback=lambda s: None)
    art,res=DiscoveryEngine(planner,pol,hand,tmp_path).run(page,"Find member","demo://legacy-bank#home",{"member_id":"12345"})
    assert art is None and res.status==OutcomeKind.FAILURE and res.outcome_code=="POLICY_BLOCK"
    assert Path(res.evidence["screenshot"]).exists()


def test_discovery_planner_error_routes_intervention(page,tmp_path):
    class BrokenPlanner:
        def decide(self,*args,**kwargs): raise RuntimeError("invalid model response")
    hand=HumanHandoffManager(tmp_path,operator_callback=lambda s: None)
    art,res=DiscoveryEngine(BrokenPlanner(),PolicyEngine.from_file(ROOT/"policy.json"),hand,tmp_path).run(page,"Find member","demo://legacy-bank#home",{"member_id":"12345"})
    assert art is None and res.status==OutcomeKind.ESCALATED
    assert list(tmp_path.glob("intervention-*.json"))


def test_hard_failure_has_debug_context(page,tmp_path):
    a=artifact("demo://legacy-bank#member?member=12345&scenario=permission"); a.steps=[]
    r=engine(tmp_path).run(page,a,{"member_id":"12345"})
    assert r.status==OutcomeKind.FAILURE and r.outcome_code=="PERMISSION_DENIED"
    assert r.expected and r.observed and "PERMISSION_DENIED" in r.observed


def test_same_page_and_session_identity_survive_handoff(page,tmp_path):
    page_identity=id(page); seen={}
    h=HumanHandoffManager(tmp_path,operator_callback=lambda surface: seen.update(page_same=id(surface.page)==page_identity,owner=h.lease.owner,session=h.session_id))
    initial_session=h.session_id
    page.set_content('<button>Manual action</button>')
    h.intervene(PlaywrightSurface(page),"goal","reason")
    assert seen=={"page_same":True,"owner":"human","session":initial_session}
    assert h.lease.owner=="automation" and h.session_id==initial_session


def test_irreversible_policy_can_fail_closed():
    cfg=json.loads((ROOT/"policy.json").read_text()); cfg["irreversible_action_policy"]="block"
    pol=PolicyEngine(cfg)
    s=Step(id="x",action=ActionType.CLICK,target=target("commit",role("button","Commit")),risk=RiskClass.IRREVERSIBLE)
    with pytest.raises(PolicyViolation,match="irreversible_action_blocked"):
        pol.check_step(s)


def test_replay_logs_redact_sensitive_input_and_output(page,tmp_path):
    r=engine(tmp_path).run(page,artifact(),{"member_id":"24680"})
    assert r.status==OutcomeKind.SUCCESS
    text=(tmp_path/"replay.jsonl").read_text()
    assert "24680" not in text and "$912.14" not in text
    assert "[PARAM:member_id]" in text and "[REDACTED]" in text


def test_redactor_covers_common_regulated_patterns():
    red=Redactor()
    raw="ssn 123-45-6789 card 4111 1111 1111 1111 email person@example.com amount $4,281.73 token=abcdef"
    safe=red.text(raw)
    for secret in ["123-45-6789","4111 1111 1111 1111","person@example.com","$4,281.73","abcdef"]:
        assert secret not in safe


def test_gemini_planner_uses_api_key_header_not_url(monkeypatch):
    from cua.planner import GeminiPlanner
    captured={}
    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"candidates":[{"content":{"parts":[{"text":'{"action":"finish","outputs":{},"reason":"goal met"}'}]}}]}
    def fake_post(url,**kwargs): captured.update(url=url,kwargs=kwargs); return Resp()
    monkeypatch.setattr("cua.planner.httpx.post",fake_post)
    d=GeminiPlanner("super-secret-key").decide("goal",{"elements":[]},[])
    assert d["action"]=="finish"
    assert "super-secret-key" not in captured["url"]
    assert captured["kwargs"]["headers"]["x-goog-api-key"]=="super-secret-key"


def test_gemini_model_can_be_overridden_from_env(monkeypatch):
    from cua.planner import GeminiPlanner
    monkeypatch.setenv("GEMINI_API_KEY","x"); monkeypatch.setenv("GEMINI_MODEL","gemini-custom")
    assert GeminiPlanner.from_env().model=="gemini-custom"
