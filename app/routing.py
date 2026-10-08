"""Pick the Jira project for a Causely Issue.

Precedence: explicit request override (?project=KEY) > first matching route > default.
Every candidate must pass the allowed/blocked policy; nothing falls back to DEV.
"""
from __future__ import annotations

from fnmatch import fnmatch

from .causely import IssueSnapshot


class ProjectNotPermitted(Exception):
    pass


def resolve_project(issue: IssueSnapshot, settings, override: str | None = None) -> str:
    if override:
        if not settings.project_permitted(override):
            raise ProjectNotPermitted(f"project {override} is not in jira.projects.allowed (or is blocked)")
        return override.upper()
    fields = {
        "namespace": issue.namespace,
        "cluster": issue.cluster,
        "entity_name": issue.entity_name,
        "team_label": issue.team,
    }
    for route in settings.jira_cfg.get("routes", []) or []:
        match = route.get("match", {})
        if match and all(fnmatch(fields.get(k, "") or "", str(v)) for k, v in match.items()):
            project = route["project"]
            break
    else:
        project = settings.default_project
    if not settings.project_permitted(project):
        raise ProjectNotPermitted(f"resolved project {project} is not permitted")
    return project.upper()
