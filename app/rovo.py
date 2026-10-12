"""Jira through the Atlassian Rovo MCP server, called deterministically from code.

Same interface as jira.JiraClient, so workflow.py does not care which backend is used.
Auth is headless: an org admin must enable API token auth for the Rovo MCP server
(admin.atlassian.com > Rovo MCP server > Authentication). Then use either
  Basic  base64(email:api_token)   (personal API token), or
  Bearer <service account API key>.
"""
from __future__ import annotations

import base64
from typing import Any

from .jira import issue_label
from .mcp_client import McpToolClient


def _find_key(obj: Any) -> str | None:
    if isinstance(obj, dict):
        if isinstance(obj.get("key"), str) and "-" in obj["key"]:
            return obj["key"]
        for v in obj.values():
            if (k := _find_key(v)):
                return k
    elif isinstance(obj, list):
        for v in obj:
            if (k := _find_key(v)):
                return k
    return None


def _inline(node: dict) -> str:
    if node.get("type") != "text":
        return ""
    text = node.get("text", "")
    marks = node.get("marks") or []
    if any(m.get("type") == "strong" for m in marks):
        text = f"**{text}**"
    href = next((m.get("attrs", {}).get("href") for m in marks if m.get("type") == "link"), None)
    if href:
        text = f"[{text}]({href})"
    return text


def _adf_to_markdown(body: dict | str) -> str:
    """Rovo v2 accepts markdown, not ADF. The rest of the app still builds ADF."""
    if not isinstance(body, dict):
        return str(body)
    lines: list[str] = []
    for block in body.get("content") or []:
        kind = block.get("type")
        if kind == "bulletList":
            for item in block.get("content") or []:
                bits = []
                for para in item.get("content") or []:
                    bits.append("".join(_inline(n) for n in para.get("content") or []))
                lines.append("- " + "".join(bits))
            continue
        text = "".join(_inline(n) for n in block.get("content") or [])
        if kind == "heading":
            level = int((block.get("attrs") or {}).get("level") or 3)
            text = f"{'#' * level} {text}"
        if text:
            lines.append(text)
    return "\n\n".join(lines)


def rovo_headers(email: str, api_token: str, service_key: str = "") -> dict[str, str]:
    if service_key:
        return {"Authorization": f"Bearer {service_key}"}
    basic = base64.b64encode(f"{email}:{api_token}".encode()).decode()
    return {"Authorization": f"Basic {basic}"}


async def probe_rovo(mcp_url: str, headers: dict[str, str], cloud_id: str) -> str | None:
    """One cheap read to prove Rovo MCP accepts these credentials. None = OK, else the reason.

    Without org-admin approval, Rovo MCP rejects API tokens on every call. An agent that
    can't call its tools would just report "no duplicate", so check before relying on it."""
    if not cloud_id:
        return "ATLASSIAN_CLOUD_ID is not set"
    try:
        await McpToolClient(mcp_url, headers).call(
            "searchJiraIssuesUsingJql", {"cloudId": cloud_id, "jql": "created >= -1d", "maxResults": 1,
                                         "fields": ["key"]})
    except Exception as e:  # noqa: BLE001 - any failure means "not usable"
        msg = str(e)
        if "permission to connect via API token" in msg:
            return ("Rovo MCP rejected the API token. Either API-token auth is off for the org "
                    "(admin.atlassian.com > Rovo MCP server > Authentication), or the token isn't "
                    "a scoped token with the Jira agent-interface scopes: set ROVO_API_TOKEN")
        return msg[:300]
    return None


class RovoJiraClient:
    def __init__(self, mcp_url: str, headers: dict[str, str], cloud_id: str, site_url: str):
        self.mcp = McpToolClient(mcp_url, headers)
        self.cloud_id = cloud_id
        self.site = site_url.rstrip("/")

    def browse_url(self, key: str) -> str:
        return f"{self.site}/browse/{key}"

    async def find_by_issue_id(self, issue_id: str, projects: list[str]) -> str | None:
        """Only looks inside the bot's allowed projects, so it never touches anything else."""
        res = await self.mcp.call("searchJiraIssuesUsingJql", {
            "cloudId": self.cloud_id,
            "jql": f'project in ({",".join(projects)}) AND labels = "{issue_label(issue_id)}" ORDER BY created ASC',
            "fields": ["key"], "maxResults": 50,
        })
        issues = res.get("issues", []) if isinstance(res, dict) else []
        return issues[0]["key"] if issues else None

    async def project_exists(self, key: str) -> bool:
        # listJiraProjects is not a directly callable tool on Rovo MCP v2.
        # A JQL search against the project key succeeds only when the account can see it.
        try:
            await self.mcp.call("searchJiraIssuesUsingJql", {
                "cloudId": self.cloud_id,
                "jql": f'project = "{key}" ORDER BY created DESC',
                "fields": ["key"],
                "maxResults": 1,
            })
        except RuntimeError as e:
            msg = str(e)
            if "does not exist" in msg.lower():
                return False
            if "permission to connect via api token" in msg.lower():
                raise RuntimeError(
                    "Rovo MCP refused this API token for Jira. An org admin must enable "
                    "API token authentication (admin.atlassian.com > Rovo MCP server > Authentication), "
                    "or set jira.backend to rest."
                ) from e
            raise
        return True

    async def get_ticket(self, key: str) -> dict | None:
        try:
            res = await self.mcp.call("getJiraIssue", {
                "cloudId": self.cloud_id, "issueIdOrKey": key,
                "fields": ["summary", "status", "labels", "created"],
                "responseContentFormat": "markdown",
            })
        except RuntimeError:
            return None
        f = res.get("fields", {}) if isinstance(res, dict) else {}
        return {
            "key": res.get("key", key),
            "summary": f.get("summary", ""),
            "labels": f.get("labels", []) or [],
            "created": f.get("created", ""),
            "status_category": ((f.get("status") or {}).get("statusCategory") or {}).get("key", ""),
        }

    async def create(self, project: str, issue_type: str, summary: str, description: dict,
                     labels: list[str], priority: str | None) -> str:
        args = {
            "cloudId": self.cloud_id, "projectKey": project, "issueType": issue_type,
            "summary": summary, "description": _adf_to_markdown(description),
            "contentFormat": "markdown", "labels": labels,
        }
        if priority:
            args["priority"] = priority
        try:
            res = await self.mcp.call("createJiraIssue", args)
        except RuntimeError as e:
            if not priority or "priority" not in str(e).lower():
                raise
            args.pop("priority")  # project may not expose priority on its create screen
            res = await self.mcp.call("createJiraIssue", args)
        key = _find_key(res)
        if not key:
            raise RuntimeError(f"createJiraIssue returned no key: {str(res)[:300]}")
        return key

    async def comment(self, key: str, body: dict) -> None:
        await self.mcp.call("addOrEditJiraIssueComment", {
            "cloudId": self.cloud_id, "issueIdOrKey": key,
            "commentBody": _adf_to_markdown(body), "contentFormat": "markdown",
        })

    async def add_labels(self, key: str, labels: list[str]) -> None:
        current = (await self.get_ticket(key) or {}).get("labels", [])
        merged = sorted(set(current) | set(labels))
        await self.mcp.call("editJiraIssue", {
            "cloudId": self.cloud_id, "issueIdOrKey": key, "fields": {"labels": merged},
        })
