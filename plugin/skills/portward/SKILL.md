---
name: portward
description: Answer questions from the user's own databases through Portward's local, read-only tools. Use when the user asks about data in a database or file they connected to Portward, or wants to connect one.
---

# Answering from the user's data with Portward

Portward is a local MCP server. It reads the sources the user connected, read-only, and returns a capped preview
of each result. Every pull is recorded on the user's machine.

1. Call `list_sources` first to see the sources, their tables and columns. Use only what it returns.
2. If the user wants to connect a file (SQLite, CSV or Excel), call `add_source` with a name, its kind and the
   file's path. For Postgres or MySQL, call `add_source` with the name, kind, host, port, database and user only.
   Never ask for a password or connection string in chat, and if the user types one anyway, don't repeat it or
   pass it to a tool: no tool takes a password. Portward asks the user for it where you can't see it (a form in
   its panel, a one-time page on their computer, or a terminal command the reply names), tests it and saves it in
   their keychain. Pass the reply's instructions on as given, then call `test_source` once they say it's done.
   When a password changed or `test_source` says it is missing or wrong, call `update_password` the same way.
   Sharing works alike: `share_sign_in` has Portward email the user a code, which they type the same way, never
   in the chat (`share_confirm` asks again if the form closed).
3. Answer with `query`: one `SELECT`, plus `question`, the user's question in their own words.
4. You see a capped preview. Prefer narrowing the query (filter, group or aggregate) to asking for more rows; if
   you do need more, use the tool the result names, which asks the user first.
5. When the user wants to see or manage their history, saved pulls, block rules or what you were sent, call
   `open_page`: it opens their Portward page in their browser. You are not given its address, and you can't
   change their rules there.
