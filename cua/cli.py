from __future__ import annotations
import argparse,json,subprocess,sys,time
from pathlib import Path
from playwright.sync_api import sync_playwright
from .planner import GeminiPlanner, ScriptedPlanner
from .policy import PolicyEngine
from .handoff import HumanHandoffManager
from .engine import DiscoveryEngine, ReplayEngine
from .models import CapabilityArtifact

ROOT=Path(__file__).resolve().parents[1]

def _browser(headed:bool):
    pw=sync_playwright().start(); browser=pw.chromium.launch(headless=not headed, executable_path=(__import__("os").environ.get("PLAYWRIGHT_CHROMIUM") or ("/usr/bin/chromium" if __import__("pathlib").Path("/usr/bin/chromium").exists() else None))); return pw,browser,browser.new_page()

def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest="cmd",required=True)
    d=sub.add_parser("discover"); d.add_argument("--goal",required=True); d.add_argument("--target",default="demo://legacy-bank#home"); d.add_argument("--inputs",default='{"member_id":"12345"}'); d.add_argument("--out",default="evidence/capability.json"); d.add_argument("--provider",choices=["gemini"],default="gemini"); d.add_argument("--headed",action="store_true")
    r=sub.add_parser("replay"); r.add_argument("--artifact",default="evidence/capability.json"); r.add_argument("--inputs",default='{"member_id":"12345"}'); r.add_argument("--headed",action="store_true"); r.add_argument("--human-mode",choices=["terminal"],default="terminal")
    a=p.parse_args(); policy=PolicyEngine.from_file(ROOT/"policy.json"); handoff=HumanHandoffManager(ROOT/"evidence",mode=getattr(a,"human_mode","terminal"))
    pw,browser,page=_browser(getattr(a,"headed",False))
    try:
      if a.cmd=="discover":
        try:
            planner=GeminiPlanner.from_env()
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(2)
        engine=DiscoveryEngine(planner,policy,handoff,ROOT/"evidence")
        artifact,result=engine.run(page,a.goal,a.target,json.loads(a.inputs)); print(result.model_dump_json(indent=2))
        if artifact:
            out=ROOT/a.out; out.parent.mkdir(parents=True,exist_ok=True); out.write_text(artifact.model_dump_json(indent=2)); print(f"saved {out}")
      else:
        artifact=CapabilityArtifact.model_validate_json((ROOT/a.artifact).read_text()); result=ReplayEngine(policy,handoff,ROOT/"evidence").run(page,artifact,json.loads(a.inputs)); print(result.model_dump_json(indent=2))
    finally: browser.close(); pw.stop()
if __name__=="__main__": main()
