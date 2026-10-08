"""Dry-run Jira: everything runs except Jira writes.

With JIRA_DRY_RUN=true, create/comment/label calls are logged and written to DRY_RUN_DIR as
Markdown instead of reaching Jira. If Jira credentials are set, reads (label search, ticket
lookups) still go to the real Jira, so dedup behaves as it would in production. Without
credentials, reads return "nothing found".
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def adf_to_markdown(node: Any) -> str:
    """Just enough of Atlassian Document Format for the node types evidence.py emits."""
    if not isinstance(node, dict):
        return ""
    t = node.get("type")
    kids = node.get("content", [])
    if t == "text":
        text = node.get("text", "")
        for m in node.get("marks", []):
            if m.get("type") == "strong":
                text = f"**{text}**"
            elif m.get("type") == "link":
                text = f"[{text}]({m['attrs']['href']})"
        return text
    inner = "".join(adf_to_markdown(k) for k in kids) if t in ("paragraph", "heading") else ""
    if t == "heading":
        return "#" * (node.get("attrs", {}).get("level", 3)) + f" {inner}\n\n"
    if t == "paragraph":
        return f"{inner}\n\n"
    if t == "bulletList":
        items = ["- " + adf_to_markdown(li).strip() for li in kids]
        return "\n".join(items) + "\n\n"
    return "".join(adf_to_markdown(k) for k in kids)


class DryRunJira:
    def __init__(self, inner: Any | None, out_dir: str):
        self.inner = inner
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.n = len(list(self.out.glob("DRYRUN-*.md")))

    def browse_url(self, key: str) -> str:
        return str((self.out / f"{key}.md").resolve())

    async def find_by_issue_id(self, issue_id: str, projects: list[str]) -> str | None:
        return await self.inner.find_by_issue_id(issue_id, projects) if self.inner else None

    async def get_ticket(self, key: str) -> dict | None:
        if key.startswith("DRYRUN-"):
            return None
        return await self.inner.get_ticket(key) if self.inner else None

    async def project_exists(self, key: str) -> bool:
        return await self.inner.project_exists(key) if self.inner else True

    async def create(self, project: str, issue_type: str, summary: str, description: dict,
                     labels: list[str], priority: str | None) -> str:
        self.n += 1
        key = f"DRYRUN-{self.n}"
        body = (f"# {summary}\n\n"
                f"_Dry run: would create a **{issue_type}** in **{project}**, priority "
                f"**{priority or 'project default'}**, labels `{', '.join(labels)}`._\n\n"
                + adf_to_markdown(description))
        path = self.out / f"{key}.md"
        path.write_text(body)
        log.info("DRY RUN: would create %s ticket %r -> %s", project, summary, path)
        return key

    async def comment(self, key: str, body: dict) -> None:
        path = self.out / f"{key}.md"
        text = "\n---\n\n## Comment (dry run)\n\n" + adf_to_markdown(body)
        if path.exists():
            with path.open("a") as f:
                f.write(text)
        log.info("DRY RUN: would comment on %s%s", key, f" -> {path}" if path.exists() else "")

    async def add_labels(self, key: str, labels: list[str]) -> None:
        log.info("DRY RUN: would add labels %s to %s", labels, key)
