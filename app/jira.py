"""Jira Cloud REST v3 client. Dedup key is the label causely-issue-<uuid>."""
from __future__ import annotations

import httpx


def issue_label(issue_id: str) -> str:
    return f"causely-issue-{issue_id}"


def api_base(site_url: str, cloud_id: str) -> str:
    """Scoped API tokens only work through the api.atlassian.com gateway (401 on the site URL).
    Classic tokens work there too, so always use it when the cloud id is known."""
    return f"https://api.atlassian.com/ex/jira/{cloud_id}" if cloud_id else site_url.rstrip("/")


class JiraClient:
    def __init__(self, base_url: str, email: str, token: str, cloud_id: str = ""):
        self.base = base_url.rstrip("/")  # the site, for browse links
        self.http = httpx.AsyncClient(base_url=api_base(base_url, cloud_id), auth=(email, token),
                                      timeout=30, headers={"Accept": "application/json"})

    def browse_url(self, key: str) -> str:
        return f"{self.base}/browse/{key}"

    async def find_by_issue_id(self, issue_id: str, projects: list[str]) -> str | None:
        """Only looks inside the bot's allowed projects, so it never touches anything else."""
        jql = f'project in ({",".join(projects)}) AND labels = "{issue_label(issue_id)}" ORDER BY created ASC'
        r = await self.http.get("/rest/api/3/search/jql",
                                params={"jql": jql, "fields": "key,status", "maxResults": 5})
        r.raise_for_status()
        issues = r.json().get("issues", [])
        return issues[0]["key"] if issues else None

    async def create(self, project: str, issue_type: str, summary: str, description: dict,
                     labels: list[str], priority: str | None) -> str:
        fields = {
            "project": {"key": project},
            "issuetype": {"name": issue_type},
            "summary": summary,
            "description": description,
            "labels": labels,
        }
        if priority:
            fields["priority"] = {"name": priority}
        r = await self.http.post("/rest/api/3/issue", json={"fields": fields})
        if r.status_code == 400 and priority and "priority" in r.text:
            fields.pop("priority")  # project may not expose priority on its create screen
            r = await self.http.post("/rest/api/3/issue", json={"fields": fields})
        if r.status_code == 400:
            raise RuntimeError(f"Jira rejected ticket create in {project}: {r.text[:500]}")
        r.raise_for_status()
        return r.json()["key"]

    async def comment(self, key: str, body: dict) -> None:
        r = await self.http.post(f"/rest/api/3/issue/{key}/comment", json={"body": body})
        r.raise_for_status()

    async def project_exists(self, key: str) -> bool:
        r = await self.http.get(f"/rest/api/3/project/{key}")
        return r.status_code == 200

    async def get_ticket(self, key: str) -> dict | None:
        r = await self.http.get(f"/rest/api/3/issue/{key}",
                                params={"fields": "summary,status,labels,created"})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        f = r.json()["fields"]
        return {
            "key": key, "summary": f.get("summary", ""), "labels": f.get("labels", []),
            "created": f.get("created", ""),
            "status_category": ((f.get("status") or {}).get("statusCategory") or {}).get("key", ""),
        }

    async def add_labels(self, key: str, labels: list[str]) -> None:
        r = await self.http.put(f"/rest/api/3/issue/{key}",
                                json={"update": {"labels": [{"add": l} for l in labels]}})
        r.raise_for_status()
