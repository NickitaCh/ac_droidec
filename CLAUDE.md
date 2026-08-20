# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the bot

```
pip install -r requirements.txt
python main.py
```

- Requires a `.env` file with `DISCORD_TOKEN=...` (the only value read from the environment — see Configuration below).
- Requires a `swgoh-comlink` server reachable at `http://localhost:3000` (a separate process, not part of this repo) — most commands and background loops call it and will error/log warnings without it.
- `guild_management.db` (SQLite) is created automatically on startup (`database.init_db()` / `database.init_birthday_table()` run in `on_ready`); it's gitignored and safe to delete to reset state.
- `requirements.txt` is saved as UTF-16 (an editor artifact, not intentional) — if `pip install -r requirements.txt` ever fails to parse it, re-save the file as UTF-8 rather than assuming the dependency list changed.

There is no test suite and no linter/formatter configured in this repo (no `pytest`, no `.flake8`/`pyproject.toml` lint config, no CI). Don't invent test/lint commands — if you add tests or linting, wire them up explicitly and document the command here.

## Architecture

### Entry point and cog loading
`main.py` builds a `GuildManagerBot(commands.Bot)`, attaches shared state as attributes (`bot.comlink`, `bot.guild_roster_cache`, `bot.ally_code`, `bot.allowed_role_ids`, per-feature IDs like `bot.PING_CHANNEL_ID`, `bot.BIRTHDAY_ROLE_ID`, etc.), then on `__main__` loops over every `.py` file in `cogs/` and calls `bot.load_extension(f"cogs.{name}")`. Every cog file therefore needs a module-level `def setup(bot): bot.add_cog(...)` — that's the extension point for adding a new feature module. Slash commands are registered as guild-scoped (`test_guilds=[TUSA_GUILD_ID, SNG_GUILD_ID]`) rather than global, which is why they update instantly during development.

### Configuration convention
Configuration is split into two scopes — **this is a deliberate departure from the old single-guild-era convention, made 2026-08-13 as part of an in-progress multi-tenancy migration** (see below):
- **Operator / bot-wide config** (secrets, cross-guild tuning that has no per-guild meaning) stays exactly as before: hardcoded module-level constants in `main.py`, attached to `GuildManagerBot.__init__` as `bot.<ATTR>` attributes. Only `DISCORD_TOKEN` comes from `.env`. Examples: `COMLINK_URL`, `ALLOWED_USER_IDS` (bot-owner bypass), `N_LIMIT`, `PLAYER_STATS_SYNC_HOURS`. **Keep following this pattern for new operator-level values** — add a constant near the top of `main.py` and attach it in `GuildManagerBot.__init__`.
- **Per-guild config** (identity + Discord channel/role IDs for each SWGOH guild the bot serves — ally code, officer/member role IDs, ping/birthday/TB channel and role IDs, TB ping schedule) now lives in the `guilds` table in `guild_management.db` (`database.py`: `_ensure_guilds_table`, `get_guild_config`/`get_all_guild_configs`/`create_guild`/`update_guild_config`), **not** as `main.py` constants — this is so a new SWGOH guild can be onboarded (via the planned web dashboard) without a code deploy. **Do not add new per-guild settings as `main.py` constants — add a column to `guilds` instead.**

**Migration status:** schema + a one-time seed (`database.seed_default_guild(...)`, called from `main.py::on_ready`, inserts the current single guild — AbsoluteChaos — as `guilds.id=1` from the legacy `main.py` constants, idempotent/no-op once the table is non-empty) exist, but most cog code doesn't read from `guilds` yet — `bot.ally_code`, `bot.ALLOWED_ROLE_IDS`, `bot.TB_PLAN_CHANNEL_ID` etc. are still what's actually used at runtime today. A `guild_resolver.py` module (resolves which `guilds.id` a Discord interaction belongs to, via role membership) and a per-cog cutover are the next steps of this migration — if you're picking this work back up, don't assume it's finished just because the table exists.

Legacy inconsistency, predates the multi-tenancy work and is still true for any code path not yet migrated: the officer role ID `1153753506772164629` is defined twice in `main.py` (`ALLOWED_ROLE_IDS` and `ALLOWED_OFFICER_ROLE_ID`, same value) and then re-hardcoded as a raw literal directly inside `cogs/birthday.py` (`ALLOWED_ROLE_ID`), `cogs/datacron_requirements.py`, `cogs/guild_events.py` (`@commands.has_any_role(1153753506772164629)`), and `cogs/self_registration.py` (`OFFICER_ROLE_ID`) instead of referencing the bot attribute. Similarly, `COMLINK_URL = "http://localhost:3000"` is duplicated as a literal inside `cogs/tasks.py` and `cogs/guild_events.py` (which builds its own separate `SwgohComlink` instance rather than using `bot.comlink`). If you change a shared ID or URL, grep for the literal across `cogs/` rather than assuming one constant controls it.

### Permission model
Two independent layers:
1. **Global gate** in `main.py`: `@bot.check` (prefix `!` commands) and `@bot.slash_command_check` (slash commands) both call `_check_allowed_role`, which allows `bot.allowed_user_ids` unconditionally or requires a role in `bot.allowed_role_ids`. It reads `author._roles` (raw interaction payload) instead of `author.roles` specifically to work around `Interaction.guild` sometimes being `None` for uncached guilds — don't "simplify" this back to `.roles` without re-reading the comment in `main.py`.
2. **Per-command gate**: some slash commands additionally decorate with `@commands.has_any_role(<id>)` (birthday, datacron requirements, TB report commands) as a second, redundant check using the hardcoded literal described above.

`on_slash_command_error` in `main.py` catches `MissingRole`/`MissingAnyRole`/`CheckFailure` and replies with a standard "access denied" ephemeral message; other exceptions are just printed.

### Database access
`database.py` wraps raw `sqlite3` — there is no ORM and no shared connection: nearly every function opens its own `sqlite3.connect(DB_NAME)`, runs one statement, commits, and closes. Several cogs (`register.py`, `tasks.py`, `violations.py`) also open their own `sqlite3.connect(database.DB_NAME)` directly instead of going through `database.py`, so DB access is not fully centralized — check both `database.py` and the calling cog when tracing a query.

Table creation is split between two styles:
- Core tables (`user_mapping`, `position_warns`, `tasks`, `game_units`) are created once via `database.init_db()` at startup.
- Newer feature tables (`birthdays`, `bot_state`, `tb_events`/`tb_player_summary`/`tb_player_detail`, `tb_planet_names`/`tb_event_planet_names`, `datacron_requirements`, `datacron_focused_requirements`) are lazily created by a private `_ensure_*_table(cursor)` helper called at the top of every function that touches that table — safe to call repeatedly (`CREATE TABLE IF NOT EXISTS`). Schema migrations for these tables are done ad hoc by attempting `ALTER TABLE ... ADD COLUMN` and swallowing `sqlite3.OperationalError` if the column already exists (see `_ensure_datacron_requirements_table`) — follow this pattern for new columns on existing tables rather than writing a migration runner.

`bot_state` is a generic key/value table (`get_bot_state`/`set_bot_state`) used for small values that must survive a bot restart, currently just the last-reported TB result fingerprint (`cogs/guild_events.py`) so the bot doesn't re-announce a TB report after a restart.

### Shared guild roster cache
`bot.guild_roster_cache` is the central player-name ↔ ally-code lookup used across `violations.py`, `tasks.py`, `guild_events.py`, and `datacron_requirements.py` (for autocomplete and resolving a chosen player to their ally code). It's a `HybridCache` (defined in `cogs/violations.py`, a `dict` subclass) that behaves as `{player_name: ally_code}` but also supports `.get(ally_code)` to reverse-resolve a name — used by `tasks.py` when only the ally code is known. It's populated/refreshed hourly by `ViolationsCog.update_roster_cache` (a `tasks.loop`), which pulls the live guild roster from Comlink, overwrites the `user_mapping` table, and rebuilds the cache; on Comlink failure it falls back to whatever is cached in `user_mapping` and drops the loop interval to 5 minutes until the network recovers.

### Comlink integration
`bot.comlink` is a single `SwgohComlink` instance created once in `main.py`; most cogs use it via `self.bot.comlink`. `cogs/guild_events.py` is the exception — it constructs its own second `SwgohComlink(url="http://localhost:3000")` instance rather than reusing `bot.comlink`. Blocking Comlink calls must be wrapped in `asyncio.to_thread(...)` inside async handlers — an unwrapped call blocks the entire event loop (all guilds, all commands) for the duration of the HTTP request, which showed up in production as spurious "Interaction took more than 3 seconds to respond" failures on unrelated slash commands whenever `violations.py`'s `update_roster_cache` or `tasks.py`'s `tasks_audit_loop` happened to be mid-run (both are hourly loops that call Comlink once per guild member). Fixed 2026-08-19 by wrapping their `get_player`/`get_guild` calls in `to_thread` too — every Comlink call site in the codebase should now go through `to_thread`; keep new ones consistent with this.

`tasks.py`'s unit-catalog sync (`_do_units_synchronization`) bypasses the `swgoh-comlink` library for the `/data` (full game data) endpoint and calls it via raw `aiohttp` POST instead, because the library didn't support the required request shape — it fetches `latestGamedataVersion` from `get_metadata()`/`/metadata` first, then POSTs to `/data` to pull the full unit catalog into the `game_units` table (used for the datacron/task unit autocomplete). This sync runs once at startup and then hourly.

### Background task pattern
Every cog with recurring work follows the same shape: a `disnake.ext.tasks.loop(...)` started in `Cog.__init__`, cancelled in `cog_unload`, and gated by a `@loop.before_loop` that awaits `bot.wait_until_ready()`. Time-based loops that fire at a specific wall-clock minute (`rotation_ping.py`, `birthday.py`, the TB order post in `guild_events.py`) poll every 30 seconds and use an in-memory "last sent minute/key" guard to avoid double-sending within the same run — this guard resets on restart, so a restart landing exactly on a trigger minute could in theory double-send; the one exception is the TB-completion report in `guild_events.py`, which instead fingerprints the result (`hashlib.sha1` over the JSON) and persists the fingerprint via `database.set_bot_state`, so it survives restarts without re-announcing.

### Territory Battle (TB) report subsystem — `cogs/guild_events.py`
This is the most intricate module. Comlink's raw stat keys (`mapStatId`, e.g. `power_zone_tb3_mixed_phase03_conflict01_bonus`) are decoded via regex (`TB_ZONE_RE`, `TB_ROUND_RE`) into phase/branch(conflict)/action/value; per-phase totals come from Comlink's own `*_round_N` keys (treated as authoritative) while zone keys provide the planet-level breakdown. Comlink never exposes real planet names, only a branch code (`01`/`02`/`03` = Light/Dark/Mixed + a `bonus` zone) — real names are recovered by an `on_message` listener that parses officer announcements ("Восход Империи — N этап") in `TB_PLAN_CHANNEL_ID`, mapping planet name → branch via the hardcoded `TB_PLANET_CONFLICT` table, and storing the result in `tb_planet_names` (live) / `tb_event_planet_names` (snapshotted per completed TB, for history/compare commands). `/тб_отчет план` is the manual fallback when a planet isn't in `TB_PLANET_CONFLICT` yet. History is capped at the last 3 TB events (`database.prune_tb_events`, `TB_HISTORY_KEEP`).

### Datacron requirements — `cogs/datacron_requirements.py`
Builds an in-memory catalog of datacron seasons/abilities from `comlink.get_game_data(items=<DatacronDefinitions flag>)` plus RUS_RU localization strings, cached on `bot.datacron_cache` and refreshed every 12 hours. Guild officers register required datacron builds (`/дк_требования добавить`, both "base" 3/6/9-tier datacrons and "focused"/specialized single-character datacrons) which are matched against a player's live roster (`/дк_требования проверить`) or reported guild-wide. Requirements carry a `priority` (`required`/`optional`/`useful`) that drives both display grouping and the guild-wide compliance report (which only flags players missing a `required` item).

Each "base" requirement can additionally carry up to 5 stat sub-requirements (`datacron_requirement_stats` table, `stat_id` + `min_value` percent) — `stat_id` is one of the 8 numeric `UnitStat` IDs a base datacron can actually roll (`DATACRON_STAT_LABELS`; confirmed against live Comlink player data + the full `datacronAffixTemplateSet` catalog, not guessed), and a player's owned datacron's raw `affix[].statValue` is divided by `DATACRON_STAT_SCALE` (1,000,000) to get the percent — the same scale/IDs already used for mod math in `stat_engine.py`. This is a **soft check**: `/дк_требования проверить` still counts the requirement as satisfied purely by ability match (3/6/9 tiers); a stat below its threshold only downgrades the display icon from ✅ to ⚠️ with a "recommend re-rolling" line, it never flips a match to ❌ and is not reflected in the guild-wide summary counts (which stay ability-match-only, per `_match_counts`).

### Discord size-limit handling
Because reports can be large, two conventions repeat across cogs: (1) chunk long text into multiple embeds under Discord's 4096-char `description` limit (`_lines_to_embeds`/`_build_priority_embeds` in `datacron_requirements.py`) or multiple 2000-char messages (`_chunk_message` in `guild_events.py`), and (2) for very long tabular reports, write to a temp file and upload as a Discord attachment instead of a message (`GuildEvents.send_as_file`, used for all `/тб_отчет` outputs).

### Slash command and autocomplete conventions
Multi-action features (`/дк_требования`, `/тб_отчет`) use a slash command group with `.sub_command(...)` rather than several flat top-level commands. Autocomplete callbacks are module-level async functions (not cog methods) so they can be imported and reused across cogs — e.g. `autocomplete_players` is defined once in `cogs/violations.py` and imported into `tasks.py`, `datacron_requirements.py`, and `guild_events.py` for player selection.
