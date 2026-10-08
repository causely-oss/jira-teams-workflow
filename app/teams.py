"""Post Adaptive Cards to a Teams channel via a Workflows (Power Automate) webhook.

Office 365 connector "Incoming Webhooks" are retired; Workflows is the supported path.
Limitation: Workflows posts cannot reply in-thread. Threaded replies need a Bot Framework
bot (proactive messaging with a stored conversation reference). See README, phase 2.
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


class TeamsClient:
    def __init__(self, workflow_url: str):
        self.url = workflow_url
        self.http = httpx.AsyncClient(timeout=20)

    async def post_card(self, card: dict) -> None:
        if not self.url:
            log.info("TEAMS_WORKFLOW_URL unset; card not posted: %s", card["body"][0]["text"])
            return
        payload = {
            "type": "message",
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}],
        }
        r = await self.http.post(self.url, json=payload)
        if r.status_code >= 300:
            # Teams posting must never break the Jira path.
            log.error("Teams post failed %s: %s", r.status_code, r.text[:300])
