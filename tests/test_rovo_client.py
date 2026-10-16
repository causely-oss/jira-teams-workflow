import pytest

from app.rovo import RovoJiraClient


class FakeMcp:
    def __init__(self, responses):
        self.calls, self.responses = [], responses

    async def call(self, tool, args):
        self.calls.append((tool, args))
        r = self.responses.get(tool)
        if isinstance(r, Exception):
            self.responses[tool] = {"key": "DEV-901"}  # succeed on retry
            raise r
        return r


def client(responses):
    c = RovoJiraClient("https://mcp.atlassian.com/v1/mcp", {}, "cloud-1", "https://example.atlassian.net")
    c.mcp = FakeMcp(responses)
    return c


@pytest.mark.asyncio
async def test_find_by_label_uses_jql():
    c = client({"searchJiraIssuesUsingJql": {"issues": [{"key": "DEV-5"}], "isLast": True}})
    assert await c.find_by_issue_id("abc", ["OPS"]) == "DEV-5"
    jql = c.mcp.calls[0][1]["jql"]
    assert 'labels = "causely-issue-abc"' in jql and "project in (OPS)" in jql


@pytest.mark.asyncio
async def test_project_exists_exact_key():
    c = client({"searchJiraIssuesUsingJql": {"issues": []}})
    assert await c.project_exists("ops") is True
    assert 'project = "ops"' in c.mcp.calls[0][1]["jql"]
    c2 = client({"searchJiraIssuesUsingJql": RuntimeError(
        "The value 'OPS' does not exist for the field 'project'.")})
    assert await c2.project_exists("OPS") is False


@pytest.mark.asyncio
async def test_create_retries_without_priority():
    c = client({"createJiraIssue": RuntimeError("Field 'priority' cannot be set")})
    key = await c.create("DEV", "Bug", "s", {"type": "doc", "version": 1, "content": []}, ["l"], "High")
    assert key == "DEV-901"
    assert "priority" not in c.mcp.calls[-1][1]
    assert c.mcp.calls[-1][1]["issueType"] == "Bug"


@pytest.mark.asyncio
async def test_comment_sends_markdown():
    c = client({"addOrEditJiraIssueComment": {}})
    await c.comment("DEV-1", {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "hello"}]},
    ]})
    args = c.mcp.calls[0][1]
    assert args["contentFormat"] == "markdown" and args["commentBody"] == "hello"


@pytest.mark.asyncio
async def test_add_labels_merges_existing():
    c = client({"searchJiraIssuesUsingJql": {"data": {"issues": [
                    {"key": "DEV-1", "fields": {"labels": ["mcp"], "status": {}}}]}},
                "editJiraIssue": {}})
    await c.add_labels("DEV-1", ["causely-issue-x"])
    assert c.mcp.calls[-1][1]["fields"]["labels"] == ["causely-issue-x", "mcp"]


@pytest.mark.asyncio
async def test_probe_rovo_reports_api_token_rejection(monkeypatch):
    from app import rovo

    async def denied(self, tool, args, **kw):
        raise RuntimeError('MCP searchJiraIssuesUsingJql failed: {"error":true,"message":"You don\'t have '
                           'permission to connect via API token. Please ask your organization admin for access."}')

    monkeypatch.setattr(rovo.McpToolClient, "call", denied)
    err = await rovo.probe_rovo("https://mcp.example/v2/mcp", {}, "cloud-1")
    assert "scoped token" in err and "Authentication" in err


@pytest.mark.asyncio
async def test_probe_rovo_ok_and_missing_cloud_id(monkeypatch):
    from app import rovo

    async def ok(self, tool, args, **kw):
        return {"issues": []}

    monkeypatch.setattr(rovo.McpToolClient, "call", ok)
    assert await rovo.probe_rovo("u", {}, "cloud-1") is None
    assert "cloud id" in await rovo.probe_rovo("u", {}, "")


def test_jira_rest_goes_through_gateway_when_cloud_id_known():
    from app.jira import JiraClient, api_base
    assert api_base("https://x.atlassian.net/", "cid") == "https://api.atlassian.com/ex/jira/cid"
    assert api_base("https://x.atlassian.net/", "") == "https://x.atlassian.net"
    c = JiraClient("https://x.atlassian.net", "e", "t", "cid")
    assert str(c.http.base_url).startswith("https://api.atlassian.com/ex/jira/cid")
    assert c.browse_url("OPS-1") == "https://x.atlassian.net/browse/OPS-1"


@pytest.mark.asyncio
async def test_v2_data_wrapper_and_ticket_lookup():
    c = client({"searchJiraIssuesUsingJql": {"data": {"issues": [{"key": "TB-4", "fields": {
        "summary": "s", "labels": ["causely"], "created": "2026-10-08",
        "status": {"name": "Backlog", "statusCategory": {"key": "new"}}}}]}}})
    assert await c.find_by_issue_id("x", ["TB"]) == "TB-4"
    t = await c.get_ticket("TB-4")
    assert t["labels"] == ["causely"] and t["status_category"] == "new"
    assert c.mcp.calls[-1][1]["view"] == "full" and c.mcp.calls[-1][1]["jql"] == 'key = "TB-4"'


@pytest.mark.asyncio
async def test_get_ticket_missing_returns_none():
    c = client({"searchJiraIssuesUsingJql": RuntimeError("Issue does not exist or you do not have permission")})
    assert await c.get_ticket("TB-99999") is None


@pytest.mark.asyncio
async def test_probe_rovo_unwraps_exception_group(monkeypatch):
    from app import rovo

    class FakeResponse:
        status_code = 401

    class FakeHTTPError(Exception):
        response = FakeResponse()

    async def boom(self, tool, args, **kw):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [FakeHTTPError("Client error '401 Unauthorized'")])

    monkeypatch.setattr(rovo.McpToolClient, "call", boom)
    err = await rovo.probe_rovo("u", {}, "cloud-1")
    assert err.startswith("HTTP 401")
    assert "TaskGroup" not in err
