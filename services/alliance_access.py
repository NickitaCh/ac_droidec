"""Integration of shared configuration with the existing guild-scoped DB API."""

import functools
import inspect
import sqlite3
from contextlib import closing


def refresh_before(*kinds):
    def decorate(func):
        signature = inspect.signature(func)

        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            from services import alliances
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            alliances.refresh_links(bound.arguments["guild_id"], kinds)
            return func(*args, **kwargs)
        return wrapped
    return decorate


def guard_edit(kind, key=None, lookup=None, season=False):
    """Protect every existing writer, including ID-only and bulk-delete APIs."""
    def decorate(func):
        signature = inspect.signature(func)

        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            import database
            from services import alliances
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            values = bound.arguments
            gid = values["guild_id"]
            resource_kind = kind
            if kind == "dc_squad":
                resource_kind = "dc_base" if values["kind"] == "base" else "dc_focused"
            resource_key = values[key] if key else "priority"
            with closing(sqlite3.connect(database.DB_NAME)) as c:
                if lookup:
                    table, column = lookup
                    row = c.execute("SELECT " + column + " FROM " + table + " WHERE guild_id=? AND id=?", (gid, values["req_id"])).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone() else None
                    if row:
                        alliances.assert_editable(gid, resource_kind, row[0])
                if season:
                    # The web's 'clear season' calls the two bulk deleters in
                    # succession. Validate both before either can delete rows.
                    for check_kind, table in (("dc_base", "datacron_requirements"), ("dc_focused", "datacron_focused_requirements")):
                        if c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                            for row in c.execute("SELECT id FROM " + table + " WHERE guild_id=? AND set_id=?", (gid, values["set_id"])):
                                alliances.assert_editable(gid, check_kind, row[0])
            if not season:
                alliances.assert_editable(gid, resource_kind, resource_key)
                if kind == "plate" and values.get("fields", {}).get("plate_name"):
                    alliances.assert_editable(gid, resource_kind, values["fields"]["plate_name"])
            return func(*args, **kwargs)
        return wrapped
    return decorate
