# Portward plugin for Claude Code and Cowork

Choose what Claude sees from your own database. This plugin adds Portward's local MCP server and a skill that
tells Claude when to use it.

## What it runs

The open-source `portward-free` package, version 0.1.0, started by Claude Code over stdio on your machine. The
plugin carries the package itself (`vendor/portward_free-0.1.0-py3-none-any.whl`, the same file published on PyPI),
so the code that runs is the code in this folder. You need
[uv](https://docs.astral.sh/uv/getting-started/installation/); the first start downloads the package's dependencies
(the MCP SDK, sqlglot, DuckDB, openpyxl and others) from PyPI.

## What it reads, sends and keeps

- It reads only the sources you add, read-only: SQLite, CSV and Excel files you name by path, and Postgres and
  MySQL databases. You type a database's password in Portward's panel, a one-time page on your computer or a
  terminal prompt, never in the chat; it is kept in your computer's keychain.
- Query results go only to Claude, and only the rows you allow. Claude sends what it gets to its own AI service,
  as it does with anything in the chat.
- It sends data to Portward's own services only in these cases, each one your choice: anonymous usage counts while
  they are on (Portward asks you first); a table you share with a teammate, encrypted; your account and synced
  history if you sign in to Portward Personal; and a database login you put on the hosted route. The main README
  lists every case and exactly what is sent:
  https://github.com/hamzaelsiesy/portward-mcp#what-leaves-your-computer-and-when
- Privacy policy: https://portward.ai/privacy
- It keeps one local database file in your app-data folder with your sources, settings and a record of every
  pull (the question, the SQL, the row count and a hash of the result, not the rows). Updating the plugin keeps it.

## Install

    /plugin marketplace add hamzaelsiesy/portward-mcp
    /plugin install portward@portward

Or without the plugin: `claude mcp add --scope user --transport stdio portward -- uvx portward-free==0.1.0`.

## License

Apache-2.0. See [LICENSE](LICENSE).
