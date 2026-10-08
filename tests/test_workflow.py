import asyncio

import pytest

from app.causely import IssueSnapshot, ticket_task
from app.settings import Settings
from app.store import Store
from app.workflow import Workflow, extract_issue_id
from tests.fixtures import ISSUE_ID, details


DRAFT = {"title": "Remediate payment gateway errors", "context": "ctx", "description": "desc",
         "requirements": ["fix it"], "acceptance_criteria": ["error rate normal"],
         "additional_notes": "notes"}


class FakeCausely:
    def __init__(self, d, draft=DRAFT):
        self.d = d
        self.draft = draft
        self.tasks, self.alerts = [], []

    async def get_issue(self, issue_id):
        await asyncio.sleep(0.01)  # widen the race window
        return IssueSnapshot.from_details(self.d) if self.d and issue_id == self.d["id"] else None

    async def generate_ticket(self, issue, alert=None):
        self.alerts.append(alert)
        self.tasks.append(issue.id)
        if isinstance(self.draft, Exception):
            raise self.draft
        return self.draft


class FakeJira:
    def __init__(self, existing=None):
        self.tickets = dict(existing or {})  # label -> key
        self.created, self.comments = [], []
        self.human = {}  # tickets a person opened: key -> ticket dict

    def browse_url(self, key):
        return f"https://jira.example/browse/{key}"

    async def find_by_issue_id(self, issue_id, projects):
        await asyncio.sleep(0.01)
        key = self.tickets.get(f"causely-issue-{issue_id}")
        return key if key and key.rsplit("-", 1)[0] in projects else None

    async def create(self, project, issue_type, summary, description, labels, priority):
        key = f"{project}-{100 + len(self.created)}"
        self.created.append({"key": key, "labels": labels, "priority": priority, "summary": summary,
                             "description": description})
        self.tickets[labels[0]] = key
        return key

    async def comment(self, key, body):
        self.comments.append((key, body))

    async def get_ticket(self, key):
        return self.human.get(key)

    async def add_labels(self, key, labels):
        self.human[key]["labels"] += labels
        for l in labels:
            self.tickets[l] = key


class FakeTeams:
    def __init__(self):
        self.cards, self.full = [], []

    async def post_card(self, card):
        self.cards.append(card["body"][0]["text"])
        self.full.append(card)


def make_wf(tmp_path, d=None, jira=None, verdict=None, cooldown=1800, rovo=None, routes=None):
    s = Settings()
    s.config = {"jira": {"projects": {"default": "OPS", "allowed": ["OPS", "DEMO", "DEV"],
                                      "blocked": ["SEC", "SUP"]},
                         "routes": routes or [],
                         "issue_type": "Bug", "min_ticket_severity": "Medium",
                         "evidence_cooldown_seconds": cooldown,
                         "priority_map": {"Medium": "Medium", "Critical": "Highest"},
                         "extra_labels": ["causely"]},
                "alerts": {"dedup_window_seconds": 600}}

    async def triage(alert):
        return verdict or {"mapping_state": "unmapped", "issue_id": None, "summary": "nothing found"}

    wf = Workflow(s, FakeCausely(d if d is not None else details()), jira or FakeJira(), FakeTeams(),
                  Store(str(tmp_path / "s.db")), triage, rovo_check=rovo)
    return wf


@pytest.mark.asyncio
async def test_uc1_creates_then_dedups(tmp_path):
    wf = make_wf(tmp_path)
    r1 = await wf.on_causely_notification({"text": f"https://portal.causely.app/issues/{ISSUE_ID}"})
    assert r1["action"] == "created" and r1["project"] == "OPS"
    assert wf.jira.created[0]["labels"][0] == f"causely-issue-{ISSUE_ID}"
    r2 = await wf.on_causely_notification({"issue": {"id": ISSUE_ID}})
    assert r2["action"] == "no_change"
    assert len(wf.jira.created) == 1 and wf.jira.comments == []


def _adf_text(node) -> str:
    if isinstance(node, dict):
        return node.get("text", "") + " ".join(_adf_text(c) for c in node.get("content", []))
    return ""


@pytest.mark.asyncio
async def test_create_uses_generate_ticket_draft(tmp_path):
    wf = make_wf(tmp_path)
    await wf.handle_issue(ISSUE_ID, trigger="test")
    assert wf.causely.tasks == [ISSUE_ID]
    t = wf.jira.created[0]
    assert t["summary"] == "[Causely] Remediate payment gateway errors"
    body = _adf_text(t["description"])
    for s in ("Requirements", "fix it", "Acceptance criteria", "error rate normal", "Diagnosis"):
        assert s in body  # draft sections plus Causely's own evidence


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [RuntimeError("MCP down"), {"text": "not json"}, None])
async def test_generate_ticket_failure_falls_back(tmp_path, draft):
    wf = make_wf(tmp_path)
    wf.causely.draft = draft
    r = await wf.handle_issue(ISSUE_ID, trigger="test")
    assert r["action"] == "created"
    t = wf.jira.created[0]
    assert t["summary"].startswith("[Causely] ") and "Remediate" not in t["summary"]
    assert "Diagnosis" in _adf_text(t["description"])


@pytest.mark.asyncio
async def test_alert_creates_ticket_with_alert_context(tmp_path):
    wf = make_wf(tmp_path, verdict={"mapping_state": "mapped_entity_symptom", "issue_id": ISSUE_ID,
                                    "summary": "x"})
    alert = {"alerts": [{"status": "firing", "fingerprint": "f1",
                         "labels": {"alertname": "HighErrorRate", "service": "checkout"}}]}
    r = await wf.on_alert("alertmanager", alert)
    assert r[0]["action"] == "created"
    assert wf.causely.alerts[0]["name"] == "HighErrorRate"
    body = _adf_text(wf.jira.created[0]["description"])
    assert "Correlated alert" in body and "alertmanager: HighErrorRate" in body
    assert "alertmanager alert 'HighErrorRate'" in body  # trigger line
    task = ticket_task(IssueSnapshot.from_details(details()), wf.causely.alerts[0])
    assert 'alertmanager alert "HighErrorRate"' in task


def test_ticket_task_carries_evidence():
    issue = IssueSnapshot.from_details(details())
    task = ticket_task(issue)
    assert issue.id in task and issue.primary_diagnosis_name in task
    assert all(s["name"] in task for s in issue.symptoms)


@pytest.mark.asyncio
async def test_race_between_both_triggers_creates_one_ticket(tmp_path):
    wf = make_wf(tmp_path, verdict={"mapping_state": "mapped_entity_symptom", "issue_id": ISSUE_ID, "summary": "x"})
    alert = {"alerts": [{"status": "firing", "fingerprint": "abc",
                         "labels": {"alertname": "HighErrorRate", "service": "checkout"}}]}
    await asyncio.gather(wf.on_causely_notification({"issue_id": ISSUE_ID}),
                         wf.on_alert("alertmanager", alert))
    assert len(wf.jira.created) == 1
    assert len(wf.jira.comments) == 1  # the alert lands as evidence on the single ticket


@pytest.mark.asyncio
async def test_existing_jira_ticket_gets_evidence(tmp_path):
    jira = FakeJira({f"causely-issue-{ISSUE_ID}": "OPS-42"})
    wf = make_wf(tmp_path, jira=jira)
    r = await wf.on_causely_notification({"issue_id": ISSUE_ID})
    assert r["action"] == "commented" and r["jira_key"] == "OPS-42"
    assert jira.created == []


@pytest.mark.asyncio
async def test_escalation_bypasses_cooldown_but_flapping_does_not(tmp_path):
    wf = make_wf(tmp_path)
    await wf.on_causely_notification({"issue_id": ISSUE_ID})
    wf.causely.d = details(severity="Critical")
    assert (await wf.handle_issue(ISSUE_ID, "t"))["action"] == "commented"
    wf.causely.d = details(severity="Medium")
    assert (await wf.handle_issue(ISSUE_ID, "t"))["action"] == "no_change"
    wf.causely.d = details(severity="Critical")  # back up to an already-reported level
    assert (await wf.handle_issue(ISSUE_ID, "t"))["action"] == "no_change"


@pytest.mark.asyncio
async def test_resolution_comments(tmp_path):
    wf = make_wf(tmp_path)
    await wf.on_causely_notification({"issue_id": ISSUE_ID})
    wf.causely.d = details(state="RESOLVED")
    r = await wf.handle_issue(ISSUE_ID, "t")
    assert r["action"] == "commented" and "no longer active" in r["reason"]


@pytest.mark.asyncio
async def test_below_threshold_no_ticket(tmp_path):
    wf = make_wf(tmp_path, d=details(severity="Low"))
    r = await wf.on_causely_notification({"issue_id": ISSUE_ID})
    assert r["action"] == "below_threshold" and wf.jira.created == []
    assert "below ticket threshold" in wf.teams.cards[0]


@pytest.mark.asyncio
async def test_unmapped_alert_posts_to_teams_only(tmp_path):
    wf = make_wf(tmp_path)
    r = await wf.on_alert("datadog", {"title": "CPU high", "alert_id": "1", "tags": "service:web"})
    assert r[0]["action"] == "no_issue" and wf.jira.created == []
    assert "not explained" in wf.teams.cards[0]


@pytest.mark.asyncio
async def test_duplicate_alert_delivery_ignored(tmp_path):
    wf = make_wf(tmp_path)
    p = {"ProblemTitle": "Response time degradation", "PID": "P-1", "State": "OPEN"}
    await wf.on_alert("dynatrace", p)
    r = await wf.on_alert("dynatrace", p)
    assert r[0]["reason"] == "duplicate delivery"


def test_extract_issue_id_variants():
    assert extract_issue_id({"issueId": ISSUE_ID.upper()}) == ISSUE_ID
    assert extract_issue_id({"attachments": [{"text": f"see /issues/{ISSUE_ID} now"}]}) == ISSUE_ID
    assert extract_issue_id({"text": "no id here"}) is None


def test_snapshot_parses_real_payload():
    s = IssueSnapshot.from_details(details())
    assert s.cluster == "demo-cluster" and s.causal_chain[0]["hops"] == 0
    assert "tracey-shop/checkout" in s.impacted_services


# ---------------- Rovo in the loop ----------------

def rovo_says(key, confidence, related=None):
    async def check(issue):
        return {"duplicate_key": key, "confidence": confidence, "rationale": "same 5xx on payment-gw",
                "related": related or []}
    return check


@pytest.mark.asyncio
async def test_rovo_high_confidence_adopts_human_ticket(tmp_path):
    jira = FakeJira()
    jira.human["OPS-77"] = {"key": "OPS-77", "summary": "payment-gw 5xx", "labels": [],
                            "status_category": "indeterminate"}
    wf = make_wf(tmp_path, jira=jira, rovo=rovo_says("OPS-77", "high"))
    r = await wf.on_causely_notification({"issue_id": ISSUE_ID})
    assert r["action"] == "adopted" and r["jira_key"] == "OPS-77"
    assert jira.created == [] and f"causely-issue-{ISSUE_ID}" in jira.human["OPS-77"]["labels"]
    # Next notification finds it through the label, no second Rovo call needed.
    r2 = await wf.on_causely_notification({"issue_id": ISSUE_ID})
    assert r2["jira_key"] == "OPS-77" and r2["action"] == "no_change"


@pytest.mark.asyncio
async def test_rovo_match_on_done_ticket_is_not_adopted(tmp_path):
    jira = FakeJira()
    jira.human["DEV-501"] = {"key": "DEV-501", "summary": "old unrelated bug",
                              "labels": ["mcp"], "status_category": "done"}
    wf = make_wf(tmp_path, jira=jira, rovo=rovo_says("DEV-501", "high"))
    r = await wf.on_causely_notification({"issue_id": ISSUE_ID})
    assert r["action"] == "created"


@pytest.mark.asyncio
async def test_rovo_medium_creates_and_flags(tmp_path):
    jira = FakeJira()
    jira.human["OPS-80"] = {"key": "OPS-80", "summary": "x", "labels": [], "status_category": "new"}
    wf = make_wf(tmp_path, jira=jira, rovo=rovo_says("OPS-80", "medium"))
    r = await wf.on_causely_notification({"issue_id": ISSUE_ID})
    assert r["action"] == "created" and r["possible_duplicate"] == "OPS-80"
    assert "Rovo flagged OPS-80" in str(wf.teams.full[-1])


@pytest.mark.asyncio
async def test_rovo_failure_does_not_block_ticket(tmp_path):
    async def boom(issue):
        raise RuntimeError("Rovo MCP 401: API token auth not enabled")
    wf = make_wf(tmp_path, rovo=boom)
    r = await wf.on_causely_notification({"issue_id": ISSUE_ID})
    assert r["action"] == "created"


# ---------------- Project selection and guardrails ----------------

@pytest.mark.asyncio
async def test_override_picks_project(tmp_path):
    wf = make_wf(tmp_path)
    r = await wf.on_causely_notification({"issue_id": ISSUE_ID}, project="demo")
    assert r["project"] == "DEMO" and wf.jira.created[0]["key"].startswith("DEMO-")


@pytest.mark.asyncio
async def test_route_picks_project(tmp_path):
    wf = make_wf(tmp_path, routes=[{"match": {"cluster": "demo-cluster"}, "project": "DEMO"}])
    r = await wf.on_causely_notification({"issue_id": ISSUE_ID})
    assert r["project"] == "DEMO"


@pytest.mark.asyncio
async def test_dev_allowed_explicitly_but_not_by_default(tmp_path):
    wf = make_wf(tmp_path)
    r = await wf.on_causely_notification({"issue_id": ISSUE_ID})
    assert r["project"] == "OPS"                        # default is not DEV
    (tmp_path / "b").mkdir()
    wf2 = make_wf(tmp_path / "b")
    r2 = await wf2.on_causely_notification({"issue_id": ISSUE_ID}, project="DEV")
    assert r2["action"] == "created" and r2["project"] == "DEV"
    (tmp_path / "c").mkdir()
    wf3 = make_wf(tmp_path / "c", routes=[{"match": {"cluster": "demo-cluster"}, "project": "DEV"}])
    assert (await wf3.on_causely_notification({"issue_id": ISSUE_ID}))["project"] == "DEV"


@pytest.mark.asyncio
async def test_blocked_project_is_refused(tmp_path):
    wf = make_wf(tmp_path)
    r = await wf.on_causely_notification({"issue_id": ISSUE_ID}, project="SUP")
    assert r["action"] == "refused" and wf.jira.created == []


@pytest.mark.asyncio
async def test_rovo_never_adopts_a_blocked_project_ticket(tmp_path):
    jira = FakeJira()
    jira.human["SUP-77"] = {"key": "SUP-77", "summary": "payment-gw 5xx", "labels": [],
                            "status_category": "indeterminate"}
    wf = make_wf(tmp_path, jira=jira, rovo=rovo_says("SUP-77", "high"))
    r = await wf.on_causely_notification({"issue_id": ISSUE_ID})
    assert r["action"] == "created" and r["project"] == "OPS"
    assert jira.human["SUP-77"]["labels"] == [] and jira.comments == []


@pytest.mark.asyncio
async def test_labelled_ticket_outside_allowed_projects_is_ignored(tmp_path):
    jira = FakeJira({f"causely-issue-{ISSUE_ID}": "SUP-42"})
    wf = make_wf(tmp_path, jira=jira)
    r = await wf.on_causely_notification({"issue_id": ISSUE_ID})
    assert r["action"] == "created" and r["project"] == "OPS" and jira.comments == []


def _settings(projects, routes=None):
    s = Settings()
    s.config = {"jira": {"projects": projects, "routes": routes or []}}
    return s


@pytest.mark.parametrize("projects,routes,msg", [
    ({"allowed": ["OPS"]}, None, "default is not set"),
    ({"default": "DEV", "allowed": ["OPS"], "blocked": ["DEV"]}, None, "not allowed or is blocked"),
    ({"default": "CHANGE_ME", "allowed": ["CHANGE_ME"]}, None, "CHANGE_ME"),
    ({"default": "OPS", "allowed": ["OPS"]}, [{"match": {"cluster": "x"}, "project": "SUP"}], "route"),
    ({"default": "OPS", "allowed": ["OPS", "DEV"], "blocked": ["DEV"]}, None, "both allowed and blocked"),
])
def test_startup_validation_rejects_bad_config(projects, routes, msg):
    with pytest.raises(ValueError, match=msg):
        _settings(projects, routes).validate_projects()


def test_startup_validation_accepts_good_config():
    _settings({"default": "OPS", "allowed": ["OPS"], "blocked": ["DEV"]}).validate_projects()
