"""Use case 2 reasoning step: map an external alert to a Causely Issue.

The agent is read-only. It can call Causely triage tools and must return a structured
verdict. All writes (Jira, Teams) happen in workflow.py, in deterministic code.
"""
from __future__ import annotations

import json
import logging

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from .alerts import NormalizedAlert
from .causely_auth import CauselyAuth

log = logging.getLogger(__name__)

READ_ONLY_TOOLS = [
    "get_alerts", "investigate_alert", "name_lookup", "get_diagnoses", "get_diagnosis_details",
    "get_issues", "get_issue_details", "get_service_summary", "get_entity_health",
]

# Condensed from the causely-alert-triage skill decision tree.
SYSTEM_PROMPT = """You map an incoming alert to the Causely Issue that explains it.

Procedure:
1. get_alerts(alert_name_expr=<alert name>, active_only=true). Check mapping_state.
   - mapped_entity_symptom: investigate_alert(alert=...) or get_diagnoses(symptom_ids=...).
   - mapped_entity_only: use the mapped entity.
   - unmapped_*: if a service or namespace is given, name_lookup it and continue from the entity.
     Never invent entities.
2. Find the Issue: get_issues(entity_ids=[...]) for the root-cause entity from the diagnosis
   (follow the causal chain to the root, not the alerting entity). If the alerting entity is a
   downstream symptom, the Issue belongs to the upstream root entity.
3. Return the Issue id exactly as Causely returned it. If no active Issue explains the alert,
   return issue_id null. Do not guess an id.

Be terse. Use as few tool calls as possible."""

OUTPUT_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "mapping_state": {"type": "string"},
            "issue_id": {"type": ["string", "null"]},
            "root_entity": {"type": ["string", "null"]},
            "diagnosis": {"type": ["string", "null"]},
            "summary": {"type": "string", "description": "2-3 sentences, for the Teams card"},
        },
        "required": ["mapping_state", "issue_id", "summary"],
        "additionalProperties": False,
    },
}


async def triage_alert(alert: NormalizedAlert, *, model: str, causely_url: str, causely_auth: CauselyAuth) -> dict:
    token = await causely_auth.access_token()
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={
            "causely": {
                "type": "http",
                "url": causely_url,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        },
        tools=[],                                            # no built-in file/shell tools
        allowed_tools=[f"mcp__causely__{t}" for t in READ_ONLY_TOOLS],
        permission_mode="dontAsk",                           # anything not allowed is denied
        setting_sources=[],                                  # ignore local Claude settings
        output_format=OUTPUT_SCHEMA,
        max_turns=12,
        max_budget_usd=0.50,
    )
    prompt = "Alert to triage:\n" + json.dumps(alert.as_dict(), indent=2)
    verdict: dict | None = None
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, ResultMessage):
            if msg.is_error:
                log.error("triage agent error: %s", msg.errors or msg.result)
            verdict = msg.structured_output
            if verdict is None and msg.result:
                try:
                    verdict = json.loads(msg.result)
                except json.JSONDecodeError:
                    pass
            log.info("triage done turns=%s cost=$%s", msg.num_turns, msg.total_cost_usd)
    return verdict or {"mapping_state": "error", "issue_id": None, "summary": "Triage agent returned no verdict."}
