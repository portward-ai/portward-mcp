"""The local database's schema, as an ordered list of migrations.

The file's `PRAGMA user_version` is the number of migrations applied. Opening a file runs the ones it is missing,
all in one transaction, so an update never loses history or settings and a failed migration changes nothing.

To change the schema, append a migration. Never edit or reorder one that has shipped: files in the wild were built
from it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence

Migration = Callable[[sqlite3.Connection], None]


def _run(db: sqlite3.Connection, *statements: str) -> None:
    """One statement at a time: `executescript` would commit the migration's transaction halfway."""
    for statement in statements:
        db.execute(statement)


def _v1_connections_and_settings(db: sqlite3.Connection) -> None:
    _run(
        db,
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        # settings is JSON and never holds secrets (those go in the OS keychain)
        "CREATE TABLE connections (name TEXT PRIMARY KEY, kind TEXT NOT NULL, settings TEXT NOT NULL, "
        "created_at TEXT NOT NULL)",
    )


def _v2_pulls(db: sqlite3.Connection) -> None:
    _run(
        db,
        # tables and columns are JSON lists; keep_rows says whether result rows are kept (encrypted, a later ticket)
        "CREATE TABLE pulls (id TEXT PRIMARY KEY, parent_id TEXT REFERENCES pulls (id), question TEXT NOT NULL, "
        "sql TEXT NOT NULL, source TEXT NOT NULL, tables TEXT NOT NULL, columns TEXT NOT NULL, "
        "created_at TEXT NOT NULL, row_count INTEGER NOT NULL, result_hash TEXT NOT NULL, saved_name TEXT, "
        "keep_rows INTEGER NOT NULL DEFAULT 0)",
        "CREATE INDEX pulls_by_time ON pulls (created_at)",
    )


def _v3_notes(db: sqlite3.Connection) -> None:
    _run(
        db,
        # column_name '' is the table's own note
        "CREATE TABLE notes (source TEXT NOT NULL REFERENCES connections (name) ON DELETE CASCADE, "
        "table_name TEXT NOT NULL, column_name TEXT NOT NULL DEFAULT '', note TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, PRIMARY KEY (source, table_name, column_name))",
    )


def _v4_ai_log(db: sqlite3.Connection) -> None:
    _run(
        db,
        # one conversation per server run; client is the name the AI client gave
        "CREATE TABLE conversations (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, client TEXT)",
        # every tool response the AI got: what was asked and sent (arguments, text, content, structured content) is
        # one AES-GCM blob under the keychain key (portward_free.kept); with no usable keychain, `sealed` is NULL and
        # `withheld` says why, so result values never sit in this file in plaintext
        "CREATE TABLE ai_responses (id INTEGER PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations "
        "(id), created_at TEXT NOT NULL, tool TEXT NOT NULL, is_error INTEGER NOT NULL, sealed BLOB, withheld TEXT)",
        "CREATE INDEX ai_responses_by_conversation ON ai_responses (conversation_id, id)",
    )


def _v5_saved_and_kept(db: sqlite3.Connection) -> None:
    _run(
        db,
        # a saved name points at one pull; saving the name again moves it
        "CREATE UNIQUE INDEX pulls_by_saved_name ON pulls (saved_name) WHERE saved_name IS NOT NULL",
        # one pull's result rows, AES-GCM sealed (portward_free.kept); the key is in the OS keychain, never here
        "CREATE TABLE kept_rows (pull_id TEXT PRIMARY KEY REFERENCES pulls (id) ON DELETE CASCADE, "
        "sealed BLOB NOT NULL)",
    )


def _v6_rules(db: sqlite3.Connection) -> None:
    _run(
        db,
        # one block rule: a whole table (column '') or one column of it; names match case-insensitively
        "CREATE TABLE rules (source TEXT NOT NULL, table_name TEXT NOT NULL, column_name TEXT NOT NULL DEFAULT '', "
        "created_at TEXT NOT NULL)",
        "CREATE UNIQUE INDEX rules_by_name ON rules (source, lower(table_name), lower(column_name))",
    )


def _v7_usage_weeks(db: sqlite3.Connection) -> None:
    _run(
        db,
        # anonymous usage counts (portward_free.usage), one row per ISO week the tool was used while they're on;
        # `report` is random and new each week, so two weeks' reports can't be linked. Deleted once sent.
        "CREATE TABLE usage_weeks (week TEXT PRIMARY KEY, report TEXT NOT NULL, pulls INTEGER NOT NULL DEFAULT 0, "
        "shares INTEGER NOT NULL DEFAULT 0, source_types TEXT NOT NULL DEFAULT '[]', "
        "ai_clients TEXT NOT NULL DEFAULT '[]')",
    )


def _v8_shares(db: sqlite3.Connection) -> None:
    _run(
        db,
        # the sender's own record of each share (#50): who it went to and when; never the link's key or the rows.
        # columns is the JSON list of the columns shared
        "CREATE TABLE shares (id TEXT PRIMARY KEY, pull_id TEXT REFERENCES pulls (id), recipient TEXT NOT NULL, "
        "created_at TEXT NOT NULL, expires_at TEXT NOT NULL, columns TEXT NOT NULL, row_count INTEGER NOT NULL)",
        "CREATE INDEX shares_by_time ON shares (created_at)",
    )


def _v9_sync(db: sqlite3.Connection) -> None:
    _run(
        db,
        # encrypted sync (#55): when a pull got its saved name, so two computers' names for it settle on the latest
        "ALTER TABLE pulls ADD COLUMN saved_at TEXT",
        # what this computer and the sync service last agreed on, per record: the record's version there, its content
        # there (`remote`, JSON) and what this computer held for it then (`local`, JSON; NULL: nothing here, e.g. a
        # note for a source this computer doesn't have yet). The same data as the rest of this file: never sent.
        "CREATE TABLE sync_records (record_id TEXT PRIMARY KEY, kind TEXT NOT NULL, version INTEGER NOT NULL, "
        "remote TEXT NOT NULL, local TEXT)",
    )


def _v10_rule_targets(db: sqlite3.Connection) -> None:
    _run(
        db,
        # what the source a rule was made on points at (storage/targets.py: a file's real path and its inode, a
        # database's host, port and name), so any source pointing at the same data gets the rule, whatever its name
        "ALTER TABLE rules ADD COLUMN target TEXT",
        "ALTER TABLE rules ADD COLUMN target_alt TEXT",
        "CREATE INDEX rules_by_target ON rules (target)",
        "CREATE INDEX rules_by_target_alt ON rules (target_alt)",
    )


def _v11_panel_state(db: sqlite3.Connection) -> None:
    _run(
        db,
        # What the panels need from the server, here rather than in memory because a client can start two server
        # processes on one app-data folder (Claude Desktop: one answers the AI's calls, the other the panel's).
        # An open ask for a secret: its token only as a SHA-256 hash, its public id, what it is for (`ask_key`), and
        # how to rebuild it (`kind` and its JSON `params`, never secret: a source's settings, an email address)
        "CREATE TABLE open_asks (token_hash TEXT PRIMARY KEY, id TEXT NOT NULL UNIQUE, ask_key TEXT NOT NULL, "
        "kind TEXT NOT NULL, params TEXT NOT NULL, expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0)",
        "CREATE INDEX open_asks_by_key ON open_asks (ask_key)",
        # the rows a recent pull lent the results panel, AES-GCM sealed like kept rows (portward_free.kept)
        "CREATE TABLE panel_pulls (pull_id TEXT PRIMARY KEY, held_at REAL NOT NULL, expires_at REAL NOT NULL, "
        "sealed BLOB NOT NULL)",
    )


def _v12_panel_selections(db: sqlite3.Connection) -> None:
    _run(
        db,
        # what the user put in the chat from the results panel (portward_free.selections): the picked rows and columns
        # of one panel pull, AES-GCM sealed like kept rows, for the AI to fetch with get_selection. The selection's id
        # (what the chat message gives the AI) only as a SHA-256 hash; it expires with or before the pull it is from
        "CREATE TABLE panel_selections (id_hash TEXT PRIMARY KEY, pull_id TEXT NOT NULL, held_at REAL NOT NULL, "
        "expires_at REAL NOT NULL, sealed BLOB NOT NULL)",
    )


def _v13_share_links(db: sqlite3.Connection) -> None:
    _run(
        db,
        # a new share's link for the share panel to show (portward_free.sharing.links): the link (its # part is the
        # share's key), who it is for and the sender's name for the mail draft, AES-GCM sealed like kept rows, under
        # the share's public id; it expires within a day, never after the share
        "CREATE TABLE share_links (share_id TEXT PRIMARY KEY, held_at REAL NOT NULL, expires_at REAL NOT NULL, "
        "sealed BLOB NOT NULL)",
    )


def _v14_profiles(db: sqlite3.Connection) -> None:
    _run(
        db,
        # Personal's profiles (portward_free.profiles): every saved version, whole; the latest is the profile. Grants
        # are JSON [{schema, table, column, level}]; made_by and changed_by are the account emails that saved them
        "CREATE TABLE profiles (id TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL, "
        "description TEXT NOT NULL, made_by TEXT NOT NULL, changed_by TEXT NOT NULL, grants TEXT NOT NULL, "
        "saved_at TEXT NOT NULL, PRIMARY KEY (id, version))",
        # the connections that run on profiles (hide by default) instead of block rules, matched like rules by name
        # or by what the connection points at (storage/targets.py)
        "CREATE TABLE profile_connections (source TEXT PRIMARY KEY, target TEXT, target_alt TEXT, "
        "switched_at TEXT NOT NULL)",
        # which profiles are applied to which connection; `missing` is the JSON list of grant names that weren't in
        # the data when last looked, so one that turns up later is noticed once
        "CREATE TABLE applied_profiles (source TEXT NOT NULL, profile_id TEXT NOT NULL, target TEXT, target_alt TEXT, "
        "missing TEXT NOT NULL, applied_at TEXT NOT NULL, PRIMARY KEY (source, profile_id))",
    )


def _v15_profile_context(db: sqlite3.Connection) -> None:
    _run(
        db,
        # a profile's context (#92 on): JSON [{schema, table, column, meaning}], column null for the table's own
        "ALTER TABLE profiles ADD COLUMN context TEXT NOT NULL DEFAULT '[]'",
    )


def _v16_context_drafts(db: sqlite3.Connection) -> None:
    _run(
        db,
        # a profile's context becomes an object, so named definitions (#95) and later links sit beside the meanings:
        # JSON {meanings: [{schema, table, column, meaning}], definitions: [{term, text}]}
        "UPDATE profiles SET context = json_object('meanings', json(context), 'definitions', json('[]')) "
        "WHERE json_type(context) = 'array'",
        # context the AI drafted, waiting for the user's approval: never part of a profile until approved, never sent
        # to the AI. `body` is JSON {meaning: {...}} or {definition: {...}}; drafted_by is the AI app's client name
        "CREATE TABLE context_drafts (id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, body TEXT NOT NULL, "
        "drafted_by TEXT NOT NULL, drafted_at TEXT NOT NULL)",
        # the user's pick when applied profiles describe one column or term differently: `candidates` fingerprints
        # the texts picked between (profile id and text hash each), so the pick holds until either changes
        "CREATE TABLE context_picks (subject TEXT NOT NULL, candidates TEXT NOT NULL, chosen TEXT NOT NULL, "
        "picked_at TEXT NOT NULL, PRIMARY KEY (subject, candidates))",
    )


def _v17_ai_reads(db: sqlite3.Connection) -> None:
    _run(
        db,
        # the AI log's summary (#98): one row per read, never the rows themselves. profiles is JSON [{name, version}],
        # columns JSON [{table, column, level}]; origin is the computer (or gateway) that wrote it
        "CREATE TABLE ai_reads (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, origin TEXT NOT NULL, app TEXT, "
        "route TEXT NOT NULL, connection TEXT NOT NULL, profiles TEXT NOT NULL, columns TEXT NOT NULL, "
        "row_count INTEGER NOT NULL, result_hash TEXT NOT NULL)",
        "CREATE INDEX ai_reads_by_time ON ai_reads (created_at)",
    )


def _v18_usage_routes(db: sqlite3.Connection) -> None:
    # #103: which web routes reached this computer that week ("direct", "hosted"), for the anonymous usage counts
    _run(db, "ALTER TABLE usage_weeks ADD COLUMN routes TEXT NOT NULL DEFAULT '[]'")


def _v19_assumptions(db: sqlite3.Connection) -> None:
    _run(
        db,
        # what the AI assumed when no definition covered a question (#96), one row per assumption, with the pull it
        # was made for; defined_as is the term of the definition the user turned it into. Never synced
        "CREATE TABLE assumptions (id TEXT PRIMARY KEY, pull_id TEXT NOT NULL, source TEXT NOT NULL, "
        "question TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL, defined_as TEXT)",
        "CREATE INDEX assumptions_by_time ON assumptions (created_at)",
    )


def _v20_ai_read_context(db: sqlite3.Connection) -> None:
    # #125: the approved context each read's answer relied on, JSON [{kind, about, profile, version}]
    _run(db, "ALTER TABLE ai_reads ADD COLUMN context TEXT NOT NULL DEFAULT '[]'")


def _v21_application_terms(db: sqlite3.Connection) -> None:
    _run(
        db,
        # #134: an application can end (UTC ISO time; null: until taken off) and be limited to some web AI apps
        # (JSON list of OAuth client ids; empty: every AI app)
        "ALTER TABLE applied_profiles ADD COLUMN until TEXT",
        "ALTER TABLE applied_profiles ADD COLUMN apps TEXT NOT NULL DEFAULT '[]'",
        # the web AI apps seen on this computer's web routes, so the user can pick one by name; never synced
        "CREATE TABLE web_apps (client_id TEXT PRIMARY KEY, name TEXT, last_seen TEXT NOT NULL)",
    )


def _v22_profile_row_rules(db: sqlite3.Connection) -> None:
    # #132: a profile's row rules, JSON [{table, schema?, where}] where `where` is row_rules' structured tree, never SQL
    _run(db, "ALTER TABLE profiles ADD COLUMN rows TEXT NOT NULL DEFAULT '[]'")


def _v23_ai_read_chain(db: sqlite3.Connection) -> None:
    # #135: each entry's link to the one its computer wrote before, and its own hash ('' before chaining)
    _run(db, "ALTER TABLE ai_reads ADD COLUMN prev TEXT NOT NULL DEFAULT ''",
         "ALTER TABLE ai_reads ADD COLUMN hash TEXT NOT NULL DEFAULT ''")


MIGRATIONS: Sequence[Migration] = (
    _v1_connections_and_settings,
    _v2_pulls,
    _v3_notes,
    _v4_ai_log,
    _v5_saved_and_kept,
    _v6_rules,
    _v7_usage_weeks,
    _v8_shares,
    _v9_sync,
    _v10_rule_targets,
    _v11_panel_state,
    _v12_panel_selections,
    _v13_share_links,
    _v14_profiles,
    _v15_profile_context,
    _v16_context_drafts,
    _v17_ai_reads,
    _v18_usage_routes,
    _v19_assumptions,
    _v20_ai_read_context,
    _v21_application_terms,
    _v22_profile_row_rules,
    _v23_ai_read_chain,
)

LATEST_VERSION = len(MIGRATIONS)


class NewerFileError(Exception):
    """The file was written by a newer version of Portward than this one."""


def schema_version(db: sqlite3.Connection) -> int:
    return int(db.execute("PRAGMA user_version").fetchone()[0])


def migrate(db: sqlite3.Connection, migrations: Sequence[Migration] = MIGRATIONS) -> int:
    """Bring the file up to `len(migrations)`; returns the version it was at."""
    found = schema_version(db)
    target = len(migrations)
    if found > target:
        raise NewerFileError(f"this local database is version {found}, newer than this Portward ({target}); update Portward")
    if found == target:
        return found
    db.execute("BEGIN IMMEDIATE")
    try:
        for migration in migrations[found:]:
            migration(db)
        db.execute(f"PRAGMA user_version = {target:d}")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return found
