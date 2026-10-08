"""Deterministic Causely MCP access (no LLM in this process). Used for the Issue fetch in
both flows and for the ticket draft, so the Jira ticket is always built from Causely's own
output, not a model's paraphrase."""
from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any

from .causely_auth import CauselyAuth
from .mcp_client import McpToolClient


class CauselyClient(McpToolClient):
    def __init__(self, url: str, auth: CauselyAuth):
        super().__init__(url, {})
        self.auth = auth

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        token = await self.auth.access_token()
        return await super().call(tool, args, headers={"Authorization": f"Bearer {token}"})

    async def get_issue(self, issue_id: str) -> "IssueSnapshot | None":
        data = await self.call("get_issue_details", {"issue_id": issue_id})
        issues = data.get("issues") or []
        return IssueSnapshot.from_details(issues[0]) if issues else None

    async def generate_ticket(self, issue: "IssueSnapshot", alert: dict | None = None) -> dict:
        """Causely's structured ticket draft. The tool does not look the Issue up itself,
        so the task text must carry the evidence or the draft comes back generic."""
        return await self.call("generate_ticket", {"task": ticket_task(issue, alert)})


def ticket_task(issue: "IssueSnapshot", alert: dict | None = None) -> str:
    where = ", ".join(p for p in (f"cluster {issue.cluster}" if issue.cluster else "",
                                  f"namespace {issue.namespace}" if issue.namespace else "") if p)
    parts = [
        f'Create a remediation ticket for Causely Issue {issue.id} "{issue.name}" on '
        f"{issue.entity_type.lower() or 'entity'} {issue.entity_name}" + (f" ({where})." if where else "."),
        f"Severity {issue.severity} (peak {issue.peak_severity}), active since {issue.started_at}.",
        f"Primary diagnosis: {issue.primary_diagnosis_name} on {issue.primary_diagnosis_entity} "
        f"(diagnosis id {issue.primary_diagnosis_id}).",
    ]
    if issue.causal_chain:
        parts.append("Causal chain: " + "; ".join(
            f"hop {n['hops']} {n['entity']} ({', '.join(s for s in n['signals'] if s)})"
            for n in issue.causal_chain) + ".")
    if issue.symptoms:
        parts.append("Active symptoms: " + "; ".join(
            f"{s['name']} on {s['entity']}" for s in issue.symptoms) + ".")
    if issue.impacted_services:
        parts.append("Impacted services: " + ", ".join(issue.impacted_services) + ".")
    if alert:
        parts.append(f"Raised by {alert['source']} alert \"{alert['name']}\" "
                     f"(severity {alert.get('severity') or 'n/a'}) on "
                     f"{alert.get('service') or 'unknown service'}; Causely identified this Issue "
                     "as its root cause.")
    return " ".join(parts)


@dataclass
class IssueSnapshot:
    """The subset of get_issue_details the workflow needs."""
    id: str
    name: str
    state: str
    severity: str
    peak_severity: str
    started_at: str
    entity_id: str
    entity_name: str
    entity_type: str
    labels: dict[str, str]
    primary_diagnosis_id: str
    primary_diagnosis_name: str
    primary_diagnosis_entity: str
    diagnosis_count: int
    symptoms: list[dict] = field(default_factory=list)          # {id, name, entity}
    causal_chain: list[dict] = field(default_factory=list)      # {entity, hops, signals}
    impacted_services: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_details(cls, d: dict) -> "IssueSnapshot":
        pd = d.get("primary_diagnosis") or {}
        det = d.get("primary_diagnosis_detail") or {}
        chain = (det.get("entity_causal_chain") or det.get("causal_chain") or {}).get("nodes", [])
        return cls(
            id=d["id"],
            name=d.get("name", ""),
            state=d.get("state", ""),
            severity=d.get("severity", ""),
            peak_severity=d.get("peak_severity", ""),
            started_at=d.get("started_at", ""),
            entity_id=d.get("entity", {}).get("id", ""),
            entity_name=d.get("entity", {}).get("name", ""),
            entity_type=d.get("entity", {}).get("type", ""),
            labels=(det.get("entity") or {}).get("labels", {}) or {},
            primary_diagnosis_id=pd.get("id", ""),
            primary_diagnosis_name=det.get("name") or pd.get("name", ""),
            primary_diagnosis_entity=(pd.get("entity") or {}).get("name", ""),
            diagnosis_count=int(d.get("diagnosis_count") or 0),
            symptoms=[
                {"id": s.get("id"), "name": s.get("name"), "entity": (s.get("entity") or {}).get("name")}
                for s in det.get("symptoms", []) if s.get("active", True)
            ],
            causal_chain=[
                {
                    "entity": (n.get("entity") or {}).get("name"),
                    "hops": n.get("hops"),
                    "signals": [sg.get("name") for sg in n.get("signals", [])],
                }
                for n in sorted(chain, key=lambda n: n.get("hops", 0))
            ],
            impacted_services=sorted(
                s.get("name") for s in det.get("impacted_services", []) if s.get("name")
            ),
            raw=d,
        )

    @property
    def is_active(self) -> bool:
        return self.state.upper() == "ACTIVE"

    @property
    def namespace(self) -> str:
        if ns := self.labels.get("causely.ai/namespace") or self.labels.get("k8s.namespace.name"):
            return ns
        return self.entity_name.split("/")[0] if "/" in self.entity_name else ""

    @property
    def cluster(self) -> str:
        return self.labels.get("causely.ai/cluster") or self.labels.get("k8s.cluster.name", "")

    @property
    def team(self) -> str:
        return self.labels.get("causely.ai/team", "")
