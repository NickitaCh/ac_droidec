import base64
import io
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from services import tw_def_image, tw_def_notify
from tg_bot import notifier


def player(count=1):
    return {"name": "Игрок", "ally_code": "123456789", "discord_id": "123", "entries": [
        {"zone": "1 верх", "zone_key": "1t", "squad": "Лега. Рей с ОмиФалкрам", "unit_ids": ["GLREY"],
         "units": ["Рей", "Бен Соло"], "datacrons": ["ДК Кодекс, ур. 9 — 3: Защита; 6: Атака; 9: Призыв"]}
        for _ in range(count)]}


class MapTests(unittest.IsolatedAsyncioTestCase):
    def test_map_grows_without_dropping_assignments(self):
        with patch.object(tw_def_image, "_portrait", return_value=None):
            small = Image.open(io.BytesIO(tw_def_image.render("Гильдия", "111", player())))
            large = Image.open(io.BytesIO(tw_def_image.render("Гильдия", "111", player(8))))
        self.assertEqual(small.width, 1440)
        self.assertGreater(large.height, small.height)

    def test_message_structure(self):
        text = "\n".join(tw_def_notify.format_dm("Гильдия", "111", [player()]))
        self.assertIn("**1 верх — Лега. Рей с ОмиФалкрам: Рей, Бен Соло**", text)
        self.assertIn("• 3 ур.: Защита\n• 6 ур.: Атака\n• 9 ур.: Призыв", text)

    async def test_telegram_queues_private_image_bytes(self):
        from tg_bot import outbox
        with patch.object(outbox, "enqueue_dm", return_value=True) as enqueue:
            ok, _ = await tw_def_notify.send_maps(None, "", "tg:123", [("map.png", b"png")], "tg")
        self.assertTrue(ok)
        attachment = enqueue.call_args.args[2][0]
        self.assertEqual(base64.b64decode(attachment["data_base64"]), b"png")

    async def test_telegram_sends_inline_image_without_url(self):
        app = unittest.mock.Mock()
        app.api.send_photo = AsyncMock()
        await notifier._send_attachment(app, 123, None, {"filename": "map.png", "content_type": "image/png",
                                                       "data_base64": base64.b64encode(b"png").decode()})
        app.api.send_photo.assert_awaited_once_with(123, b"png", "map.png", thread_id=None)

    async def test_map_failure_keeps_text_and_reports_partial(self):
        rows = [player()]
        with patch.object(tw_def_notify.database, "get_guild_config", return_value={"name": "Guild"}), \
             patch.object(tw_def_notify, "build_player_rows", return_value=rows), \
             patch.object(tw_def_image, "render", side_effect=RuntimeError("broken")), \
             patch.object(tw_def_notify.tb_platoon_notify, "send_dm", AsyncMock(return_value=(True, "ok"))) as send:
            report = await tw_def_notify.send_broadcast("token", 1, {"name": "111"}, None, {"123456789"}, "discord_only")
        send.assert_awaited()
        self.assertEqual(report[0]["status"], "partial")


if __name__ == "__main__":
    unittest.main()
