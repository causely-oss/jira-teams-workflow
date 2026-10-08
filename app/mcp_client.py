"""Minimal deterministic MCP tool caller (no LLM), shared by Causely and Rovo clients."""
from __future__ import annotations

import json
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


class McpToolClient:
    def __init__(self, url: str, headers: dict[str, str] | None = None):
        self.url = url
        self.headers = headers or {}

    async def call(self, tool: str, args: dict[str, Any], *, headers: dict[str, str] | None = None) -> Any:
        async with streamablehttp_client(self.url, headers=self.headers if headers is None else headers) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool(tool, args)
        text = "".join(c.text for c in res.content if getattr(c, "type", "") == "text")
        if res.isError:
            raise RuntimeError(f"MCP {tool} failed: {text[:500]}")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}
