"""Wrap mode (#100): Portward in front of a provider's own MCP server (Postgres MCP at launch).

`providers` says which of a server's tools Portward governs; `source` makes the wrapped server a Portward source
(the guard and rewrite apply as on Portward's own connectors); `server` is the MCP server the AI app runs instead
of the original (`portward-free wrap <name>`); `setup` adds one and warns when the original is still set up.
"""
