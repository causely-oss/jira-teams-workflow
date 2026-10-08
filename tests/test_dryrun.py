import pytest

from app.causely import IssueSnapshot
from app.dryrun import DryRunJira, adf_to_markdown
from app.evidence import ticket_description
from tests.fixtures import details
from tests.test_workflow import make_wf


def test_adf_to_markdown_renders_evidence():
    issue = IssueSnapshot.from_details(details())
    md = adf_to_markdown(ticket_description(issue, "https://portal.example", "test"))
    assert "### Diagnosis" in md and "- hop 0:" in md and "](https://portal.example/issues/" in md


@pytest.mark.asyncio
async def test_dry_run_writes_files_not_jira(tmp_path):
    wf = make_wf(tmp_path)
    real = wf.jira
    wf.jira = DryRunJira(real, str(tmp_path / "out"))
    r = await wf.handle_issue(details()["id"], trigger="test")
    assert r["action"] == "created" and r["jira_key"] == "DRYRUN-1"
    assert real.created == []  # nothing reached the (fake) real Jira
    text = (tmp_path / "out" / "DRYRUN-1.md").read_text()
    assert text.startswith("# [Causely] ") and "would create a **Bug** in **OPS**" in text


@pytest.mark.asyncio
async def test_dry_run_without_credentials(tmp_path):
    j = DryRunJira(None, str(tmp_path))
    assert await j.find_by_issue_id("x", ["OPS"]) is None
    assert await j.project_exists("OPS") is True
    await j.comment("DRYRUN-9", {"type": "doc", "content": []})  # no file, no error
