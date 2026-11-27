# Portward (free, local)

<!-- mcp-name: ai.portward/portward -->

Use Claude or ChatGPT on your own data, and choose what the AI sees. Portward runs on your computer. The AI writes
a query, Portward checks that it only reads, and the rows show to you first. The AI gets only the rows you allow, and
never a table or column you blocked. Every query is recorded in one file on your computer.

It reads SQLite, CSV and Excel files, and Postgres and MySQL databases, all read-only. A database password is kept in
your computer's keychain and never passes through the chat.

New here? [Start on your computer](https://docs.portward.ai/start-on-your-computer/) takes five minutes: install,
try the sample data, block a column, connect your own data. The rest of this page is the reference.

## How rows reach the AI

Each source is in one of two modes, and you can switch at any time:

- **I send rows myself** (the default). In Claude Desktop the rows show in a results panel, and the AI gets only the
  rows and columns you pick and put in the chat. A short note goes into your message box, naming your selection by an
  id and its columns but no values; you press send, and the AI fetches exactly those rows with `get_selection`. In an
  app without panels, Portward asks you in the app's own prompt before the AI gets any rows.
- **Claude gets results.** The AI gets the result straight away, at most 200 rows, without the columns you keep from
  it.

Either way the rows the AI got are listed on your Portward page. A file source can read only the file you added. An
administrator login to a database is refused, and a login that can write is accepted with a warning but only ever
runs read-only queries.

## Install

Claude Desktop: download the extension for your computer (`portward-<version>-macos-arm64.mcpb` for Apple
silicon Macs, `-windows-x64` for Windows) and open it. There is no build for Intel Macs. Claude Desktop asks you to
install it. Nothing else is needed: no Python, no terminal.

For Claude Code and ChatGPT desktop you need [uv](https://docs.astral.sh/uv/getting-started/installation/). Your AI
app starts the server itself.

Claude Code:

    claude mcp add --scope user --transport stdio portward -- uvx portward-free==0.1.0

ChatGPT desktop (and the Codex CLI, which share one config):

    codex mcp add portward -- uvx portward-free==0.1.0

Or add this to `~/.codex/config.toml` yourself:

```toml
[mcp_servers.portward]
command = "uvx"
args = ["portward-free==0.1.0"]
```

Claude Code and Cowork can also install it as a plugin, which adds a skill that tells Claude when to use it:

    /plugin marketplace add portward-ai/portward-mcp
    /plugin install portward@portward

## Update

Change the version in the command (or the plugin) and restart your AI app. Your sources, settings and history live
in your app-data folder, not in the package, so an update keeps them. A newer version migrates the file the first
time it opens it; an older version refuses a file a newer one wrote, and asks you to update.

- macOS: `~/Library/Application Support/Portward/portward.db`
- Windows: `%APPDATA%\Portward\portward.db`
- Linux: `$XDG_DATA_HOME/portward/portward.db` (default `~/.local/share/portward/portward.db`)

`PORTWARD_HOME` overrides the folder. `uvx portward-free==0.1.0 --version` prints the version you run.

## Adding a file

The AI can ask to add a SQLite, CSV or Excel file, but it isn't added until you say yes yourself: in your AI app's
own prompt where it has one, else by pressing Allow under the file's name in the Portward panel or a one-time page
on your computer, else by running the command it gives you in a terminal (it asks you to type the source's name):

    uvx portward-free==0.1.0 add-file <name> <kind> <path>

That command runs only at a terminal you're using (not with its output captured), so an AI that can run commands
can't add a file through it. Adding the same file again under the same name asks nothing.

## Block rules

You can block a table, or one column, from the AI for good (`block`, or your Portward page); removing a block asks
you first. A rule belongs to what the source points at, not to its name: a file is known by its real path and by
the file itself (so a symlink, a hard link or a second name for it is blocked the same way), a database by its host,
port and database name. Rules stay when you remove a source, and apply again when you add it back under any name.
A rule on a Postgres partition or inheritance child also holds on every parent table whose rows include it. If a
blocked column isn't there any more (renamed, most likely), its whole table is kept from the AI until you block the
new name or remove the rule, and `list_sources` says so.

## Database passwords

A database's password never passes through the chat: no tool the AI can call takes one. The AI adds the
connection's host, port, database and user, and Portward asks you for the password where the AI can't see it:

- in a client with panels (Claude Desktop), a small form in the Portward panel;
- in a client that can open a page for a server (MCP URL elicitation), a one-time page on your computer
  (`http://127.0.0.1:<port>/...`), which works once, for 10 minutes;
- otherwise, a command to run in a terminal, which asks for the password without showing what you type:

      uvx portward-free==0.1.0 password <name>

Each way tests the connection first and saves the password in your OS keychain only if it works. To change a
password, ask the AI to update it (`update_password`), or run the same command. Changing a connection's settings
removes its saved password, so a password is never sent to a server you didn't type it for.

The code Portward emails you to confirm your address for sharing goes the same way: you type it in the panel, the
one-time page, or `uvx portward-free==0.1.0 share-code` in a terminal, never in the chat.

## Wrap mode (Personal)

Portward can sit in front of a Postgres MCP server you already use (Postgres MCP Pro), so your profiles apply to
it. Set it up yourself in a terminal; the database URI is read from `DATABASE_URI` or asked for without showing
what you type, and kept in your keychain:

    uvx portward-free==0.1.0 wrap-add <name> -- uvx postgres-mcp

Then, in your AI app, use `uvx portward-free==0.1.0 wrap <name>` in place of the original server. Portward runs the
server itself, in its read-only mode, and shows the AI only its `execute_sql` tool, with every query rewritten so
only the columns your applied profiles grant exist, plus `list_tables`. Its other tools stay hidden (they show
schema, plans or other sessions' queries). Until you apply a profile to `<name>`, the AI sees nothing of it. Setup
warns you when the original server is still set up in Claude Desktop, Claude Code or ChatGPT desktop; check
claude.ai and ChatGPT settings for a connector to the same database yourself.

It can also sit in front of a Snowflake-managed MCP server (`CREATE MCP SERVER`). Give its URL; a programmatic access
token for it is read from `SNOWFLAKE_PAT` or asked for, and kept in your keychain:

    uvx portward-free==0.1.0 wrap-add <name> --provider snowflake --url https://<account>.snowflakecomputing.com/api/v2/databases/<db>/schemas/<schema>/mcp-servers/<server>

Its tool names are whatever the server's specification chose, so Portward tells them apart by what they take: the
SQL tool (`SYSTEM_EXECUTE_SQL`, one `sql` argument) is shown, with the same rewrite; Cortex agents, Cortex Search,
Cortex Analyst and custom function or procedure tools stay hidden, because Portward can't tie what they return to the
columns your profiles grant. Setup lists which tools are governed and which are hidden, and why.

Or in front of Snowflake-Labs/mcp, the open-source server you run with `uvx snowflake-labs-mcp`. Give its command
with your account, user and warehouse (not the password: it is read from `SNOWFLAKE_PAT` or asked for, and passed to
the server as `SNOWFLAKE_PASSWORD`; a programmatic access token works as one):

    uvx portward-free==0.1.0 wrap-add <name> --provider snowflake -- uvx snowflake-labs-mcp --account <account> --user <user> --warehouse <warehouse>

Portward starts it with a service config of its own that turns on only `run_snowflake_query` and allows only SELECT,
so its Cortex, object manager and semantic view tools never run.

## What's counted, stored and sent

It runs on your machine, over stdio, started by your AI app. It reads only the sources you add, read-only, and talks
to them directly. Everything below is checked against the code by `tests/test_readme.py`, so if the code changes
and this page doesn't, the tests fail.

### What leaves your computer, and when

- To your AI app: the replies to the tools it calls. With a results panel the AI gets only the shape of a result
  and the rows you put in the chat (it fetches them with `get_selection`, using the selection id your message
  gives it; the id is random, so it can't fetch rows you didn't put in the chat); without one it gets a capped preview, plus more rows only when you approve them.
  Your AI app sends what it gets to its own AI service, under that app's terms, as it does with anything in the chat.
- To Portward's usage service, `https://signup.portward.ai/v1/usage`: anonymous usage counts (below), only while
  they're on.
- To Portward's share service, only when you share (below): your email and name once, to confirm your email; then,
  for each share, your teammate's email and the encrypted table.
- To Portward's account service (`PORTWARD_ACCOUNT_URL`, default `https://signup.portward.ai`), only if you sign in
  to a Personal account: this computer's token, and while sync is on, your history, saved pulls, notes, rules,
  profiles and AI log summary encrypted here (below). Also, about once an hour while you're signed in, the facts
  your account page shows, which are not encrypted: this week's counts (rows the AI read and asks per AI app, and
  how many asks named something no profile grants), each profile's name, version and grant counts, your connections'
  names and routes, this computer's id and name, and Portward's version. Never table or column names, questions,
  SQL or rows.
- To Portward's hosted engine, only for a database connection that is on the hosted route (Personal), and only
  after you said yes for that connection on this computer: in your AI app's own prompt, or by running
  `uvx portward-free==0.1.0 hosted-allow <name>` in a terminal and typing the source's name. Choosing the hosted
  route on your account page, or the AI asking for it, isn't enough by itself. What goes: that connection's
  host, login and password and the rules, notes and profiles that apply to it, sealed to the engine's key. The
  engine then reads that database for claude.ai or ChatGPT on the web, so on that route rows pass through
  Portward's servers.
- Opening "Need this for your team?" goes to `https://portward.ai/teams`; while usage counts are on it goes through
  `https://signup.portward.ai/v1/usage/team` first, which counts the click for that week and sends you on.

Nothing else is sent anywhere. Every request to a Portward service says it comes from `portward/<version>` and,
like any web request, comes from your IP address.

### Usage counts

Nothing is counted or sent until you have seen Portward's question about usage counts yourself, in one of the
places you see directly: your AI app's own prompt (where it can show one), the results panel, your Portward page,
or a Portward command you run in a terminal. A note in the AI's reply doesn't count, and the AI can't turn them on
before you were asked. Once you've seen the question they're on unless you say no: answer no there, use the panel's
or the page's switch, tell the AI (it calls `set_usage_counts` with `on=false`). Turning them off deletes the counts
kept so far. `PORTWARD_USAGE=off` or `DO_NOT_TRACK=1` in the environment turns them off for good, and you're never
asked. Nothing is sent for an hour after you've seen the question; after that, at most one attempt an hour, from a
tool call. `usage_counts` shows the exact reports that would be sent next.

One report is sent to `https://signup.portward.ai/v1/usage` for each week Portward is used, with exactly these
fields and nothing else:

- `report`: a random id, new every week, so a resend of the same week replaces itself. There is no install id, so
  two weeks' reports can't be linked to each other or to you.
- `week`: the week (for example `2026-W45`).
- `installed_week`: the week this install first ran.
- `pulls`: the number of pulls that week.
- `shares`: the number of shares that week.
- `source_types`: the kinds of source pulled from (`sqlite`, `csv`, `excel`, `postgres`, `mysql`, and
  `wrap-postgres` or `wrap-snowflake` for a Postgres MCP or Snowflake MCP server Portward wraps).
- `ai_clients`: the kinds of AI app used (`claude-desktop`, `claude-code`, `chatgpt`, `other`).
- `profiles`: how many profiles you have (Personal; 0 otherwise).
- `grants_full`: how many of their grants show a column in full.
- `grants_anonymised`: how many show one anonymised.
- `grants_totals`: how many show one as totals only.
- `routes`: the web routes that reached this computer (`direct`, `hosted`), with Personal.

Never your data, queries, questions, source, table or column names, paths, hosts, or any identifier. The counts
wait in the local file (`usage_weeks`) until sent; a finished week is deleted once the service has it.

### On your computer

One database file in your app-data folder (see Update above). It holds:

- `settings`: your settings (keep rows, usage counts and when and where you saw the question and they were last
  sent, the week Portward was
  installed) and, once you confirm it for sharing, your email and name and when your share token expires.
- `connections`: each source's name, kind and settings: a file's path, or a database's host, port, database and
  login name. Never its password.
- `notes`: notes you add to tables and columns.
- `rules`: your block rules.
- `pulls`: one history entry per pull: question, SQL, source, tables, columns, time, row count, a hash of the
  result, and its saved name if you saved it. Not the result rows.
- `kept_rows`: result rows, only if you turn keeping rows on (`keep_rows`, off by default). Encrypted with
  AES-256-GCM under the keychain key below, and deleted after 30 days.
- `conversations`: one entry per run of the server, with the name your AI app gave.
- `ai_responses`: what the AI was sent, for your Portward page ("sent to the AI"): each tool's name, time and
  arguments and its reply (including the rows `get_selection` sent), and the exact text of each 'Put in chat' note
  the panel builds, encrypted with
  AES-256-GCM under the keychain key below (if the keychain can't be used, only the tool name and time are kept).
  Deleted after 30 days.
- `usage_weeks`: usage counts not yet sent (above).
- `shares`: who you shared with, when, from which pull, which columns, how many rows, and when it expires. Never
  the link or its key.
- `sync_records`: with sync on (Personal), what this computer and the sync service last agreed on for each synced
  record: its version there and the same history entry, saved name, note or rule as above.
- `panel_pulls`: with a panel, the rows of your last 50 results (at most 1,000 each) so the panel can show them,
  encrypted with AES-256-GCM under the keychain key below, and deleted after 24 hours. (Some AI apps run two
  Portward processes, one for the AI and one for the panel; this is how the panel reaches the rows.)
- `panel_selections`: with a panel, each selection you put in the chat: the rows and columns you picked (encrypted
  with AES-256-GCM under the keychain key below), the result they are from, and the selection's id only as a SHA-256
  hash. The last 200 are kept, each until the result it is from is no longer held for the panel (24 hours at most).
- `open_asks`: forms open now for a password, the sharing code or a recovery key: what each is for (the source's
  settings as above, or your email), when it expires, and wrong tries so far. Only a hash of the form's one-time
  token, never the token or what you type. Closed once used, and after 10 minutes.
- `share_links`: with a panel, each new share's link (its `#` part is the share's key), who it is for and your name
  for the mail draft, encrypted with AES-256-GCM under the keychain key below, so the share panel can show it for you
  to copy. Kept for 24 hours (never past the share's expiry), the last 100 shares at most. The AI never gets it.
- `profiles`: with Personal, every saved version of each of your profiles: its name, one-line description, the
  tables and columns it grants and at what level, its version, and the account email that first made it and that
  saved this version, with its context: the meanings of what it grants, its named definitions and its links between
  columns, as you approved them (and meanings you imported from a data dictionary, as you confirmed the mapping).
  Never data or passwords.
- `context_drafts`: meanings, definitions and links an AI app drafted for a profile, waiting for you to approve or
  throw away. A draft is never part of a profile or sent to the AI until you approve it, and never synced.
- `assumptions`: what the AI said it assumed when no definition covered a question, with the pull it was made for,
  and the definition you turned it into, if you did. Kept on this computer only: never synced.
- `context_picks`: which profile's words you picked when two profiles on one source describe a column or term
  differently, for exactly those two texts (so you're asked again once either changes).
- `profile_connections`: which sources run on profiles instead of block rules (by name and by what they point at,
  like `rules`).
- `applied_profiles`: which profiles are applied to which source, and the grants that weren't in that source when
  last looked (so one that turns up later is noticed once), when each stops applying (until) and the web AI apps
  it's for, if limited.
- `web_apps`: the web AI apps (OAuth client ids and the names they gave) that reached this computer's web route, so
  you can apply a profile for one by name. Never synced.
- `ai_reads`: the AI log's summary, one entry per read the AI was handed: when, which AI app, the route, the source,
  the profiles and versions that allowed it, the tables and columns with the level each was shown at, the row count
  and a hash of the result. Never the rows. Kept a year; with sync on it goes to your other computers encrypted.
  The proof report (`export_proof_report`, Personal) is made from it.

Besides that file: exports you ask for (CSV or Excel, and proof reports as PDF and CSV, not encrypted) go in its
`exports` folder, and the sample
database, if you use it, is `sample.sqlite` next to it.

### In your keychain

Secrets go in your OS keychain (macOS Keychain, Windows Credential Manager, or Secret Service on Linux), under the
service name `Portward`, never in the file:

- `connection:<name>`: a database source's password.
- `kept-rows-key`: the AES-256 key that encrypts kept rows and what the AI was sent. Made on first use. Without it
  those can't be read.
- `share-token`: the token from confirming your email for sharing (one per computer). It expires on the share
  service after 90 days; a share made in its last 30 days swaps it for a new one first, for up to a year after you
  confirmed your email, and then you confirm it again.
- `account-token`: this computer's token for your Personal account, once you sign in.
- `sync-device-key`: with sync on, this computer's own AES-256 key, which opens the account's sync key that the
  sync service keeps wrapped for this computer.

### Sharing

Sharing is the only time Portward sends your data from your machine (the hosted route above reads your database from Portward's side instead), and only when you share. The pull is run again under your
block rules, encrypted here (AES-256-GCM, a new key for each share), and only the encrypted copy goes to Portward's
share service (`PORTWARD_SHARE_URL`, default `https://share.portward.ai`). The key is in the link's `#` part, which
browsers never send to a server, so the share service never has it, and neither does the AI. With a panel the new
link shows in Portward's share panel: press Copy link and send it to your teammate yourself, or press Open in mail app
for a draft to them in your own mail app. Nothing is sent until you send it. In an AI app without panels a draft to
your teammate opens in your mail app straight away, as before. Your teammate opens it with a code Portward emails
them, and their browser decrypts it. Shares are view only unless you choose to let your teammate download the table
(the AI leaves download off unless you ask for it); then their page has a Download CSV button that makes the file in
their browser. Anyone who can see a table can still copy what's on screen, so view only means no download button,
not that the data can't be copied. Shares expire after 7 days. With Portward Personal a share can last up to 90 days, and on your account page (or by asking the AI)
you can see when each was opened and withdraw one: its link stops working and the encrypted copy is deleted at once.

A share can be up to 5 MB encrypted; a bigger table is refused before anything is sent (share fewer rows or
columns). The share service also limits how many shares one person makes, and how many one teammate is sent, each
hour and each day; when you reach a limit it says when to try again.

The code that confirms your email for sharing works only on the computer that asked for it: that computer sends a
random secret with the request and with the code, and the service keeps only its hash. An address gets at most 3
code emails an hour and takes at most 10 wrong codes a day; after that even the right code waits until the next
day. With Personal, changing a share's expiry from the AI asks you to confirm in your AI app's own prompt (an app
that can't ask sends you to your account's "Your shares" page instead); withdrawing a share happens at once.

#### What a share holds

Encrypted, so only someone with the link can read it:

- `v`: the format version (2; a version 1 share has no `allow_download` and is view only).
- `title`: the pull's title.
- `source`: the source's name.
- `pulled_at`: when it was pulled.
- `columns`: the column names.
- `rows`: the rows.
- `row_count`: the number of rows.
- `truncated`: whether the source had more rows than were pulled.
- `allow_download`: whether your teammate may download it as a CSV (false unless you chose it). It is sealed with the
  rows, so the share service never learns it and it can't be changed after the share is made.
- `anonymised`: the columns you chose to anonymise.
- `totals`: only when you chose totals for a column: which columns, the smallest group shown, how many rows the
  totals come from and how many were left out.

Before it is encrypted you choose, for each column: include (the default), hide (it isn't in the share at all),
anonymise or totals only. Anonymise swaps each value for a code (an HMAC-SHA256 under a random key made for that
share and then thrown away), so the same value has the same code within the share but can't be worked back to the
value, and gets a different code in every other share. Totals only groups the rows by the other columns and shows a
row count and each totals column's sum (or count of values, for a column that isn't numbers); groups of fewer than
5 rows are left out and there is no grand total, so no single row's value can be read off or subtracted out.

#### What the share service keeps

- Your email and name, and a hash of your share token and when it expires, from when you confirmed your email.
  An expired token's entry is deleted 30 days after it expires.
- For each share: your email and name, your teammate's email, the encrypted copy, and when it was made and expires.
  The encrypted copy is deleted when the share expires (at once if you withdraw it), and the rest a day later.
- The time of each open.
- One-time codes, as hashes only: they last 10 minutes and allow 5 tries.

### Sync (Personal)

With sync on, your history (questions, SQL, sources, tables, columns, times, row counts, result hashes), saved pull
names, notes, block rules, Personal's profiles (with their approved context) and the connections they're applied
to, and the AI log's summary (`ai_reads` above) go to the sync service encrypted on your computer, so Portward can't
read them. Not your result rows, kept rows, the values the AI was sent, your connections or any password: those stay
on each computer.

- The first computer makes a random key for your account and a recovery key that it shows you once, in a page on
  that computer (or in a terminal). Write it down: it is the only way to open your synced data on a new computer,
  and Portward can't recover it, because it never leaves your computer.
- Each record is encrypted with AES-256-GCM under the account key, and filed under a keyed hash, so the service
  doesn't see what it is or what it is about. The service keeps the account key only wrapped: under each
  computer's own key from its keychain, and under a key made from your recovery key with Argon2id.
- A new computer asks you for the recovery key where the AI can't see it (the Portward panel, a page on that
  computer, or `portward-free sync-restore` in a terminal). Signing a computer out, or ending it on your account
  page, ends its wrap, so it needs the recovery key again.
- When two computers change the same thing before syncing, nothing is dropped: two versions of a note are both
  kept in it, a block from either computer applies on both, and for a saved name the later naming wins (both
  pulls stay in history). A block removed on one computer stays on the others until you remove it there too.
- A profile syncs as its latest version, and applies on every computer where it's applied. Saved on two computers
  at once, one version applies and the other is kept as a copy, applied nowhere. A connection switched back to
  block rules on one computer stays on profiles on the others until you switch it there too.
- The recovery key page opens through a file on your computer only you can read, so its address never shows in
  the list of running programs, and it shows the key to one viewer only.
- Making a new recovery key (`sync_new_recovery_key`, or `portward-free sync-recovery-key` in a terminal) also makes
  a new account key: the computer re-encrypts everything under it, so the old recovery key and any old copy of the
  service's data open nothing synced after that. Your other computers move to the new key on their next sync with
  nothing to type; a new computer needs the new recovery key. Only a computer that holds the account key can do
  this: a stolen sign-in token alone can't replace your recovery key.
- The sync service keeps at most 100,000 records and 256 MB of encrypted data per account.
- Sync stops when Personal ends; everything on each computer stays. The sync service keeps your encrypted data for
  90 days after Personal ends and then deletes it; if Personal is on again within those 90 days, sync picks up where
  it left off.

#### What the sync service keeps

- Per account: the wrapped account key (one per signed-in computer, and one under your recovery key) and a hash
  that tells computers which key is in use.
- Per record: the encrypted record, its keyed-hash id, a version number and when it last changed.

### Your Personal account

Signing in opens your browser on Portward's account page, which uses WorkOS to sign you in; this computer keeps
only its own token, in the keychain (`account-token`). Signing out on the account page also ends your WorkOS
session in that browser. Ending a computer on the account page signs that computer out and ends its sync key.

### Offers of Personal

Free has no banners, nags or limits. Portward mentions Personal in one line, at most once each, in three moments:
the first time you try to connect claude.ai or ChatGPT on the web (`serve-http` or `serve-direct`), create a
profile, or show a column to the AI anonymised or as totals. Tell the AI you don't want one again and that offer
stops for good on that computer. When Portward can't reach your account to check your plan, it offers nothing.

### When Personal ends

Your profiles freeze: they keep applying exactly as they were (anonymised and totals included), so nothing the AI
couldn't see becomes visible, but they can't be edited, created, applied to new connections or swapped back for
block rules. You can still take a profile off a connection, since that only hides more. Wrapped servers keep
running under them. claude.ai and ChatGPT on the web and sync stop. Your synced data is kept encrypted for 90 days,
then deleted; the hosted route is stopped and the logins it holds are deleted too. When Personal is on again your
profiles unfreeze, and within those 90 days everything synced comes back.

### Download your data or delete your account (Personal)

On your account page (or ask the AI, which opens that page for you), "Download my data" gives you a JSON file of
everything Portward's server holds for your account. Synced records and shares are in it as the encrypted copies the
server keeps, since Portward has no key to read them; your readable history is the one on your computers.

Deleting your account needs you to sign in again on that page and type your email to confirm; the AI can't do it.
It cancels your subscription at once (the rest of the period isn't refunded automatically) and removes from the
server, straight away: your account and email, your synced records and keys, the shares you sent with their
encrypted copies and opens, your sharing confirmations, and every computer's sign-in. History, saved pulls, rules
and settings on your computers are not touched. Shares other people sent to your email address stay until they
expire or their sender withdraws them: they are the sender's, so your address stays on them. Stripe keeps your invoices, and the server's encrypted backups are
deleted after 35 days.

### Support

Questions, or something here doesn't match what you see: email
[support@portward.ai](mailto:support@portward.ai). Need this for your team? See `https://portward.ai/teams`.

## Your Portward page

Ask your AI to "open my Portward page", or run `uvx portward-free==0.1.0 page` in a terminal. It shows your history,
saved pulls, block rules, who you shared with (and when each share expires), settings and, per conversation,
exactly what the AI was sent. You can block and unblock
tables and columns there, and turn keeping rows on or off; unblocking asks you to confirm first.

The page is served from your computer only: on 127.0.0.1, at an address with a random token made for each run, which
Portward hands to your browser (or your terminal) and never to the AI. It answers only to its own address, so a
website can't reach it by pointing its name at your computer, and it accepts changes only from its own forms. It runs
no scripts and loads nothing from the internet. Its one outside link is "Need this for your team?".

## Development
This folder is its own package (`portward-free`, module `portward_free`), with no other Portward code in it.

    cd free
    uv run pytest                      # tests (the install tests build the wheel and run it with uvx)
    uv run portward-free               # the MCP server over stdio
    claude mcp add portward -- uv --directory /path/to/free run portward-free

The Claude Desktop extension is built on the OS and CPU it is for (CI builds both, Apple silicon Mac and Windows: `.github/workflows/extension.yml`). It needs uv, node and a C compiler (Xcode command line tools, or Visual Studio on Windows):

    uv run python mcpb/build_mcpb.py --out dist                # Nuitka-compiled, packed as dist/*.mcpb
    PORTWARD_MCPB=dist/portward-0.1.0-macos-arm64.mcpb uv run pytest tests/test_mcpb.py

The build signs the compiled files only when signing settings are present: `PORTWARD_MACOS_SIGN_IDENTITY` (and
`PORTWARD_NOTARY_PROFILE` to notarize) on macOS, `PORTWARD_WINDOWS_CERT_SHA1` on Windows. Without them it is
unsigned, which is fine for testing but makes macOS and Windows warn on install.

Listing files: `server.json` (MCP registry), `mcpb/manifest.json` (Claude Desktop extension),
`.claude-plugin/marketplace.json` and `plugin/` (Claude Code plugin).
When you change the version in `pyproject.toml`, change it in those files and this README too; the tests fail until
they agree. After a release, replace the wheel in `plugin/vendor/` with the one published to PyPI
and update `plugin/.mcp.json`: the plugin starts the server from that file.
