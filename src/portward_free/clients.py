"""What the connected client can do, read from the capabilities it declared when it connected.

The tools use this to pick a path: a results panel (MCP Apps) or the no-panel fallback, and the client's own
prompt (elicitation) or a plain-text question for approvals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp.server.apps import client_supports_apps
from mcp.server.mcpserver import Context


@dataclass(frozen=True)
class ClientSupport:
    panel: bool  # renders MCP Apps panels (io.modelcontextprotocol/ui with the app MIME type)
    elicitation: bool  # can ask the user a question for the server (form elicitation)
    url_elicitation: bool = False  # can send the user to a URL the server gives (URL-mode elicitation)


def client_support(ctx: Context[Any]) -> ClientSupport:
    capabilities = ctx.client_capabilities
    elicitation = capabilities.elicitation if capabilities else None
    # An empty elicitation capability means form mode (the spec's default); url-only clients can't show a form.
    can_form = elicitation is not None and (elicitation.form is not None or elicitation.url is None)
    return ClientSupport(panel=client_supports_apps(ctx), elicitation=can_form,
                         url_elicitation=elicitation is not None and elicitation.url is not None)


def client_name(session: Any) -> str | None:
    """The name the AI app gave when it connected (its MCP client info), if any."""
    params = getattr(session, "client_params", None)
    info = getattr(params, "client_info", None)
    return getattr(info, "name", None)
