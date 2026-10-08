"""Rovo in the loop: before opening a ticket, look for one a human already opened.

The label search only finds tickets this bot created. This read-only agent uses the
Rovo MCP server to judge whether an existing open ticket describes the same problem,
and to collect related tickets / postmortems for context. It never writes. The workflow
re-verifies any ticket it names before acting on it.
"""
from __future__ import annotations

import json
import logging

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from .causely import IssueSnapshot

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You decide whether a Causely Issue is already tracked by an existing Jira ticket.

Use searchJiraIssuesUsingJql (cloudId is given) to look for OPEN tickets created in the
lookback window that mention the affected entity, its namespace, the root entity of the
causal chain, or the impacted services. Try 2-3 targeted JQL queries, e.g.
  project in (<projects>) AND statusCategory != Done AND created >= -<N>d AND text ~ "<entity>"
Open promising candidates with getJiraIssue.
{rovo_search_line}

A ticket is a DUPLICATE only if it describes the same live operational problem: same entity
(or its causal root), compatible failure mode (errors, latency, crashes...), and a timeframe
that overlaps the Issue. Text matches are not enough. Engineering tickets about product or
tooling bugs that merely mention the service name are NOT duplicates.

confidence: high = clearly the same incident; medium = plausibly the same; low = unrelated.
List up to 5 related items (tickets or Confluence pages such as past postmortems) that a
responder would want, even if none is a duplicate. Never invent keys or URLs."""

OUTPUT_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "duplicate_key": {"type": ["string", "null"]},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "rationale": {"type": "string"},
            "related": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"ref": {"type": "string"}, "title": {"type": "string"},
                                   "why": {"type": "string"}},
                    "required": ["ref", "title", "why"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["duplicate_key", "confidence", "rationale", "related"],
        "additionalProperties": False,
    },
}


async def find_existing_ticket(issue: IssueSnapshot, *, projects: list[str], model: str,
                               rovo_url: str, rovo_headers: dict[str, str], cloud_id: str,
                               lookback_days: int = 14, use_rovo_search: bool = False) -> dict:
    tools = ["searchJiraIssuesUsingJql", "getJiraIssue"]
    if use_rovo_search:
        tools.append("search")  # Rovo Search: Jira + Confluence, consumes Rovo credits
    rovo_line = ("Then run ONE Rovo `search` for past postmortems or runbooks on this entity."
                 if use_rovo_search else "")
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=SYSTEM_PROMPT.replace("{rovo_search_line}", rovo_line),
        mcp_servers={"rovo": {"type": "http", "url": rovo_url, "headers": rovo_headers}},
        tools=[],
        allowed_tools=[f"mcp__rovo__{t}" for t in tools],
        permission_mode="dontAsk",
        setting_sources=[],
        output_format=OUTPUT_SCHEMA,
        max_turns=10,
        max_budget_usd=0.40,
    )
    context = {
        "cloudId": cloud_id,
        "projects": projects,
        "lookback_days": lookback_days,
        "causely_issue": {
            "name": issue.name, "severity": issue.severity, "started_at": issue.started_at,
            "entity": issue.entity_name, "entity_type": issue.entity_type,
            "namespace": issue.namespace, "cluster": issue.cluster,
            "primary_diagnosis": f"{issue.primary_diagnosis_name} on {issue.primary_diagnosis_entity}",
            "causal_chain": issue.causal_chain, "symptoms": issue.symptoms,
            "impacted_services": issue.impacted_services,
        },
    }
    verdict = None
    async for msg in query(prompt=json.dumps(context, indent=2), options=options):
        if isinstance(msg, ResultMessage):
            verdict = msg.structured_output
            if verdict is None and msg.result:
                try:
                    verdict = json.loads(msg.result)
                except json.JSONDecodeError:
                    pass
            log.info("rovo dedup turns=%s cost=$%s", msg.num_turns, msg.total_cost_usd)
    return verdict or {"duplicate_key": None, "confidence": "low",
                       "rationale": "Rovo check returned no verdict", "related": []}
