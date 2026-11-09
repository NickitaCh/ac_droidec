import asyncio
import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from cogs import stat_requirements as stats
from services import mod_scheme_image


def row(stat, threshold=300, scheme=None, slot=None, priority="required", skill=None):
    return (1, "leaf", "HUX", stat, ">=", threshold, priority, None, None, None, None,
            skill, slot, None, scheme)


class SchemeTests(unittest.IsolatedAsyncioTestCase):
    def test_snapshot_keeps_common_and_active_variant_and_explicit_alternatives(self):
        rows = [row("Speed"), row(stats.STAT_MOD_PRIMARY, 5, 1, "arrow", skill="5,48"),
                row("Health", 100000, 2), row("Protection", 50000, 1, priority="optional")]
        with patch.object(stats, "_load_char_rows", return_value=(rows, "Хакс", None, [], "")) as load, \
             patch.object(stats.database, "get_mod_secondary_recommendations", return_value=[
                 ("leaf", 0, 5), ("leaf", 1, 41), ("leaf", 2, 1), ("skipped", 0, 28)]):
            model = stats._stats_scheme_model("plate", "HUX", 7, 1, "Быстрый", {"skipped"})
        load.assert_called_once_with("plate", "HUX", 7, skip_plates={"skipped"})
        self.assertEqual(model["scheme"], "Быстрый")
        self.assertEqual(model["slots"]["arrow"], [stats.progress_row_text(rows[1])[1]])
        self.assertEqual(model["requirements"], [stats.progress_row_text(rows[0]), stats.progress_row_text(rows[3])])
        self.assertNotIn("square", model["slots"])
        self.assertEqual(model["secondaries"], ["Скорость", "Атака"])

    def state(self):
        pages, picks = stats._build_stats_pages("Плейт", ["Итог"], [("Хакс", "✅", "Хакс"), ("Рей", "✅", "Рей")],
                                                [("Хакс", "Нормы"), ("Рей", "Нормы")])
        for page in pages:
            if "char" in page:
                page["scheme_model"] = {"base_id": page["char_name"]}
        return dict(pages=pages, picks=picks, index=1, token="123", color=0, footer="", show_scheme=True,
                    lock=asyncio.Lock(), allowed_ids={1}, guild_id=1)

    async def test_navigation_replaces_attachment_and_summary_removes_it(self):
        state = self.state()
        inter = SimpleNamespace(edit_original_response=AsyncMock())
        with patch.object(mod_scheme_image, "render", side_effect=lambda m: m["base_id"].encode()) as render:
            await stats._edit_stats_pager(inter, state)
            first = inter.edit_original_response.call_args.kwargs
            self.assertEqual(first["files"][0].fp.getvalue(), "Хакс".encode())
            state["index"] = 2
            await stats._edit_stats_pager(inter, state)
            second = inter.edit_original_response.call_args.kwargs
            self.assertEqual(second["files"][0].fp.getvalue(), "Рей".encode())
            state["index"] = 0
            await stats._edit_stats_pager(inter, state)
            summary = inter.edit_original_response.call_args.kwargs
            self.assertEqual(summary["attachments"], [])
            self.assertEqual(summary["files"], [])
            self.assertNotIn("attachment://", str([c.to_component_dict() for c in summary["components"]]))
            state["index"] = 1
            await stats._edit_stats_pager(inter, state)
            self.assertEqual(render.call_count, 2)
            state["show_scheme"] = False
            await stats._edit_stats_pager(inter, state)
            self.assertEqual(inter.edit_original_response.call_args.kwargs["files"], [])

    async def test_button_toggle_and_dropdown_edit_original_message(self):
        state = self.state()
        state["show_scheme"] = False
        inter = SimpleNamespace(component=SimpleNamespace(custom_id="stp|123|scheme"),
                                response=SimpleNamespace(defer=AsyncMock()),
                                edit_original_response=AsyncMock(), values=["2"])
        cog = stats.StatRequirementsCog.__new__(stats.StatRequirementsCog)
        with patch.dict(stats._stats_pagers, {"123": state}), \
             patch.object(stats, "_check_stats_pager_access", AsyncMock(return_value=state)), \
             patch.object(mod_scheme_image, "render", side_effect=lambda m: m["base_id"].encode()):
            await cog.on_stats_pager_click(inter)
            self.assertTrue(state["show_scheme"])
            self.assertEqual(inter.edit_original_response.call_args.kwargs["files"][0].fp.getvalue(), "Хакс".encode())
            inter.component.custom_id = "stp|123|pick0"
            await cog.on_stats_pager_pick(inter)
            self.assertEqual(state["index"], 2)
            self.assertEqual(inter.edit_original_response.call_args.kwargs["files"][0].fp.getvalue(), "Рей".encode())
            inter.component.custom_id = "stp|123|scheme"
            await cog.on_stats_pager_click(inter)
            self.assertFalse(state["show_scheme"])
            self.assertEqual(inter.edit_original_response.call_args.kwargs["files"], [])

    def test_image_expands_for_long_requirements_without_inventing_slots(self):
        model = dict(name="Хакс", base_id="HUX", plate="Тестовый плейт", scheme="Вариант 1",
                     slots={"arrow": ["Скорость или Здоровье"]}, requirements=[("Скорость", ">= 300")])
        with patch.object(mod_scheme_image, "_portrait", return_value=None):
            small = Image.open(io.BytesIO(mod_scheme_image.render(model)))
            model["requirements"] *= 30
            large = Image.open(io.BytesIO(mod_scheme_image.render(model)))
        self.assertEqual(small.width, 1000)
        self.assertGreater(large.height, small.height)


if __name__ == "__main__":
    unittest.main()
