from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from playwright.sync_api import Page
from .models import LocatorStrategy, TargetRef, Checkpoint


INTERACTIVE_JS = r"""
() => {
  document.querySelectorAll('[data-cua-eid]').forEach(e => e.removeAttribute('data-cua-eid'));
  const nodes = [...document.querySelectorAll('a,button,input,select,textarea,[role=button],[role=link],span,output')]
    .filter(el => { const r=el.getBoundingClientRect(); return r.width>0 && r.height>0; });
  return nodes.map((el,i) => {
    const eid = `e${i+1}`; el.setAttribute('data-cua-eid', eid);
    const labels = el.labels ? [...el.labels].map(x => x.innerText.trim()).filter(Boolean) : [];
    const aria = el.getAttribute('aria-label'); const cls=(el.getAttribute('class')||'').trim();
    const text = (el.innerText || el.value || el.getAttribute('placeholder') || '').trim();
    return {eid, tag:el.tagName.toLowerCase(), type:el.getAttribute('type'), name:el.getAttribute('name'),
      role:el.getAttribute('role') || (el.tagName==='BUTTON'?'button':el.tagName==='A'?'link':null),
      aria_label:aria, labels, class_name:cls, text:text.slice(0,160), href:el.getAttribute('href')};
  });
}
"""


class PlaywrightSurface:
    def __init__(self, page: Page):
        self.page = page

    def observe(self) -> dict[str, Any]:
        elements = self.page.evaluate(INTERACTIVE_JS)
        body = self.page.locator("body").inner_text(timeout=3000)
        return {"url": self.page.url, "title": self.page.title(), "visible_text": body[:6000], "elements": elements}

    def target_from_eid(self, eid: str) -> TargetRef:
        el = self.page.locator(f'[data-cua-eid="{eid}"]')
        if el.count() != 1:
            raise RuntimeError(f"ephemeral element {eid} not found")
        info = el.evaluate("""e => ({tag:e.tagName.toLowerCase(), name:e.getAttribute('name'), aria:e.getAttribute('aria-label'),
          text:(e.innerText||e.value||'').trim(), labels:e.labels?[...e.labels].map(x=>x.innerText.trim()):[], type:e.getAttribute('type'), class_name:(e.getAttribute('class')||'').trim()})""")
        strategies: list[LocatorStrategy] = []
        if info.get("aria"):
            role = "button" if info["tag"] == "button" else ("link" if info["tag"] == "a" else "textbox")
            strategies.append(LocatorStrategy(kind="role", role=role, value=info["aria"]))
        if info.get("labels"):
            strategies.append(LocatorStrategy(kind="label", value=info["labels"][0]))
        if info.get("name"):
            strategies.append(LocatorStrategy(kind="name", value=info["name"]))
        if info.get("text") and info["tag"] in {"button","a"}:
            role = "button" if info["tag"] == "button" else "link"
            strategies.append(LocatorStrategy(kind="role", role=role, value=info["text"][:120]))
            strategies.append(LocatorStrategy(kind="text", value=info["text"][:120]))
        # Last-resort structural selector. Never use discovery-only data-cua-eid.
        if info.get("name"):
            strategies.append(LocatorStrategy(kind="css", value=f'{info["tag"]}[name="{info["name"]}"]'))
        elif info.get("class_name"):
            classes=[c for c in info["class_name"].split() if c.replace("-","").replace("_","").isalnum()]
            if classes: strategies.append(LocatorStrategy(kind="css", value=info["tag"] + "." + ".".join(classes)))
        elif info.get("type"):
            strategies.append(LocatorStrategy(kind="css", value=f'{info["tag"]}[type="{info["type"]}"]'))
        if not strategies:
            strategies.append(LocatorStrategy(kind="css", value=info["tag"]))
        desc = info.get("aria") or (info.get("labels") or [None])[0] or info.get("text") or info.get("name") or info["tag"]
        return TargetRef(description=str(desc), strategies=strategies)

    def locator(self, target: TargetRef):
        errors=[]
        for s in target.strategies:
            try:
                if s.kind == "role":
                    loc = self.page.get_by_role(s.role or "button", name=s.value, exact=s.exact)
                elif s.kind == "label":
                    loc = self.page.get_by_label(s.value, exact=s.exact)
                elif s.kind == "text":
                    loc = self.page.get_by_text(s.value, exact=s.exact)
                elif s.kind == "name":
                    loc = self.page.locator(f'[name="{s.value}"]')
                else:
                    loc = self.page.locator(s.value)
                if loc.count() == 1:
                    return loc
                errors.append(f"{s.kind}:{s.value} count={loc.count()}")
            except Exception as e:
                errors.append(f"{s.kind}:{e}")
        raise RuntimeError("no_unique_locator; " + " | ".join(errors))

    def verify(self, cp: Checkpoint) -> bool:
        if cp.kind == "url_contains":
            return cp.value in self.page.url
        if cp.kind == "text_visible":
            matches = self.page.get_by_text(cp.value, exact=False)
            for i in range(matches.count()):
                try:
                    if matches.nth(i).is_visible():
                        return True
                except Exception:
                    continue
            return False
        if cp.kind == "element_visible":
            return self.locator(cp.target).is_visible()
        return False

    def screenshot(self, path: str | Path) -> str:
        p=Path(path); p.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(p), full_page=True)
        return str(p)

    def begin_human_capture(self):
        self.page.evaluate("""() => { window.__humanActions=[]; if(window.__humanCaptureInstalled) return;
          window.__humanCaptureInstalled=true;
          document.addEventListener('click', e => { const t=e.target; window.__humanActions.push({type:'click', tag:t.tagName, text:(t.innerText||t.value||'').slice(0,80), ts:Date.now()}); }, true);
          document.addEventListener('change', e => { const t=e.target; window.__humanActions.push({type:'change', tag:t.tagName, name:t.name||'', value:'[REDACTED]', ts:Date.now()}); }, true);
        }""")

    def end_human_capture(self) -> list[dict]:
        try:
            return self.page.evaluate("() => window.__humanActions || []")
        except Exception:
            return []
