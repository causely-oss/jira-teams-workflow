"""Normalize inbound alerts from Datadog, Alertmanager, Dynatrace, or a generic JSON shape."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field


@dataclass
class NormalizedAlert:
    source: str
    name: str
    status: str = "firing"            # firing | resolved
    severity: str = ""
    service: str = ""
    namespace: str = ""
    cluster: str = ""
    labels: dict = field(default_factory=dict)
    source_id: str = ""

    @property
    def fingerprint(self) -> str:
        key = self.source_id or f"{self.name}|{self.service}|{self.namespace}|{self.cluster}"
        return hashlib.sha256(f"{self.source}|{key}".encode()).hexdigest()[:16]

    def as_dict(self) -> dict:
        return asdict(self)


def _tags_to_dict(tags) -> dict:
    if isinstance(tags, dict):
        return tags
    out = {}
    items = tags.split(",") if isinstance(tags, str) else (tags or [])
    for t in items:
        t = str(t).strip()
        if ":" in t:
            k, v = t.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def from_datadog(p: dict) -> list[NormalizedAlert]:
    # Expects the webhook template in README (title, alert_id, alert_transition, priority, tags).
    tags = _tags_to_dict(p.get("tags", ""))
    transition = str(p.get("alert_transition") or p.get("alert_status") or "").lower()
    return [NormalizedAlert(
        source="datadog",
        name=p.get("title") or p.get("alert_title") or "Datadog alert",
        status="resolved" if "recovered" in transition or "resolved" in transition else "firing",
        severity=str(p.get("priority") or ""),
        service=tags.get("service", ""),
        namespace=tags.get("kube_namespace", "") or tags.get("namespace", ""),
        cluster=tags.get("kube_cluster_name", "") or tags.get("cluster_name", ""),
        labels=tags,
        source_id=str(p.get("alert_id") or p.get("aggreg_key") or ""),
    )]


def from_alertmanager(p: dict) -> list[NormalizedAlert]:
    out = []
    for a in p.get("alerts", []):
        lb = a.get("labels", {})
        out.append(NormalizedAlert(
            source="alertmanager",
            name=lb.get("alertname", "Alertmanager alert"),
            status=a.get("status", "firing"),
            severity=lb.get("severity", ""),
            service=lb.get("service") or lb.get("app") or lb.get("container") or "",
            namespace=lb.get("namespace", ""),
            cluster=lb.get("cluster", ""),
            labels={**lb, **{f"annotation.{k}": v for k, v in a.get("annotations", {}).items()}},
            source_id=a.get("fingerprint", ""),
        ))
    return out


def from_dynatrace(p: dict) -> list[NormalizedAlert]:
    # Dynatrace problem notification with a custom JSON payload (see README).
    tags = _tags_to_dict(p.get("Tags", ""))
    state = str(p.get("State", "OPEN")).upper()
    return [NormalizedAlert(
        source="dynatrace",
        name=p.get("ProblemTitle", "Dynatrace problem"),
        status="resolved" if state in ("RESOLVED", "CLOSED") else "firing",
        severity=str(p.get("ProblemSeverity", "")),
        service=p.get("ImpactedEntity") or tags.get("service", ""),
        namespace=tags.get("k8s.namespace.name", "") or tags.get("namespace", ""),
        cluster=tags.get("k8s.cluster.name", ""),
        labels=tags,
        source_id=str(p.get("PID") or p.get("ProblemID") or ""),
    )]


def from_generic(p: dict) -> list[NormalizedAlert]:
    return [NormalizedAlert(
        source=p.get("source", "generic"), name=p["name"], status=p.get("status", "firing"),
        severity=p.get("severity", ""), service=p.get("service", ""),
        namespace=p.get("namespace", ""), cluster=p.get("cluster", ""),
        labels=p.get("labels", {}), source_id=p.get("id", ""),
    )]


NORMALIZERS = {
    "datadog": from_datadog,
    "alertmanager": from_alertmanager,
    "dynatrace": from_dynatrace,
    "generic": from_generic,
}
