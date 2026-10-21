"""/юнит (до 2026-10-21 — /юнит_тест; заменила старые /юнит и /юнит_прогноз, их код оставлен
для отката — LEGACY_UNIT_COMMANDS в cogs/unit_forecast.py). Пробный вариант /юнит по предложениям из треда «Гайд по АС Боту» (NicolozZ,
Ricardo, 2026-10-20), чтобы сравнить с /юнит на практике:

- /юнит и /юнит_прогноз в одной команде: «звёзды», «гир», «релик» пересчитывают выбранных
  персонажей у всех игроков на эту прокачку в их текущих модулях (services/unit_forecast.
  project); если игрок один — две колонки «сейчас» и «прогноз».
- вместо 10 одинаковых полей участник/аликод — флаг «сравнение»: бот открывает панель с
  выбором до 4 игроков (люди из Discord, коды союзника через окно ввода) и кнопками «С собой» / «Rokamora».

Картинка — services/toon.py + toon_image.py; корабль (один, без сравнения) — прогноз
по звёздам, как старая /юнит_прогноз (UnitForecastCog.send_forecast)."""

import asyncio
import io
import re

import disnake
from disnake.ext import commands

import database
import guild_resolver
import stat_engine
from cogs.unit_forecast import (
    LEGACY_UNIT_COMMANDS, ROKAMORA_ALLY_CODE, _parse_bracket_id, autocomplete_character_unit,
)
from services import toon, toon_image, unit_forecast

COMPARE_SLOTS = 4
PANEL_TIMEOUT = 840  # токен ответа живёт 15 минут


def level_label(stars, gear, relic) -> str:
    parts = []
    if relic is not None:
        parts.append(f"R{relic}")
    elif gear is not None:
        parts.append(f"G{gear}")
    if stars is not None:
        parts.append(f"{stars}★")
    return " ".join(parts)


def current_levels_text(players: list[dict], base_ids: list[str]) -> str:
    """«TEO Nod — G11 5★, AC Ricardo — R5 7★»; при нескольких юнитах — с их именами."""
    names = {b: database.get_game_unit_name(b) or b for b in base_ids} if len(base_ids) > 1 else {}
    parts = []
    for p in players:
        units = {toon.base_id_of(u): u for u in p.get("rosterUnit") or []}
        levels = []
        for b in base_ids:
            unit = units.get(b)
            if unit:
                lv = unit_forecast.current_level(unit)
                text = level_label(lv["stars"], None if lv["relic"] is not None else lv["gear"], lv["relic"])
            else:
                text = "нет"
            levels.append(f"{names[b]} {text}" if names else text)
        parts.append(f"{p.get('name') or '?'} — {', '.join(levels)}")
    return "; ".join(parts)


def projected_player(player: dict, base_ids: list[str], stars, gear, relic, suffix: str) -> dict:
    """Копия игрока: выбранные юниты пересчитаны на заданную прокачку, к имени — подпись."""
    roster = []
    for u in player.get("rosterUnit") or []:
        roster.append(unit_forecast.project(u, stars, gear, relic) if toon.base_id_of(u) in base_ids else u)
    return {**player, "name": f"{player.get('name') or '?'} ({suffix})", "rosterUnit": roster}


class CompareSetupView(disnake.ui.View):
    """Панель выбора игроков для сравнения: до COMPARE_SLOTS слотов — люди с Discord-сервера
    и коды союзника из одного поля. Списки игроков гильдии убраны (тред 2026-10-21)."""

    def __init__(self, cog, inter, guild_id: int, params: dict):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cog, self.inter, self.guild_id, self.params = cog, inter, guild_id, params
        self.author_id = inter.author.id
        self.users: list[disnake.User] = []
        self.codes: list[str] = []
        self.with_self = False
        self.rokamora = False

        self.user_select = disnake.ui.UserSelect(placeholder="Участники Discord", min_values=0, max_values=COMPARE_SLOTS, row=0)
        self.user_select.callback = self._on_users
        self.add_item(self.user_select)

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        return interaction.author.id == self.author_id

    async def _refresh(self, interaction: disnake.MessageInteraction, extra: str = ""):
        # Discord перерисовывает список при каждом edit_message без отметок — выбранные
        # «исчезали», хотя были запомнены (тред 2026-10-21). Отмечаем их заново.
        self.user_select.default_values = self.users or None
        await interaction.response.edit_message(content=self.status_text() + extra, view=self)

    async def _on_users(self, interaction: disnake.MessageInteraction):
        self.users = list(interaction.resolved_values)
        await self._refresh(interaction)

    def chosen(self) -> list[tuple[str, str]]:
        """[(подпись, источник)] — что выбрано сейчас, для строки статуса."""
        out = [(u.display_name, "discord") for u in self.users]
        out += [(c, "code") for c in self.codes]
        return out

    def status_text(self) -> str:
        chosen = self.chosen()
        lines = [f"**Сравнение** — выберите до {COMPARE_SLOTS} игроков любым способом (можно одного, без себя) и нажмите «Сравнить»."]
        lines.append("Выбрано: " + (", ".join(name for name, _ in chosen) if chosen else "никого")
                     + f" ({len(chosen)}/{COMPARE_SLOTS})")
        extras = [x for x, on in (("вы", self.with_self), ("Rokamora", self.rokamora)) if on]
        if extras:
            lines.append("Плюс: " + ", ".join(extras))
        return "\n".join(lines)

    @disnake.ui.button(label="Коды союзника", style=disnake.ButtonStyle.secondary, row=1)
    async def codes_button(self, button, interaction: disnake.MessageInteraction):
        await interaction.response.send_modal(CodesModal(self))

    @disnake.ui.button(label="С собой: нет", style=disnake.ButtonStyle.secondary, row=1)
    async def self_button(self, button: disnake.ui.Button, interaction: disnake.MessageInteraction):
        self.with_self = not self.with_self
        button.label = f"С собой: {'да' if self.with_self else 'нет'}"
        button.style = disnake.ButtonStyle.primary if self.with_self else disnake.ButtonStyle.secondary
        await self._refresh(interaction)

    @disnake.ui.button(label="Rokamora: нет", style=disnake.ButtonStyle.secondary, row=1)
    async def rokamora_button(self, button: disnake.ui.Button, interaction: disnake.MessageInteraction):
        self.rokamora = not self.rokamora
        button.label = f"Rokamora: {'да' if self.rokamora else 'нет'}"
        button.style = disnake.ButtonStyle.primary if self.rokamora else disnake.ButtonStyle.secondary
        await self._refresh(interaction)

    @disnake.ui.button(label="Сравнить", style=disnake.ButtonStyle.success, row=2)
    async def go_button(self, button, interaction: disnake.MessageInteraction):
        chosen = self.chosen()
        if len(chosen) > COMPARE_SLOTS:
            await interaction.response.send_message(f"❌ Не больше {COMPARE_SLOTS} игроков — уберите лишних.", ephemeral=True)
            return
        if not chosen and not self.with_self and not self.rokamora:
            await interaction.response.send_message("❌ Никто не выбран.", ephemeral=True)
            return
        await interaction.response.defer()
        codes, error = [], None
        if self.with_self:
            code, error = self.cog.own_code(self.inter, self.guild_id, self.params["альт"])
            codes.append(code)
        for u in self.users:
            reg = database.get_any_main_registration(str(u.id))
            if not reg:
                error = error or f"❌ {u.mention} не привязал(а) аккаунт через /регистрация — введите его код союзника."
                continue
            codes.append(reg[0])
        codes += self.codes
        if self.rokamora:
            codes.append(ROKAMORA_ALLY_CODE)
        if error:
            await interaction.followup.send(error, ephemeral=True)
            return
        self.stop()
        await self.inter.edit_original_response(content="⏳ Собираю карточку…", view=None)
        await self.cog.render_card(self.inter, interaction, list(dict.fromkeys(str(c) for c in codes if c)), self.params)


class CodesModal(disnake.ui.Modal):
    def __init__(self, view: CompareSetupView):
        self.view = view
        super().__init__(title="Коды союзника", components=[
            disnake.ui.TextInput(label=f"До {COMPARE_SLOTS} кодов через пробел или запятую", custom_id="codes",
                                 required=False, max_length=60, placeholder="123-456-789, 987-654-321",
                                 value=", ".join(view.codes) or None),
        ])

    async def callback(self, interaction: disnake.ModalInteraction):
        codes, bad = [], []
        for raw in re.split(r"[\s,;]+", interaction.text_values.get("codes") or ""):
            if not raw:
                continue
            code = guild_resolver.normalize_ally_code(raw)
            (codes if code else bad).append(code or raw)
        self.view.codes = list(dict.fromkeys(codes))
        await self.view._refresh(interaction, f"\n❌ Не коды союзника: {', '.join(bad)}" if bad else "")


class UnitTestCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def own_code(self, inter, guild_id: int, alt: bool) -> tuple[str | None, str | None]:
        if alt:
            account = guild_resolver.alt_account(inter.author)
            if account is None:
                return None, "❌ Второй аккаунт не найден — привяжите его через `/регистрация`."
            return account["ally_code"], None
        reg = database.get_user_registration(str(inter.author.id), guild_id=guild_id)
        if not reg:
            return None, "❌ Вы не зарегистрированы — используйте `/регистрация`."
        return reg[0], None

    @commands.slash_command(name="юнит_тест" if LEGACY_UNIT_COMMANDS else "юнит",
                            description="Персонаж: статы, модули, зеты; прогноз на другую прокачку; сравнение игроков")
    async def unit_test_cmd(
        self,
        inter: disnake.ApplicationCommandInteraction,
        юнит: str = commands.Param(description="Персонаж или корабль 🚀", autocomplete=autocomplete_character_unit),
        участник: disnake.User = commands.Param(default=None, description="Чужой игрок: человек в Discord (по его /регистрация, любая гильдия)"),
        аликод: str = commands.Param(default=None, description="Чужой игрок: код союзника (9 цифр)"),
        юнит2: str = commands.Param(default=None, description="Ещё персонаж", autocomplete=autocomplete_character_unit),
        юнит3: str = commands.Param(default=None, description="Ещё персонаж", autocomplete=autocomplete_character_unit),
        сравнение: bool = commands.Param(default=False, description="Панель выбора игроков для сравнения (до 4, вы — по кнопке «С собой»)"),
        звёзды: int = commands.Param(default=None, ge=1, le=7, description="Пересчитать на звёзды (1–7)"),
        гир: int = commands.Param(default=None, ge=1, le=13, description="Пересчитать на гир (1–13)"),
        релик: int = commands.Param(default=None, ge=0, le=10, description="Пересчитать на реликвию (0–10), ставит G13"),
        альт: bool = commands.Param(default=False, description="Вместо вашего основного аккаунта — второй (альт)"),
        обновить: bool = commands.Param(default=False, description="Свежие данные из игры вместо ежечасного обновления"),
    ):
        await inter.response.defer(ephemeral=сравнение)
        base_ids = list(dict.fromkeys(_parse_bracket_id(u) for u in (юнит, юнит2, юнит3) if u))
        types = database.get_unit_types(base_ids)
        for base_id in base_ids:
            if not database.get_game_unit_name(base_id):
                await inter.edit_original_response(f"❌ Персонаж «{base_id}» не найден — выберите его из списка.")
                return
        ship = any(types.get(b) == "ship" for b in base_ids)
        if ship and (len(base_ids) > 1 or сравнение or (аликод and участник)):
            await inter.edit_original_response("❌ Корабль — только один и для одного игрока: у кораблей нет модулей.")
            return
        error = unit_forecast.validate(звёзды, None if ship else гир, None if ship else релик)
        if error:
            await inter.edit_original_response(f"❌ {error}")
            return
        params = {"base_ids": base_ids, "звёзды": звёзды, "гир": гир, "релик": релик, "альт": альт, "обновить": обновить}

        # «участник»/«аликод» — посмотреть чужого игрока без панели и без привязки к гильдии
        # (тред 2026-10-21: «я только чужого мЛюка пробить хотел»); со «сравнением» — уже
        # выбраны в панели. Гильдия нужна только для своего аккаунта и панели.
        others = []
        if участник:
            reg = database.get_any_main_registration(str(участник.id))
            if not reg:
                await inter.edit_original_response(
                    f"❌ {участник.mention} не привязал(а) аккаунт через `/регистрация` — укажите его код союзника в «аликод».")
                return
            others.append(str(reg[0]))
        if аликод:
            code = guild_resolver.normalize_ally_code(аликод)
            if code is None:
                await inter.edit_original_response(f"❌ «{аликод}» — не код союзника: нужно 9 цифр.")
                return
            others.append(str(code))
        others = list(dict.fromkeys(others))

        guild_id = None
        if сравнение or not others:
            guild_id = await guild_resolver.require_guild_id(inter)
            if guild_id is None:
                return
        if сравнение:
            view = CompareSetupView(self, inter, guild_id, params)
            view.codes = others
            await inter.edit_original_response(content=view.status_text(), view=view)
            return
        if others:
            codes = others
        else:
            code, error = self.own_code(inter, guild_id, альт)
            if error:
                await inter.edit_original_response(error)
                return
            codes = [code]
        if ship:
            await self.bot.get_cog("UnitForecastCog").send_forecast(inter, codes[0], base_ids[0], звёзды, None, None)
            return
        await self.render_card(inter, None, codes, params)

    async def render_card(self, inter, component_inter, ally_codes: list[str], params: dict):
        """Карточка в канал: из панели — отдельным сообщением (панель эфемерная), иначе — ответом."""
        async def reply(text=None, file=None):
            if component_inter is not None:
                if file is not None:
                    await component_inter.followup.send(content=text, file=file)
                    await inter.edit_original_response(content="✅ Карточка отправлена в канал.", view=None)
                else:
                    await inter.edit_original_response(content=text, view=None)
            else:
                await inter.edit_original_response(content=text, file=file) if file else await inter.edit_original_response(text)

        if not await stat_engine.wait_stat_calc(self.bot, inter):
            await reply("⏳ Калькулятор статов не загрузился — попробуйте через пару минут.")
            return
        card_cog = self.bot.get_cog("UnitForecastCog")
        guild_id = guild_resolver.resolve_guild_id(inter.author)
        players, ages = await card_cog._card_players(ally_codes, guild_id, params["обновить"])
        for code, player in zip(ally_codes, players):
            if not player or not player.get("rosterUnit"):
                await reply(f"❌ Не удалось получить игрока с кодом {code} из игры.")
                return

        stars, gear, relic = params["звёзды"], params["гир"], params["релик"]
        if stars is None and (gear is not None or relic is not None):
            stars = 7  # как в /юнит_прогноз: реликвии и высокий гир — только на 7★
        base_ids = params["base_ids"]
        projection_note = None
        if any(v is not None for v in (stars, gear, relic)):
            suffix = level_label(stars, gear, relic)
            # Карточка прогноза выглядит как обычная — явно пишем, что уровень не настоящий,
            # и какой он сейчас (тред 2026-10-21).
            projection_note = (f"🔮 **Прогноз на {suffix}** — уровень пересчитан, модули текущие. Сейчас: "
                               + current_levels_text(players, base_ids))
            suffix = f"прогноз {suffix}"
            if len(players) == 1:
                current = {**players[0], "name": f"{players[0].get('name') or '?'} (сейчас)"}
                players = [current, projected_player(players[0], base_ids, stars, gear, relic, suffix)]
            else:
                players = [projected_player(p, base_ids, stars, gear, relic, suffix) for p in players]

        oldest = max((a for a in ages if a is not None), default=None)
        note = toon_image.freshness_note(oldest)
        if projection_note:
            note = projection_note + "\n" + note

        def work():
            return toon_image.render(toon.build(self.bot.stat_calc, players, base_ids))

        try:
            png = await asyncio.to_thread(work)
        except Exception as e:
            print(f"⚠️ [Юнит] {base_ids} для {ally_codes}: {e!r}")
            await reply("❌ Не удалось собрать карточку.")
            return
        await reply(note, file=disnake.File(io.BytesIO(png), filename="toon.png"))


def setup(bot):
    bot.add_cog(UnitTestCog(bot))
