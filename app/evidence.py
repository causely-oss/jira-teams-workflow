"""Evidence fingerprinting and rendering (Jira ADF + Teams Adaptive Card).

Fingerprint excludes severity and state on purpose: those are handled as explicit
escalation / resolution events. Everything else (which diagnosis leads, which symptoms
are active, what is impacted) only produces a comment when it actually changes.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .causely import IssueSnapshot


def fingerprint(issue: IssueSnapshot) -> str:
    key = {
        "dx": [issue.primary_diagnosis_name, issue.primary_diagnosis_entity],
        "symptoms": sorted(s["id"] or "" for s in issue.symptoms),
        "impacted": issue.impacted_services,
    }
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


def portal_link(portal: str, issue: IssueSnapshot) -> str:
    # Confirm the exact Issue deep-link path in the portal before relying on it.
    return f"{portal.rstrip('/')}/issues/{issue.id}"


# ---------- Jira ADF ----------

def _text(t: str, href: str | None = None, strong: bool = False) -> dict:
    node: dict[str, Any] = {"type": "text", "text": t}
    marks = []
    if href:
        marks.append({"type": "link", "attrs": {"href": href}})
    if strong:
        marks.append({"type": "strong"})
    if marks:
        node["marks"] = marks
    return node


def _p(*nodes: dict) -> dict:
    return {"type": "paragraph", "content": list(nodes)}


def _h(t: str, level: int = 3) -> dict:
    return {"type": "heading", "attrs": {"level": level}, "content": [_text(t)]}


def _bullets(items: list[str]) -> dict:
    items = items or ["(none reported)"]
    return {
        "type": "bulletList",
        "content": [{"type": "listItem", "content": [_p(_text(i))]} for i in items],
    }


def _doc(blocks: list[dict]) -> dict:
    return {"type": "doc", "version": 1, "content": blocks}


def _evidence_blocks(issue: IssueSnapshot) -> list[dict]:
    chain = [
        f"hop {n['hops']}: {n['entity']} ({', '.join(s for s in n['signals'] if s)})"
        for n in issue.causal_chain
    ]
    return [
        _h("Diagnosis"),
        _p(_text(f"{issue.primary_diagnosis_name} on {issue.primary_diagnosis_entity}", strong=True)),
        _h("Causal chain"),
        _bullets(chain),
        _h("Active symptoms"),
        _bullets([f"{s['name']} on {s['entity']}" for s in issue.symptoms]),
        _h("Impacted services"),
        _bullets(issue.impacted_services),
    ]


def _alert_blocks(alert: dict | None) -> list[dict]:
    if not alert:
        return []
    return [_h("Correlated alert"),
            _p(_text(f"{alert['source']}: {alert['name']} ({alert.get('severity') or 'n/a'}) "
                     f"on {alert.get('service') or 'unknown service'}"))]


def ticket_summary(issue: IssueSnapshot, draft: dict | None = None) -> str:
    if draft and (title := str(draft.get("title") or "").strip()):
        return f"[Causely] {title}"[:250]
    return f"[Causely] {issue.name}: {issue.entity_name}"[:250]


def _draft_blocks(draft: dict | None) -> list[dict]:
    """Sections from Causely's generate_ticket draft, in the order an engineer reads them."""
    if not draft:
        return []
    blocks: list[dict] = []
    for key, heading in (("context", "Context"), ("description", "Description")):
        if text := str(draft.get(key) or "").strip():
            blocks += [_h(heading), _p(_text(text))]
    for key, heading in (("requirements", "Requirements"),
                         ("acceptance_criteria", "Acceptance criteria")):
        items = [str(i) for i in draft.get(key) or [] if str(i).strip()]
        if items:
            blocks += [_h(heading), _bullets(items)]
    if notes := str(draft.get("additional_notes") or "").strip():
        blocks += [_h("Additional notes"), _p(_text(notes))]
    return blocks


def _related_blocks(related: list[dict] | None) -> list[dict]:
    if not related:
        return []
    return [_h("Related (found via Rovo)"),
            _bullets([f"{r['ref']}: {r['title']} ({r['why']})" for r in related[:5]])]


def ticket_description(issue: IssueSnapshot, portal: str, trigger: str,
                       related: list[dict] | None = None, draft: dict | None = None,
                       alert: dict | None = None) -> dict:
    draft_blocks = _draft_blocks(draft)
    if draft_blocks:
        draft_blocks.append(_h("Causely evidence", level=2))
    return _doc(
        [
            _p(
                _text("Opened automatically from Causely Issue "),
                _text(issue.id, href=portal_link(portal, issue)),
                _text(f". Trigger: {trigger}."),
            ),
            _p(_text(
                f"Severity {issue.severity} (peak {issue.peak_severity}). Affected entity: "
                f"{issue.entity_name} [{issue.entity_type}]. Cluster: {issue.cluster or 'n/a'}. "
                f"Namespace: {issue.namespace or 'n/a'}. Started {issue.started_at}. "
                f"{issue.diagnosis_count} diagnoses grouped under this Issue so far."
            )),
            *_alert_blocks(alert),
            *draft_blocks,
            *_evidence_blocks(issue),
            *_related_blocks(related),
        ]
    )


def evidence_comment(issue: IssueSnapshot, portal: str, reason: str, alert: dict | None = None) -> dict:
    blocks = [_p(_text(f"Causely evidence update: {reason}", strong=True)),
              _p(_text(f"Severity now {issue.severity}, state {issue.state}. "),
                 _text("Open in Causely", href=portal_link(portal, issue))),
              *_alert_blocks(alert)]
    if issue.is_active:
        blocks += _evidence_blocks(issue)
    return _doc(blocks)


# ---------- Teams Adaptive Card ----------

def teams_card(title: str, issue: IssueSnapshot | None, jira_url: str | None, portal: str,
               note: str = "", facts: list[tuple[str, str]] | None = None) -> dict:
    body: list[dict] = [{"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True}]
    f = list(facts or [])
    if issue:
        f = [
            ("Entity", issue.entity_name),
            ("Diagnosis", f"{issue.primary_diagnosis_name} on {issue.primary_diagnosis_entity}"),
            ("Severity", f"{issue.severity} (peak {issue.peak_severity})"),
            ("Impacted", ", ".join(issue.impacted_services[:5]) or "n/a"),
        ] + f
    if f:
        body.append({"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in f]})
    if note:
        body.append({"type": "TextBlock", "text": note, "wrap": True, "isSubtle": True})
    actions = []
    if jira_url:
        actions.append({"type": "Action.OpenUrl", "title": "Open Jira", "url": jira_url})
    if issue:
        actions.append({"type": "Action.OpenUrl", "title": "Open in Causely", "url": portal_link(portal, issue)})
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
        "actions": actions,
    }
