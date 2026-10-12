"""FastAPI entrypoint.

POST /webhooks/causely                 Causely Issue notification (use case 1)
POST /webhooks/alerts/{source}         datadog | alertmanager | dynatrace | generic (use case 2)
POST /replay/issue/{issue_id}          manual re-run for one Issue (demos, backfill)
POST /replay/alert/{source}            run an alert through use case 2 and wait for the result
GET  /healthz
"""
from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from functools import partial

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request

from .alerts import NORMALIZERS
from .causely import CauselyClient
from .causely_auth import CauselyAuth
from .dryrun import DryRunJira
from .jira import JiraClient
from .rovo import RovoJiraClient, probe_rovo, rovo_headers
from .rovo_agent import find_existing_ticket
from .settings import Settings
from .store import Store
from .teams import TeamsClient
from .triage_agent import triage_alert
from .workflow import Workflow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# httpx logs every request URL at INFO. Some URLs are secrets (Teams Workflows URLs carry a
# signature), so keep httpx to warnings only.
logging.getLogger("httpx").setLevel(logging.WARNING)

_SECRET_QUERY = re.compile(r"((?:^|[?&])(?:token|sig|code)=)[^&\s]+", re.I)


class RedactQuerySecrets(logging.Filter):
    """Mask ?token= (and similar) in uvicorn access log lines."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_SECRET_QUERY.sub(r"\1***", a) if isinstance(a, str) else a
                                for a in record.args)
        return True


logging.getLogger("uvicorn.access").addFilter(RedactQuerySecrets())
log = logging.getLogger("causely-jira-teams")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = Settings.load()
    s.validate_projects()  # no allowed list or a blocked default -> refuse to start
    app.state.settings = s
    r_headers = rovo_headers(s.jira_email, s.jira_api_token, s.atlassian_service_key)
    jira = None
    if s.has_jira_credentials:
        if s.jira_cfg.get("backend", "rovo") == "rovo":
            jira = RovoJiraClient(s.rovo_mcp_url, r_headers, s.atlassian_cloud_id, s.jira_base_url)
        else:
            jira = JiraClient(s.jira_base_url, s.jira_email, s.jira_api_token)
    real_jira = jira
    if s.jira_dry_run:
        jira = DryRunJira(real_jira, s.dry_run_dir)
        log.warning("JIRA_DRY_RUN is on: no Jira writes; tickets go to %s (Jira reads: %s)",
                    s.dry_run_dir, "on" if real_jira else "off, no credentials")
    elif real_jira is None:
        raise RuntimeError("Jira credentials missing: set JIRA_BASE_URL and JIRA_API_TOKEN, "
                           "or JIRA_DRY_RUN=true to test without Jira")
    uses_rovo_backend = real_jira is not None and s.jira_cfg.get("backend", "rovo") == "rovo"
    rc = s.rovo_cfg
    wants_dedup = bool(rc.get("dedup_check", True)) and real_jira is not None  # needs Atlassian access
    rovo_error = (await probe_rovo(s.rovo_mcp_url, r_headers, s.atlassian_cloud_id)
                  if uses_rovo_backend or wants_dedup else None)
    if uses_rovo_backend and rovo_error:
        raise RuntimeError(f"jira.backend is rovo but Rovo MCP is not usable: {rovo_error}. "
                           "Use jira.backend: rest, or fix Rovo access")
    if wants_dedup and rovo_error:
        log.warning("Rovo duplicate check disabled: %s", rovo_error)
        wants_dedup = False
    rovo_check = None
    if wants_dedup:
        projects = s.allowed_projects  # Rovo only searches where the bot may write
        rovo_check = partial(
            find_existing_ticket, projects=projects, model=s.anthropic_model,
            rovo_url=s.rovo_mcp_url, rovo_headers=r_headers, cloud_id=s.atlassian_cloud_id,
            lookback_days=int(rc.get("lookback_days", 14)),
            use_rovo_search=bool(rc.get("use_rovo_search", False)))
    causely_auth = CauselyAuth(s.causely_client_id, s.causely_client_secret, s.causely_token_url)
    app.state.wf = Workflow(
        settings=s,
        causely=CauselyClient(s.causely_mcp_url, causely_auth),
        jira=jira,
        rovo_check=rovo_check,
        teams=TeamsClient(s.teams_workflow_url),
        store=Store(s.state_db),
        triage=partial(triage_alert, model=s.anthropic_model,
                       causely_url=s.causely_mcp_url, causely_auth=causely_auth),
    )
    if real_jira is not None and os.getenv("CHECK_JIRA_ON_START", "true").lower() == "true":
        missing = [p for p in s.allowed_projects if not await jira.project_exists(p)]
        if missing:
            raise RuntimeError(f"Jira projects not found or not creatable by the bot account: {missing}")
    yield


app = FastAPI(title="Causely -> Jira -> Teams", lifespan=lifespan)


def _auth(request: Request, token: str | None, header: str | None) -> None:
    expected = request.app.state.settings.inbound_token
    if expected and expected not in (token, header):
        raise HTTPException(401, "bad webhook token")


def _check_project(request: Request, project: str | None) -> None:
    """Reject a bad ?project= at the door (400) instead of failing later in the background."""
    if project and not request.app.state.settings.project_permitted(project):
        raise HTTPException(400, f"project {project} is not allowed; see GET /projects")


async def _run(coro, label: str):
    try:
        log.info("%s -> %s", label, await coro)
    except Exception:  # never lose an event silently
        log.exception("%s failed", label)


@app.post("/webhooks/causely", status_code=202)
async def causely_webhook(request: Request, bg: BackgroundTasks,
                          token: str | None = Query(None), x_webhook_token: str | None = Header(None),
                          project: str | None = Query(None)):
    _auth(request, token, x_webhook_token)
    _check_project(request, project)
    payload = await request.json()
    bg.add_task(_run, request.app.state.wf.on_causely_notification(payload, project), "causely")
    return {"accepted": True}


@app.post("/webhooks/alerts/{source}", status_code=202)
async def alert_webhook(source: str, request: Request, bg: BackgroundTasks,
                        token: str | None = Query(None), x_webhook_token: str | None = Header(None),
                        project: str | None = Query(None)):
    _auth(request, token, x_webhook_token)
    _check_project(request, project)
    if source not in NORMALIZERS:
        raise HTTPException(404, f"unknown source {source}; use one of {sorted(NORMALIZERS)}")
    payload = await request.json()
    bg.add_task(_run, request.app.state.wf.on_alert(source, payload, project), f"alert:{source}")
    return {"accepted": True}


@app.post("/replay/issue/{issue_id}")
async def replay(issue_id: str, request: Request,
                 token: str | None = Query(None), x_webhook_token: str | None = Header(None),
                 project: str | None = Query(None)):
    _auth(request, token, x_webhook_token)
    _check_project(request, project)
    return await request.app.state.wf.handle_issue(issue_id, trigger="manual replay", project=project)


@app.post("/replay/alert/{source}")
async def replay_alert(source: str, request: Request,
                       token: str | None = Query(None), x_webhook_token: str | None = Header(None),
                       project: str | None = Query(None)):
    """Same as /webhooks/alerts/{source} but synchronous, so you see the verdict and the ticket."""
    _auth(request, token, x_webhook_token)
    _check_project(request, project)
    if source not in NORMALIZERS:
        raise HTTPException(404, f"unknown source {source}; use one of {sorted(NORMALIZERS)}")
    return await request.app.state.wf.on_alert(source, await request.json(), project)


@app.get("/projects")
async def projects(request: Request):
    s = request.app.state.settings
    return {"default": s.default_project, "allowed": s.allowed_projects, "blocked": s.blocked_projects}


@app.get("/healthz")
async def healthz():
    return {"ok": True}
