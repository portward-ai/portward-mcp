"""Importing a data dictionary into a profile (#96, Personal): Portward reads the user's file on this computer,
matches its entries onto what the profile grants (dictionary.py) and shows the user that mapping in their client's
own prompt. Only what they confirm joins the profile, as meanings in one new version signed with the account's email.

Nothing is kept from an import the user doesn't confirm: the mapping exists only for the length of the call, so a
draft import never leaves this computer, nor even outlives the prompt. The AI gets counts and where each entry went,
never the descriptions it didn't write, and never the name of an entry that names something the profile hides.

No `from __future__ import annotations` here: the `Resolve(...)` annotations must hold the resolvers themselves.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.types import CallToolResult
from pydantic import BaseModel, Field

from portward_free.clients import client_support
from portward_free.context import ungranted
from portward_free.dictionary import KINDS, DictionaryError, Entry, Matched, matched, read_entries
from portward_free.profile_tools import CANT_ASK, NOT_CONFIRMED, ProfileTools, Step
from portward_free.profiles import Profile, ProfileError
from portward_free.replies import error, reply
from portward_free.sources import SourceError, Table

SHOWN_CHARS = 160  # of each description, in the prompt


class ConfirmMapping(BaseModel):
    confirm: bool = Field(description="Import these descriptions into the profile")
    leave_out: str = Field(default="", description="Numbers of entries to leave out, e.g. 2, 5 (blank: none)")


def register_dictionary_tools(server: MCPServer, profiles: ProfileTools) -> None:
    DictionaryTools(profiles).register(server)


def _numbers(text: str) -> set[int]:
    return {int(n) for n in re.findall(r"\d+", text or "")}


@dataclass
class DictionaryTools:
    profiles: ProfileTools

    def _catalogs(self, profile: Profile) -> list[Sequence[Table]]:
        names = tuple(c.name for c in self.profiles.store.connections())
        found = []
        for source in self.profiles.store.profiles.where_applied(profile, names):
            try:
                found.append(self.profiles.catalog(source)[1])
            except SourceError:
                continue
        return found

    def _safe_names(self, entries: Sequence[Entry], profile: Profile, catalogs: list[Sequence[Table]]) -> list[str]:
        """The names of unmatched entries the AI may see: not one naming a table or column the profile hides."""
        return [e.name for e in entries if not ungranted(e.name, profile.grants, catalogs)
                and not ungranted(e.name.replace(" ", "_"), profile.grants, catalogs)]

    def step(self, profile: str, path: str, mapping: dict[str, str] | None) -> Step:
        email, why = self.profiles.personal_email()
        if email is None:
            return Step(refusal=why)
        try:
            chosen = self.profiles.profile(profile)
            file = Path(path).expanduser()
            found = matched(read_entries(file), chosen.grants, mapping)
        except (ProfileError, DictionaryError) as err:
            return Step(refusal=str(err))
        catalogs = self._catalogs(chosen)
        unmatched = self._safe_names(found.unmatched, chosen, catalogs)
        if not found.mapped:
            names = f": {', '.join(repr(n) for n in unmatched[:30])}" if unmatched else ""
            return Step(refusal=(f"none of the {len(found.unmatched)} entries in {file.name} matched a table or column "
                                 f"profile {chosen.name!r} grants{names}. Pass `mapping` ({{entry: table.column}}) "
                                 "for entries that describe a granted column under another name."))
        return Step(question=self._question(file.name, chosen, found, catalogs),
                    found={"email": email, "profile": chosen, "matched": found, "unmatched": unmatched})

    @staticmethod
    def _question(file: str, profile: Profile, found: Matched, catalogs: list[Sequence[Table]]) -> str:
        lines = []
        for i, (entry, meaning) in enumerate(found.mapped, 1):
            text = meaning.text if len(meaning.text) <= SHOWN_CHARS else meaning.text[:SHOWN_CHARS - 1] + "…"
            source = "" if entry.name.lower() == meaning.about.lower() else f" (from {entry.name!r})"
            lines.append(f"{i}. {meaning.about}{source}: {text}")
        question = (f"Import {len(found.mapped)} descriptions from {file} into profile {profile.name!r} as what "
                    f"its tables and columns mean? Each entry and where it goes:\n" + "\n".join(lines))
        if found.unmatched:
            question += (f"\n\n{len(found.unmatched)} other entries didn't match anything the profile grants and are "
                         "left out.")
        hidden = sorted({n for _, m in found.mapped for n in ungranted(m.text, profile.grants, catalogs)})
        if hidden:
            question += (f"\n\nWarning: some descriptions name {', '.join(hidden)}, which the profile doesn't grant; "
                         "the AI won't be given those descriptions.")
        return question

    def register(self, server: MCPServer) -> None:
        def ask(ctx: Context, profile: str, path: str,
                mapping: dict[str, str] | None = None) -> Elicit[ConfirmMapping] | ConfirmMapping:
            step = self.step(profile, path, mapping)
            if step.refusal or not client_support(ctx).elicitation:
                return ConfirmMapping(confirm=False)
            return Elicit(step.question or "", ConfirmMapping)

        @server.tool(description=(
            "Import a data dictionary the user already has (a spreadsheet, Word document, README or text file on "
            f"this computer: {', '.join(KINDS)}) into a profile, as what its tables and columns mean. Portward reads "
            "the file, matches each entry onto a table or column the profile grants and shows the user the mapping; "
            "only what they confirm is saved. For entries named differently from the columns, pass `mapping` "
            "({entry name: table.column, or table})."))
        def import_dictionary(ctx: Context, profile: str, path: str,
                              answer: Annotated[ElicitationResult[ConfirmMapping], Resolve(ask)],
                              mapping: dict[str, str] | None = None) -> CallToolResult:
            step = self.step(profile, path, mapping)
            if step.refusal:
                return error(step.refusal)
            if not client_support(ctx).elicitation:
                return error(f"Nothing imported: {CANT_ASK}")
            if not (isinstance(answer, AcceptedElicitation) and answer.data.confirm is True):
                return error(f"Nothing imported: {NOT_CONFIRMED}")
            found: Matched = step.found["matched"]  # type: ignore[assignment]
            chosen: Profile = step.found["profile"]  # type: ignore[assignment]
            skipped = _numbers(answer.data.leave_out)
            kept = [m for i, (_, m) in enumerate(found.mapped, 1) if i not in skipped]
            context = chosen.context
            for meaning in kept:
                context = context.with_meaning(meaning)
            saved = self.profiles.store.profiles.save(chosen.saved(str(step.found["email"]), context=context))
            unmatched: list[str] = step.found["unmatched"]  # type: ignore[assignment]
            text = (f"The user confirmed the mapping: {len(kept)} descriptions imported into profile {saved.name!r}, "
                    f"now {saved.signature}.")
            if len(found.unmatched):
                text += f" {len(found.unmatched)} entries matched nothing the profile grants and were left out."
            return reply({"imported": len(kept), "left_out": len(found.mapped) - len(kept),
                          "imported_about": [m.about for m in kept], "not_matched": len(found.unmatched),
                          "not_matched_names": unmatched, "profile": saved.payload()}, text)
