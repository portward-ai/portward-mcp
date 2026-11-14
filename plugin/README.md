# Portward plugin for Claude Code and Cowork

Choose what Claude sees from your own database. This plugin adds Portward's local MCP server and a skill that
tells Claude when to use it.

## What it runs

`uvx portward-free==0.1.0`, the open-source `portward-free` package from PyPI at that exact version, started by
Claude Code over stdio on your machine. You need [uv](https://docs.astral.sh/uv/getting-started/installation/);
the first start downloads the package and its dependencies (the MCP SDK, sqlglot, DuckDB and openpyxl) from
PyPI.

## What it reads, sends and keeps

- It reads only the sources you add, read-only: SQLite, CSV and Excel files you name by path, and Postgres and
  MySQL databases. You type a database's password in Portward's panel, a one-time page on your computer or a
  terminal prompt, never in the chat; it is kept in your computer's keychain.
- It sends nothing to Portward or anywhere else. Query results go only to Claude: a capped preview, and more rows
  only after you approve them.
- It keeps one local database file in your app-data folder with your sources, settings and a record of every
  pull (the question, the SQL, the row count and a hash of the result, not the rows). Updating the plugin keeps it.

## Install

    /plugin marketplace add hamzaelsiesy/portward-mcp
    /plugin install portward@portward

Or without the plugin: `claude mcp add --scope user --transport stdio portward -- uvx portward-free==0.1.0`.

## License

Apache-2.0. See [LICENSE](LICENSE).
