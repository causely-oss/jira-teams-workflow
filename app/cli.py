"""Setup helpers. Run with the .env loaded:

  python -m app.cli doctor     check config, credentials, and the Jira issue type
  python -m app.cli issues     list active Causely Issues at or above min_ticket_severity

Never prints secret values.
"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx

from .causely import CauselyClient
from .causely_auth import CauselyAuth
from .rovo import probe_rovo
from .settings import SEVERITY_RANK, Settings

OK, WARN, FAIL = "ok  ", "warn", "FAIL"


def _line(status: str, what: str, detail: str = "") -> None:
    print(f"[{status}] {what}" + (f": {detail}" if detail else ""))


def _causely(s: Settings) -> CauselyClient:
    return CauselyClient(s.causely_mcp_url,
                         CauselyAuth(s.causely_client_id, s.causely_client_secret, s.causely_token_url))


async def doctor() -> int:
    s = Settings.load()
    failed = False

    try:
        s.validate_projects()
        _line(OK, "config.yaml", f"default project {s.default_project}, allowed {s.allowed_projects}")
    except (ValueError, FileNotFoundError) as e:
        _line(FAIL, "config.yaml", str(e).replace("\n", " "))
        failed = True

    for var in ("ANTHROPIC_API_KEY", "CAUSELY_CLIENT_ID", "CAUSELY_CLIENT_SECRET"):
        if os.getenv(var):
            _line(OK, var, "set")
        else:
            _line(FAIL, var, "not set")
            failed = True
    if os.getenv("INBOUND_WEBHOOK_TOKEN"):
        _line(OK, "INBOUND_WEBHOOK_TOKEN", "set")
    else:
        _line(WARN, "INBOUND_WEBHOOK_TOKEN", "not set: every request is accepted. Fine on localhost only")

    hosts = {u.split("/")[2] for u in (s.causely_mcp_url, s.causely_token_url, s.causely_portal_url) if "//" in u}
    envs = {"staging" if ".staging." in h else "production" for h in hosts}
    if len(envs) > 1:
        _line(FAIL, "Causely URLs", f"mix staging and production ({sorted(hosts)}); they must match")
        failed = True
    try:
        res = await _causely(s).call("get_issues", {"active_only": True})
        _line(OK, "Causely MCP", f"{s.causely_mcp_url}, {len(res.get('issues') or [])} active Issues")
    except Exception as e:  # noqa: BLE001 - report any failure
        _line(FAIL, "Causely MCP", str(e)[:300])
        failed = True

    if s.jira_dry_run:
        _line(OK, "Jira", f"dry run: no Jira writes, tickets go to {s.dry_run_dir}/")
    if not s.has_jira_credentials:
        if not s.jira_dry_run:
            _line(FAIL, "Jira", "no credentials; set JIRA_BASE_URL + JIRA_API_TOKEN, or JIRA_DRY_RUN=true")
            failed = True
    else:
        failed |= await _check_jira(s)
        failed |= await _check_rovo(s)

    _line(OK if s.teams_workflow_url else WARN, "Teams",
          "webhook set" if s.teams_workflow_url else "TEAMS_WORKFLOW_URL not set: cards are only logged")
    print("\nAll checks passed." if not failed else "\nFix the FAIL lines above, then run doctor again.")
    return 1 if failed else 0


async def _check_jira(s: Settings) -> bool:
    """True if something failed. Uses REST directly: createmeta is the source of truth."""
    issue_type = s.jira_cfg.get("issue_type", "Bug")
    auth = (s.jira_email, s.jira_api_token) if s.jira_api_token else None
    headers = {"Accept": "application/json"}
    if auth is None:
        headers["Authorization"] = f"Bearer {s.atlassian_service_key}"
    async with httpx.AsyncClient(base_url=s.jira_base_url.rstrip("/"), timeout=20,
                                 auth=auth, headers=headers) as http:
        failed = False
        for project in s.allowed_projects:
            r = await http.get(f"/rest/api/3/issue/createmeta/{project}/issuetypes")
            if r.status_code != 200:
                _line(FAIL, f"Jira {project}", f"HTTP {r.status_code}; project missing or no create permission")
                failed = True
                continue
            types = {t["name"]: t["id"] for t in r.json().get("issueTypes", r.json().get("values", []))}
            if issue_type not in types:
                _line(FAIL, f"Jira {project}", f"issue type {issue_type!r} not found; available: {sorted(types)}")
                failed = True
                continue
            r = await http.get(f"/rest/api/3/issue/createmeta/{project}/issuetypes/{types[issue_type]}",
                               params={"maxResults": 100})
            fields = r.json().get("fields", r.json().get("values", []))
            sent = {"project", "issuetype", "summary", "description", "labels", "priority"}
            extra = [f["name"] for f in fields if f.get("required") and f["fieldId"] not in sent]
            if extra:
                _line(FAIL, f"Jira {project}", f"{issue_type} requires fields the bot doesn't send: {extra}. "
                      "Pick another issue_type or make them optional")
                failed = True
            else:
                _line(OK, f"Jira {project}", f"can create {issue_type}")
        return failed


async def _check_rovo(s: Settings) -> bool:
    backend = s.jira_cfg.get("backend", "rovo")
    dedup = bool(s.rovo_cfg.get("dedup_check", True))
    if backend != "rovo" and not dedup:
        _line(OK, "Rovo MCP", "not used (backend rest, rovo.dedup_check false)")
        return False
    err = await probe_rovo(s.rovo_mcp_url, s.rovo_headers, s.atlassian_cloud_id)
    if err is None:
        _line(OK, "Rovo MCP", "reachable" + (", duplicate check on" if dedup else ""))
        return False
    if backend == "rovo":
        _line(FAIL, "Rovo MCP", f"{err}. jira.backend is rovo, so the service won't start; use backend: rest")
        return True
    _line(WARN, "Rovo MCP", f"{err}. The duplicate check will be turned off at startup; "
          "set rovo.dedup_check: false to silence this")
    return False


async def issues() -> int:
    s = Settings.load()
    res = await _causely(s).call("get_issues", {"active_only": True})
    floor = s.min_ticket_rank
    rows = [i for i in res.get("issues") or [] if SEVERITY_RANK.get(i.get("severity", ""), 0) >= floor]
    if not rows:
        print("No active Issues at or above min_ticket_severity. Lower it in config.yaml to test.")
        return 0
    print(f"{'issue id':38} {'severity':9} {'name':22} entity")
    for i in rows:
        print(f"{i['id']:38} {i.get('severity', ''):9} {i.get('name', '')[:22]:22} {(i.get('entity') or {}).get('name', '')}")
    return 0


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    fn = {"doctor": doctor, "issues": issues}.get(cmd)
    if fn is None:
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(fn()))


if __name__ == "__main__":
    main()
