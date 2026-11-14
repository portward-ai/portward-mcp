"""Tool results, shared by every tool module."""

from __future__ import annotations

import json
from typing import Any

from mcp.types import CallToolResult, TextContent


def error(message: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=message)], is_error=True)


def reply(payload: dict[str, Any], text: str | None = None) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text or json.dumps(payload, default=str))],
                          structured_content=payload)
