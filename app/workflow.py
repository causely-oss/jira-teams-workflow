"""Core workflow shared by both triggers.

Use case 1: Causely Issue notification -> handle_issue()
Use case 2: external alert -> triage agent -> Causely Issue id -> handle_issue()

handle_issue() is the only code that writes to Jira, and it runs under a per-Issue lock,
so the two triggers cannot race each other into duplicate tickets.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Awaitable, Callable

from .alerts import NORMALIZERS, NormalizedAlert
from .causely import CauselyClient, IssueSnapshot
from .evidence import (evidence_comment, fingerprint, teams_card, ticket_description,
                       ticket_summary)
from .jira import JiraClient, issue_label
from .routing import ProjectNotPermitted, resolve_project
from .settings import SEVERITY_RANK, Settings
from .store import Store
from .teams import TeamsClient

log = logging.getLogger(__name__)

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
TriageFn = Callable[[NormalizedAlert], Awaitable[dict]]
RovoCheckFn = Callable[[IssueSnapshot], Awaitable[dict]]


def rank(sev: str | None) -> int:
    return SEVERITY_RANK.get(sev or "", 0)


def extract_issue_id(payload: Any) -> str | None:
    """Find the Causely Issue id in a notification payload without assuming its exact shape."""
    if isinstance(payload, dict):
        for k in ("issue_id", "issueId"):
            if isinstance(payload.get(k), str) and re.fullmatch(UUID, payload[k], re.I):
                return payload[k].lower()
        issue = payload.get("issue")
        if isinstance(issue, dict) and isinstance(issue.get("id"), str) and re.fullmatch(UUID, issue["id"], re.I):
            return issue["id"].lower()
        for v in payload.values():
            if (found := extract_issue_id(v)):
                return found
    elif isinstance(payload, list):
        for v in payload:
            if (found := extract_issue_id(v)):
                return found
    elif isinstance(payload, str):
        m = re.search(rf"issues?[/=]({UUID})", payload, re.I)  # portal deep links
        if m:
            return m.group(1).lower()
    return None


class Workflow:
    def __init__(self, settings: Settings, causely: CauselyClient, jira: JiraClient,
                 teams: TeamsClient, store: Store, triage: TriageFn,
                 rovo_check: RovoCheckFn | None = None):
        self.s, self.causely, self.jira, self.teams, self.store, self.triage = (
            settings, causely, jira, teams, store, triage)
        self.rovo_check = rovo_check

    # ---------------- Use case 1 ----------------
    async def on_causely_notification(self, payload: dict, project: str | None = None) -> dict:
        issue_id = extract_issue_id(payload)
        if not issue_id:
            log.warning("No Causely Issue id in notification payload")
            return {"action": "ignored", "reason": "no issue id in payload"}
        return await self.handle_issue(issue_id, trigger="Causely Issue notification", project=project)

    # ---------------- Use case 2 ----------------
    async def on_alert(self, source: str, payload: dict, project: str | None = None) -> list[dict]:
        results = []
        for alert in NORMALIZERS[source](payload):
            if alert.status != "firing":
                results.append({"alert": alert.name, "action": "ignored", "reason": "resolved"})
                continue
            if self.store.alert_is_duplicate(alert.fingerprint, self.s.alert_dedup_window_s):
                results.append({"alert": alert.name, "action": "ignored", "reason": "duplicate delivery"})
                continue
            results.append(await self._triage_and_route(alert, project))
        return results

    async def _triage_and_route(self, alert: NormalizedAlert, project: str | None = None) -> dict:
        verdict = await self.triage(alert)
        issue_id = (verdict.get("issue_id") or "").lower()
        if re.fullmatch(UUID, issue_id or ""):
            res = await self.handle_issue(
                issue_id, trigger=f"{alert.source} alert '{alert.name}'", alert=alert.as_dict(),
                project=project)
            if res["action"] != "not_found":
                return {"alert": alert.name, "verdict": verdict, **res}
        # No Causely Issue explains it: tell the channel, do not open a ticket.
        await self.teams.post_card(teams_card(
            f"Alert not explained by an active Causely Issue: {alert.name}",
            None, None, self.s.causely_portal_url,
            note=verdict.get("summary", ""),
            facts=[("Source", alert.source), ("Service", alert.service or "n/a"),
                   ("Causely mapping", verdict.get("mapping_state", "unknown"))],
        ))
        return {"alert": alert.name, "verdict": verdict, "action": "no_issue"}

    # ---------------- Shared core ----------------
    async def handle_issue(self, issue_id: str, trigger: str, alert: dict | None = None,
                           project: str | None = None) -> dict:
        async with self.store.issue_lock(issue_id):
            issue = await self.causely.get_issue(issue_id)
            if issue is None:
                return {"issue_id": issue_id, "action": "not_found"}

            cached = self.store.get(issue_id)
            key = (cached or {}).get("jira_key") or await self.jira.find_by_issue_id(
                issue_id, self.s.allowed_projects)
            fp = fingerprint(issue)

            if key is None:
                return await self._create(issue, fp, trigger, project, alert)
            return await self._update(issue, key, cached, fp, alert)

    async def _create(self, issue: IssueSnapshot, fp: str, trigger: str,
                      override: str | None = None, alert: dict | None = None) -> dict:
        if not issue.is_active:
            return {"issue_id": issue.id, "action": "skipped", "reason": "issue not active"}
        if rank(issue.severity) < self.s.min_ticket_rank:
            await self.teams.post_card(teams_card(
                f"Causely Issue below ticket threshold: {issue.name} on {issue.entity_name}",
                issue, None, self.s.causely_portal_url,
                note=f"No Jira ticket. Threshold is {self.s.jira_cfg.get('min_ticket_severity', 'Medium')}.",
            ))
            return {"issue_id": issue.id, "action": "below_threshold"}

        # Pick the project first: a disallowed project must stop us before any Jira write.
        try:
            project = resolve_project(issue, self.s, override)
        except ProjectNotPermitted as e:
            log.error("Refusing to create ticket for %s: %s", issue.id, e)
            await self.teams.post_card(teams_card(
                f"No ticket opened: {e}", issue, None, self.s.causely_portal_url,
                note="Fix jira.projects in config.yaml or the ?project= override."))
            return {"issue_id": issue.id, "action": "refused", "reason": str(e)}

        # Rovo in the loop: is a person already tracking this?
        related: list[dict] = []
        possible_dup = None
        if self.rovo_check:
            try:
                v = await self.rovo_check(issue)
            except Exception:
                log.exception("Rovo check failed; continuing to create")
                v = None
            if v:
                related = list(v.get("related") or [])
                dup = v.get("duplicate_key")
                if dup and v.get("confidence") == "high":
                    ticket = await self.jira.get_ticket(dup)      # verify, never trust the key blindly
                    in_scope = ticket and self.s.project_permitted(ticket["key"].rsplit("-", 1)[0])
                    if in_scope and ticket["status_category"] != "done":
                        return await self._adopt(issue, ticket, fp, trigger, v)
                if dup and v.get("confidence") in ("high", "medium"):
                    possible_dup = dup
                    related.insert(0, {"ref": dup, "title": "possible duplicate",
                                       "why": v.get("rationale", "")[:200]})

        # Causely drafts the ticket body. A failure falls back to the evidence-only ticket,
        # so a generate_ticket outage never blocks ticketing.
        try:
            draft = await self.causely.generate_ticket(issue, alert)
        except Exception:
            log.exception("generate_ticket failed for %s; creating evidence-only ticket", issue.id)
            draft = None
        if not isinstance(draft, dict):
            draft = None

        cfg = self.s.jira_cfg
        key = await self.jira.create(
            project=project,
            issue_type=cfg.get("issue_type", "Bug"),
            summary=ticket_summary(issue, draft),
            description=ticket_description(issue, self.s.causely_portal_url, trigger, related, draft,
                                           alert),
            labels=[issue_label(issue.id), *cfg.get("extra_labels", [])],
            priority=cfg.get("priority_map", {}).get(issue.severity),
        )
        self.store.upsert(issue.id, key, fp, issue.severity, issue.state, commented=True)
        note = f"Trigger: {trigger}."
        if possible_dup:
            note += f" Rovo flagged {possible_dup} as a possible duplicate; please check and link."
        await self.teams.post_card(teams_card(
            f"Created {key}: {issue.name} on {issue.entity_name}",
            issue, self.jira.browse_url(key), self.s.causely_portal_url, note=note,
        ))
        return {"issue_id": issue.id, "action": "created", "jira_key": key, "project": project,
                "possible_duplicate": possible_dup}

    async def _adopt(self, issue: IssueSnapshot, ticket: dict, fp: str, trigger: str,
                     verdict: dict) -> dict:
        """Attach the Causely Issue to a ticket a person already opened, instead of duplicating it."""
        key = ticket["key"]
        await self.jira.add_labels(key, [issue_label(issue.id)])
        reason = f"linked to Causely Issue by the bot (Rovo match: {verdict.get('rationale', '')[:300]})"
        await self.jira.comment(key, evidence_comment(issue, self.s.causely_portal_url, reason))
        self.store.upsert(issue.id, key, fp, issue.severity, issue.state, commented=True)
        await self.teams.post_card(teams_card(
            f"Attached to existing {key}: {ticket['summary'][:120]}",
            issue, self.jira.browse_url(key), self.s.causely_portal_url,
            note=f"Trigger: {trigger}. No new ticket opened; Causely evidence added to the existing one.",
        ))
        return {"issue_id": issue.id, "action": "adopted", "jira_key": key}

    def _comment_reason(self, issue: IssueSnapshot, cached: dict | None, fp: str,
                        alert: dict | None) -> str | None:
        if alert:
            return f"new correlated alert from {alert['source']}"
        if cached is None:
            return "ticket found in Jira, syncing current evidence"
        if cached["state"] == "ACTIVE" and not issue.is_active:
            return "Causely reports the Issue is no longer active"
        if cached["state"] != "ACTIVE" and issue.is_active:
            return "Issue is active again"
        if rank(issue.severity) > rank(cached["severity"]):
            return f"severity escalated {cached['severity']} -> {issue.severity}"
        cooled = time.time() - (cached["last_comment_at"] or 0) >= self.s.evidence_cooldown_s
        if fp != cached["fingerprint"] and cooled:
            return "evidence changed (diagnosis, symptoms, or impact)"
        return None

    async def _update(self, issue: IssueSnapshot, key: str, cached: dict | None, fp: str,
                      alert: dict | None) -> dict:
        reason = self._comment_reason(issue, cached, fp, alert)
        top_sev = max(issue.severity, (cached or {}).get("severity") or "", key=rank)
        if reason is None:
            # Keep the stored fingerprint so a later real change is still detected.
            self.store.upsert(issue.id, key, (cached or {}).get("fingerprint") or fp,
                              top_sev, issue.state, commented=False)
            return {"issue_id": issue.id, "action": "no_change", "jira_key": key}

        await self.jira.comment(key, evidence_comment(issue, self.s.causely_portal_url, reason, alert))
        self.store.upsert(issue.id, key, fp, top_sev, issue.state, commented=True)
        await self.teams.post_card(teams_card(
            f"Updated {key}: {reason}", issue, self.jira.browse_url(key), self.s.causely_portal_url,
        ))
        return {"issue_id": issue.id, "action": "commented", "jira_key": key, "reason": reason}
