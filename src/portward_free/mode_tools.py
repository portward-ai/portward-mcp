"""The tool that switches a source between Free's two modes (#117, modes.py).

'I send rows myself' only narrows what the AI gets, so it applies straight away. 'Claude gets results' lets the AI
receive rows without the user picking them, and the AI writes the tool calls, so it applies only on the user's own yes
in the client's prompt, never from the web (as with removing a block rule).
"""

from __future__ import annotations

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, InputRequiredResult

from portward_free import modes
from portward_free.confirm import confirm_in_client
from portward_free.replies import error, reply
from portward_free.runtime import Runtime

CONFIRM_KEY = "confirm_send_mode"


def register_mode_tools(server: MCPServer, rt: Runtime) -> None:
    store = rt.store

    @server.tool(description=(
        "Switch how a source's results reach you. 'results' (Claude gets results): you get the rows straight away, "
        f"at most {modes.AI_ROWS}, minus the columns the user keeps from you; Portward asks the user to confirm. "
        "'pick' (I send rows myself): you get only the result's shape until the user sends rows. Only call this "
        "when the user asks for it."))
    async def set_send_mode(source: str, mode: str, ctx: Context) -> CallToolResult | InputRequiredResult:
        if store.connection(source) is None:
            return error(f"no source named {source!r}; call list_sources")
        if mode not in modes.MODES:
            return error(f"mode must be {modes.names(modes.MODES)}")
        if store.profiles.on_profiles(source):
            return error(f"{source} runs on profiles: its profiles decide what you get, so it has no mode")
        done = reply({"source": source, "mode": mode, "label": modes.LABELS[mode]},
                     f"{source} is now set to '{modes.LABELS[mode]}'.")
        if mode == modes.PICK or store.send_mode(source) == mode:
            store.set_send_mode(source, mode)
            return done
        if rt.local_only:
            return error(f"{source} stays on '{modes.LABELS[modes.PICK]}': that can only be changed on the computer")
        if ctx.client_capabilities is None or ctx.client_capabilities.elicitation is None:
            return error(f"{source} stays on '{modes.LABELS[modes.PICK]}': this app can't ask the user to confirm; "
                         "they can switch it on the Portward page")
        question = (f"Let the AI get results from {source} straight away (up to {modes.AI_ROWS} rows each, minus the "
                    "columns you keep from it), without you picking rows first?")
        answer = await confirm_in_client(ctx, question, CONFIRM_KEY, f"send-mode:{source}")
        if isinstance(answer, InputRequiredResult):
            return answer
        if not answer:
            return error(f"{source} stays on '{modes.LABELS[modes.PICK]}': the user didn't confirm")
        store.set_send_mode(source, mode)
        return done
