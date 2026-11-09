import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import database
from services import alliances as a


class AllianceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = patch.object(database, "DB_NAME", str(Path(self.tmp.name) / "test.db"))
        self.db.start()
        database.init_db()
        self.gids = [database.create_guild(name="Гильдия " + str(n), ally_code=str(123456789 + n), discord_guild_id=str(100 + n), member_role_id="", officer_role_id="") for n in range(4)]
        self.owner, self.member, self.other, self.outside = self.gids
        self.aid = a.create(self.owner, "Наш альянс", "officer")
        self.join(self.member)

    def tearDown(self):
        self.db.stop()
        self.tmp.cleanup()

    def join(self, gid):
        a.invite(self.owner, gid, "officer")
        invite_id = a.page(gid)["incoming"][0]["id"]
        a.respond(gid, invite_id, True)

    def plate(self, name="Плейт", value=300, guild=None):
        gid = guild or self.owner
        database.create_stat_plate(name, "Описание", "officer", guild_id=gid)
        return database.add_stat_requirement(name, "TEST", "Speed", ">=", value, "required", None, "Комментарий", "officer", guild_id=gid)

    def plate_share(self, **kwargs):
        return a.share(self.owner, "plate", "Плейт", [self.member], **{"allow_copy": True, "allow_link": True, **kwargs})

    def test_only_one_alliance_and_no_implicit_sharing(self):
        with self.assertRaises(a.AllianceError):
            a.create(self.member, "Другой", "x")
        self.plate()
        self.assertEqual(a.page(self.member)["available"], [])

    def test_invite_permission_and_wrong_recipient(self):
        with self.assertRaises(a.AllianceError):
            a.invite(self.member, self.other, "x")
        a.invite(self.owner, self.other, "x")
        iid = a.page(self.other)["incoming"][0]["id"]
        with self.assertRaises(a.AllianceError):
            a.respond(self.outside, iid, True)
        a.membership(self.owner, self.member, "permissions", can_invite=True)
        a.invite(self.member, self.outside, "x")
        self.assertEqual(len(a.page(self.outside)["incoming"]), 1)

    def test_expired_duplicate_and_cancelled_invites(self):
        a.invite(self.owner, self.other, "x")
        with self.assertRaises(a.AllianceError):
            a.invite(self.owner, self.other, "x")
        iid = a.page(self.other)["incoming"][0]["id"]
        with closing(sqlite3.connect(database.DB_NAME)) as c:
            c.execute("UPDATE alliance_invites SET expires_at='2000-01-01' WHERE id=?", (iid,))
            c.commit()
        with self.assertRaises(a.AllianceError):
            a.respond(self.other, iid, True)
        a.invite(self.owner, self.other, "x")
        iid = a.page(self.other)["incoming"][0]["id"]
        a.cancel_invite(self.owner, iid)
        self.assertEqual(a.page(self.other)["incoming"], [])

    def test_foreign_share_and_guessed_resource_rejected(self):
        self.plate()
        sid = self.plate_share()
        for mode in ("copy", "link"):
            with self.assertRaises(a.AllianceError):
                a.consume(self.outside, sid, mode, "X", "x")
        self.join(self.other)
        with self.assertRaises(a.AllianceError):
            a.preview(self.other, sid)
        with self.assertRaises(a.AllianceError):
            a.share(self.member, "plate", "Плейт", [self.owner], True, True)

    def test_copy_is_independent_and_does_not_overwrite(self):
        rid = self.plate()
        sid = self.plate_share()
        a.consume(self.member, sid, "copy", "Копия", "x")
        database.update_stat_requirement_fields(rid, self.owner, threshold_value=400)
        self.assertEqual(database.get_stat_requirements("Копия", "TEST", self.member)[0][5], 300)
        with self.assertRaises(a.AllianceError):
            a.consume(self.member, sid, "copy", "Копия", "x")
        self.assertEqual(database.get_stat_requirements("Копия", "TEST", self.member)[0][5], 300)

    def test_link_updates_and_blocks_all_plate_writers(self):
        rid = self.plate()
        sid = self.plate_share()
        a.consume(self.member, sid, "link", "Связанный", "x")
        database.update_stat_requirement_fields(rid, self.owner, threshold_value=400)
        local = database.get_stat_requirements("Связанный", "TEST", self.member)[0]
        self.assertEqual(local[5], 400)
        for write in (
            lambda: database.delete_stat_requirement(local[0], self.member),
            lambda: database.update_stat_requirement_fields(local[0], self.member, threshold_value=1),
            lambda: database.rename_stat_plate("Связанный", "Подмена", self.member),
            lambda: database.set_character_scheme_label("Связанный", "TEST", 1, "X", self.member),
            lambda: self.plate("Связанный", guild=self.member),
            lambda: database.add_mod_secondary_recommendation("Связанный", "TEST", 5, guild_id=self.member),
            lambda: database.delete_mod_secondary_recommendation("Связанный", "TEST", 5, guild_id=self.member),
        ):
            with self.assertRaises(a.AllianceError):
                write()
        with self.assertRaises(a.AllianceError):
            a.share(self.member, "plate", "Связанный", [self.owner], True, True)

    def test_secondary_recommendations_copy_and_link_without_checks(self):
        self.plate()
        database.add_mod_secondary_recommendation("Плейт", "TEST", 5, guild_id=self.owner)
        sid = self.plate_share()
        a.consume(self.member, sid, "copy", "Копия", "x")
        a.consume(self.member, sid, "link", "Линк", "x")
        database.add_mod_secondary_recommendation("Плейт", "TEST", 41, guild_id=self.owner)
        self.assertEqual({r[2] for r in database.get_mod_secondary_recommendations("Копия", "TEST", self.member)}, {5})
        self.assertEqual({r[2] for r in database.get_mod_secondary_recommendations("Линк", "TEST", self.member)}, {5, 41})
        database.delete_mod_secondary_recommendation("Плейт", "TEST", 5, guild_id=self.owner)
        self.assertEqual({r[2] for r in database.get_mod_secondary_recommendations("Линк", "TEST", self.member)}, {41})
        self.assertEqual(database.count_stat_requirements_by_plate("Линк", self.member), 1)

    def test_revision_skips_snapshot_when_source_has_not_changed(self):
        self.plate()
        sid = self.plate_share()
        a.consume(self.member, sid, "link", "Линк", "x")
        with patch.object(a, "_snapshot", side_effect=AssertionError("Unchanged source must not be serialized again")):
            database.get_stat_requirements("Линк", "TEST", self.member)

    def test_revoke_freezes_and_detach_unlocks(self):
        rid = self.plate()
        sid = self.plate_share()
        a.consume(self.member, sid, "link", "Копия", "x")
        a.revoke(self.owner, sid)
        database.update_stat_requirement_fields(rid, self.owner, threshold_value=500)
        local = database.get_stat_requirements("Копия", "TEST", self.member)[0]
        self.assertEqual(local[5], 300)
        database.update_stat_requirement_fields(local[0], self.member, threshold_value=200)
        self.assertEqual(a.page(self.member)["links"], [])

    def test_selective_revoke_and_copy_only(self):
        self.plate()
        sid = self.plate_share(allow_link=False)
        with self.assertRaises(a.AllianceError):
            a.consume(self.member, sid, "link", "X", "x")
        self.join(self.other)
        a.share(self.owner, "plate", "Плейт", [self.member, self.other], True, True)
        a.consume(self.member, sid, "link", "X", "x")
        a.share(self.owner, "plate", "Плейт", [self.other], True, True)
        self.assertEqual(a.page(self.member)["links"], [])
        self.assertEqual(a.page(self.member)["available"], [])

    def test_all_members_includes_future_members_only_when_selected(self):
        self.plate()
        sid = a.share(self.owner, "plate", "Плейт", [], True, True, all_members=True)
        self.join(self.other)
        self.assertEqual(a.page(self.other)["available"][0]["id"], sid)

    def test_leave_and_owner_transfer(self):
        self.plate()
        sid = self.plate_share()
        a.consume(self.member, sid, "link", "Копия", "x")
        with self.assertRaises(a.AllianceError):
            a.membership(self.owner, self.owner, "leave")
        a.membership(self.owner, self.member, "transfer")
        a.membership(self.owner, self.owner, "leave")
        self.assertEqual(a.page(self.member)["links"], [])
        self.assertEqual(a.page(self.member)["alliance"]["owner_guild_id"], self.member)
        self.assertTrue(database.get_stat_plate("Копия", self.member))

    def test_modular_plate_keeps_leaf_squads_and_variants(self):
        self.plate("Источник")
        database.add_mod_secondary_recommendation("Источник", "TEST", 41, guild_id=self.owner)
        database.set_character_scheme_label("Источник", "TEST", 1, "Скорость", self.owner)
        database.set_stat_plate_squad("Источник", [{"base_id": "TEST", "min_relic": 7}], self.owner)
        database.set_stat_character_squad("Источник", "TEST", [{"base_id": "OTHER", "min_relic": 5}], self.owner)
        database.create_stat_plate("Модульный", "", "x", guild_id=self.owner, modular=True)
        database.set_stat_plate_components("Модульный", [("Источник", "")], "x", self.owner)
        sid = a.share(self.owner, "plate", "Модульный", [self.member], True, True)
        a.consume(self.member, sid, "link", "Наш модульный", "x")
        self.assertTrue(database.is_stat_plate_modular("Наш модульный", self.member))
        row = database.get_stat_requirements("Наш модульный", "TEST", self.member)[0]
        leaf = row[1]
        self.assertNotEqual(leaf, "Источник")
        self.assertEqual(database.get_character_scheme_labels(leaf, "TEST", self.member)[1], "Скорость")
        self.assertEqual(database.get_stat_plate_squads(self.member)[leaf][0]["min_relic"], 7)
        self.assertEqual(database.get_stat_plate_squads(self.member)[leaf + "\x1fTEST"][0]["min_relic"], 5)
        self.assertEqual(database.get_mod_secondary_recommendations("Наш модульный", "TEST", self.member), [(leaf, 0, 41)])
        with self.assertRaises(a.AllianceError):
            database.delete_stat_plate(leaf, self.member)

    def test_datacron_links_keep_local_id_stats_squad_and_quantity(self):
        rid = database.add_datacron_requirement(22, "Пак", "L3", "L6", "L9", "", "x", guild_id=self.owner, quantity=3)
        database.set_datacron_requirement_stats(rid, [(1, 12.5)], self.owner)
        database.set_datacron_requirement_squad("base", rid, [{"base_id": "TEST"}], self.owner)
        sid = a.share(self.owner, "dc_base", str(rid), [self.member], True, True)
        local = int(a.consume(self.member, sid, "link", "", "x"))
        with closing(sqlite3.connect(database.DB_NAME)) as c:
            c.execute("UPDATE datacron_requirements SET quantity=5 WHERE guild_id=? AND id=?", (self.owner, rid))
            c.commit()
        row = database.get_datacron_requirement(local, self.member)
        self.assertEqual(row[0], local)
        self.assertEqual(row[11], 5)
        self.assertEqual(database.get_datacron_requirement_stats(local, self.member), [(1, 12.5)])
        self.assertIn(("base", local), database.get_datacron_requirement_squads_by_set(22, self.member))
        with self.assertRaises(a.AllianceError):
            database.delete_datacron_requirements_by_set(22, self.member)
        with self.assertRaises(a.AllianceError):
            database.set_datacron_requirement_stats(local, [], self.member)

    def test_focused_datacron_copy(self):
        rid = database.add_datacron_focused_requirement(22, "Пак", "TEST", 9, "", "x", guild_id=self.owner)
        sid = a.share(self.owner, "dc_focused", str(rid), [self.member], True, True)
        local = int(a.consume(self.member, sid, "copy", "", "x"))
        self.assertEqual(database.get_datacron_focused_requirement(local, self.member)[3:5], ("TEST", 9))

    def test_clear_season_does_not_partially_delete_before_focused_link_denial(self):
        owned = database.add_datacron_requirement(22, "Наш", "L3", "L6", "L9", "", "x", guild_id=self.member)
        focused = database.add_datacron_focused_requirement(22, "Чужой", "TEST", 9, "", "x", guild_id=self.owner)
        sid = a.share(self.owner, "dc_focused", str(focused), [self.member], True, True)
        a.consume(self.member, sid, "link", "", "x")
        with self.assertRaises(a.AllianceError):
            database.delete_datacron_requirements_by_set(22, self.member)
        self.assertIsNotNone(database.get_datacron_requirement(owned, self.member))

    def test_omicron_replace_requires_confirmation_and_protects_writes(self):
        database.set_guild_omicron_priority(self.owner, ["skill2", "skill1"])
        database.set_guild_omicron_requirement_rules(self.owner, "Правила", "x")
        database.set_guild_omicron_priority(self.member, ["own"])
        sid = a.share(self.owner, "omicrons", "priority", [self.member], True, True)
        with self.assertRaises(a.AllianceError):
            a.consume(self.member, sid, "link", "", "x")
        a.consume(self.member, sid, "link", "", "x", replace=True)
        self.assertEqual(database.get_guild_omicron_priority(self.member), [("skill2", 0), ("skill1", 1)])
        database.set_guild_omicron_priority(self.owner, ["new"])
        self.assertEqual(database.get_guild_omicron_priority(self.member), [("new", 0)])
        with self.assertRaises(a.AllianceError):
            database.set_guild_omicron_requirement_rules(self.member, "Подмена")

    def test_source_deletion_and_disabled_owner_freeze(self):
        self.plate()
        sid = self.plate_share()
        a.consume(self.member, sid, "link", "Копия", "x")
        database.delete_stat_plate("Плейт", self.owner)
        self.assertTrue(database.get_stat_plate("Копия", self.member))
        self.assertEqual(a.page(self.member)["links"], [])

    def test_disabled_owner_stops_updates_and_preview(self):
        self.plate()
        sid = self.plate_share()
        a.consume(self.member, sid, "link", "Копия", "x")
        database.update_guild_config(self.owner, is_active=0)
        with self.assertRaises(a.AllianceError):
            a.preview(self.member, sid)
        self.assertTrue(database.get_stat_plate("Копия", self.member))
        self.assertEqual(a.page(self.member)["links"], [])

    def test_failed_modular_import_rolls_back_everything(self):
        self.plate("Источник")
        database.create_stat_plate("Сборный", "", "x", self.owner, modular=True)
        database.set_stat_plate_components("Сборный", [("Источник", "")], "x", self.owner)
        sid = a.share(self.owner, "plate", "Сборный", [self.member], True, True)
        real_apply = a._apply
        def fail_on_child(c, gid, kind, snapshot, key=None, actor="alliance"):
            if key != "Копия":
                raise RuntimeError("Simulated write failure")
            return real_apply(c, gid, kind, snapshot, key, actor)
        with patch.object(a, "_apply", side_effect=fail_on_child):
            with self.assertRaises(RuntimeError):
                a.consume(self.member, sid, "link", "Копия", "x")
        self.assertIsNone(database.get_stat_plate("Копия", self.member))
        self.assertEqual(a.page(self.member)["links"], [])


if __name__ == "__main__":
    unittest.main()
