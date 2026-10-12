"""Runtime settings: env vars for secrets, config.yaml for routing and policy."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SEVERITY_RANK = {"Low": 1, "Medium": 2, "High": 3, "Critical": 4}


@dataclass
class Settings:
    anthropic_model: str = field(default_factory=lambda: os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"))
    causely_mcp_url: str = field(default_factory=lambda: os.getenv("CAUSELY_MCP_URL", "https://api.causely.app/mcp"))
    causely_client_id: str = field(default_factory=lambda: os.getenv("CAUSELY_CLIENT_ID", ""))
    causely_client_secret: str = field(default_factory=lambda: os.getenv("CAUSELY_CLIENT_SECRET", ""))
    causely_token_url: str = field(default_factory=lambda: os.getenv(
        "CAUSELY_TOKEN_URL",
        "https://auth.causely.app/frontegg/identity/resources/auth/v2/api-token",
    ))
    causely_portal_url: str = field(default_factory=lambda: os.getenv("CAUSELY_PORTAL_URL", "https://portal.causely.app"))
    jira_base_url: str = field(default_factory=lambda: os.getenv("JIRA_BASE_URL", ""))
    jira_email: str = field(default_factory=lambda: os.getenv("JIRA_EMAIL", ""))
    jira_api_token: str = field(default_factory=lambda: os.getenv("JIRA_API_TOKEN", ""))
    # Rovo MCP needs a scoped API token (agent-interface scopes). Classic tokens keep working
    # for Jira REST, so this is separate. Falls back to JIRA_API_TOKEN when unset.
    rovo_api_token: str = field(default_factory=lambda: os.getenv("ROVO_API_TOKEN", ""))
    rovo_mcp_url: str = field(default_factory=lambda: os.getenv("ROVO_MCP_URL", "https://mcp.atlassian.com/v1/mcp"))
    atlassian_cloud_id: str = field(default_factory=lambda: os.getenv("ATLASSIAN_CLOUD_ID", ""))
    atlassian_service_key: str = field(default_factory=lambda: os.getenv("ATLASSIAN_SERVICE_KEY", ""))
    teams_workflow_url: str = field(default_factory=lambda: os.getenv("TEAMS_WORKFLOW_URL", ""))
    inbound_token: str = field(default_factory=lambda: os.getenv("INBOUND_WEBHOOK_TOKEN", ""))
    jira_dry_run: bool = field(default_factory=lambda: os.getenv("JIRA_DRY_RUN", "").lower() in ("1", "true", "yes"))
    dry_run_dir: str = field(default_factory=lambda: os.getenv("DRY_RUN_DIR", "./dry-run"))
    # Dry runs keep their own state, so fake DRYRUN-n keys never leak into a real run.
    state_db: str = field(default_factory=lambda: os.getenv(
        "STATE_DB", "./state.dryrun.db" if os.getenv("JIRA_DRY_RUN", "").lower() in ("1", "true", "yes")
        else "./state.db"))
    config: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        path = Path(os.getenv("ROUTING_CONFIG", "./config.yaml"))
        s.config = yaml.safe_load(path.read_text()) if path.exists() else {}
        return s

    @property
    def jira_cfg(self) -> dict:
        return self.config.get("jira", {})

    # ---- Jira project policy ----
    @property
    def projects_cfg(self) -> dict:
        return self.jira_cfg.get("projects", {})

    @property
    def default_project(self) -> str | None:
        return self.projects_cfg.get("default")

    @property
    def allowed_projects(self) -> list[str]:
        return [p.upper() for p in self.projects_cfg.get("allowed", [])]

    @property
    def blocked_projects(self) -> list[str]:
        return [p.upper() for p in self.projects_cfg.get("blocked", [])]

    def project_permitted(self, key: str | None) -> bool:
        k = (key or "").upper()
        return bool(k) and k in self.allowed_projects and k not in self.blocked_projects

    def validate_projects(self) -> None:
        """Fail fast at startup. There is deliberately no implicit default project."""
        errors = []
        if not self.allowed_projects:
            errors.append("jira.projects.allowed is empty: list the projects this bot may write to")
        if "CHANGE_ME" in [*self.allowed_projects, (self.default_project or "").upper()]:
            errors.append("replace CHANGE_ME in jira.projects with a real project key")
        if not self.default_project:
            errors.append("jira.projects.default is not set")
        elif not self.project_permitted(self.default_project):
            errors.append(f"default project {self.default_project} is not allowed or is blocked")
        for r in self.jira_cfg.get("routes", []) or []:
            if not self.project_permitted(r.get("project")):
                errors.append(f"route {r.get('match')} -> {r.get('project')} is not allowed or is blocked")
        if overlap := set(self.allowed_projects) & set(self.blocked_projects):
            errors.append(f"projects both allowed and blocked: {sorted(overlap)}")
        if errors:
            raise ValueError("Invalid Jira project config:\n  - " + "\n  - ".join(errors))

    @property
    def rovo_headers(self) -> dict[str, str]:
        from .rovo import rovo_headers
        return rovo_headers(self.jira_email, self.rovo_api_token or self.jira_api_token,
                            self.atlassian_service_key)

    @property
    def has_jira_credentials(self) -> bool:
        return bool(self.jira_base_url and (self.jira_api_token or self.atlassian_service_key))

    @property
    def rovo_cfg(self) -> dict:
        return self.config.get("rovo", {})

    @property
    def min_ticket_rank(self) -> int:
        return SEVERITY_RANK.get(self.jira_cfg.get("min_ticket_severity", "Medium"), 2)

    @property
    def evidence_cooldown_s(self) -> int:
        return int(self.jira_cfg.get("evidence_cooldown_seconds", 1800))

    @property
    def alert_dedup_window_s(self) -> int:
        return int(self.config.get("alerts", {}).get("dedup_window_seconds", 600))
