"""Farm calculations using a player's own inventory on AbsoluteChaos TEST."""

import asyncio
import logging

import disnake
from disnake.ext import commands

import database
import guild_resolver
from services import bot_profiles, farm_inventory, farm_report_image, game_account, game_account_readonly, journey_guide


AC_DISCORD_SERVER = 931280548402442310
GUILD_ID = 1
log = logging.getLogger(__name__)


def _profile():
    return bot_profiles.worker_profile() or bot_profiles.runtime_profile()


def _allowed(inter):
    if _profile() == "prod":
        access = guild_resolver.resolve_access(inter.author)
        guild_id = access.get("guild_id")
        config = database.get_guild_config(guild_id) if guild_id else None
        if not inter.guild_id or not config or str(config.get("discord_guild_id")) != str(inter.guild_id):
            raise game_account.AccountError("Откройте команду на сервере своей гильдии.")
        game_account.require_account_read(str(inter.author.id), guild_id, _profile(), access.get("tier"))
        return guild_id
    if inter.guild_id != AC_DISCORD_SERVER:
        raise game_account.AccountError("Команда доступна участникам АС на сервере гильдии.")
    game_account.require_account_read(str(inter.author.id), GUILD_ID, _profile())
    return GUILD_ID


async def _autocomplete_access(inter):
    try:
        await asyncio.to_thread(_allowed, inter)
    except game_account.AccountError:
        return False
    return True


async def _units(inter, query):
    if not await _autocomplete_access(inter):
        return {}
    units = await asyncio.to_thread(database.search_game_units, query)
    types = await asyncio.to_thread(database.get_unit_types, [code for code, _ in units])
    return {name[:100]: code for code, name in units if code in types and types[code] != "ship"}


async def _guides(inter, query):
    if not await _autocomplete_access(inter):
        return {}
    options = await asyncio.to_thread(journey_guide.guide_options)
    query = query.casefold()
    matches = [row for row in options if query in row["name"].casefold() or query in row["base_id"].casefold()]
    return {row["name"][:100]: row["base_id"] for row in matches[:25]}


async def _accounts(inter, query):
    if not await _autocomplete_access(inter):
        return {}
    accounts = await asyncio.to_thread(database.get_user_registrations, str(inter.author.id), guild_id=await asyncio.to_thread(_allowed, inter))
    labels = [(f"{name or code} · {'основной' if main else 'альт'} · {code}", str(code)) for code, name, main in accounts]
    return {label[:100]: code for label, code in labels if query.casefold() in label.casefold()}


class FarmLinkCog(commands.Cog):
    @commands.slash_command(name="фарм", description="Рассчитать недостающие материалы по запасам", guild_ids=[AC_DISCORD_SERVER] if _profile() == "test" else None)
    async def farm(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @farm.sub_command(name="персонаж", description="Посчитать материалы для персонажа. Без параметров — до G13 и R5")
    async def unit(
        self, inter: disnake.ApplicationCommandInteraction,
        персонаж: str = commands.Param(autocomplete=_units, description="Выберите персонажа"),
        снаряжение: int = commands.Param(default=None, ge=1, le=13, description="Целевой уровень G1–G13. Укажите только его, чтобы считать без реликвии"),
        реликвия: int = commands.Param(default=None, ge=1, le=10, description="Целевая реликвия R1–R10, включая прокачку до G13"),
        аккаунт: str = commands.Param(default=None, autocomplete=_accounts, description="Свой основной аккаунт или альт"),
    ):
        if снаряжение is None and реликвия is None:
            реликвия = 5
        mode, level = ("relic", реликвия) if реликвия is not None else ("gear", снаряжение)
        await self._calculate(inter, аккаунт, targets=[(персонаж, mode, level)])

    @farm.sub_command(name="руководство", description="Посчитать материалы для цели из Руководства")
    async def guide(
        self, inter: disnake.ApplicationCommandInteraction,
        цель: str = commands.Param(autocomplete=_guides, description="Выберите цель из Руководства"),
        награда: bool = commands.Param(default=False, description="Добавить снаряжение для персонажа-награды до G13"),
        аккаунт: str = commands.Param(default=None, autocomplete=_accounts, description="Свой основной аккаунт или альт"),
    ):
        await self._calculate(inter, аккаунт, guide_id=цель, include_reward=награда)

    async def _calculate(self, inter, account, *, targets=None, guide_id=None, include_reward=False):
        await inter.response.defer(ephemeral=True)
        try:
            guild_id = await asyncio.to_thread(_allowed, inter)
            if targets:
                ids = [unit_id for unit_id, _, _ in targets]
                types = await asyncio.to_thread(database.get_unit_types, ids)
                if any(unit_id not in types or types[unit_id] == "ship" for unit_id in ids):
                    raise game_account.AccountError("Выберите персонажа из подсказок Discord.")
            owner = str(inter.author.id)
            code = await asyncio.to_thread(game_account.select_account, owner, guild_id, ally_code=account)
            requirements = None
            if guide_id is not None:
                requirements = await asyncio.to_thread(database.get_journey_guide_requirements, guide_id)
                if not requirements:
                    raise game_account.AccountError("Выберите цель из списка «Руководства».")
            await self._send_report(inter, code, targets, guide_id, requirements, include_reward)
        except game_account.SessionExpired:
            view = FarmSessionView(self, owner, code, targets, guide_id, requirements, include_reward)
            await inter.edit_original_response(
                content=game_account.LOGIN_WARNING,
                view=view,
            )
        except game_account.ConnectionRequired:
            await _offer_connection(inter, owner, code)
        except game_account.AccountError as error:
            await inter.edit_original_response(content=str(error))
            return
        except Exception as error:
            log.warning("Farm calculation failed (%s)", type(error).__name__)
            await inter.edit_original_response(content="Не удалось рассчитать фарм. Попробуйте ещё раз позже.")
            return

    async def _send_report(self, inter, code, targets, guide_id, requirements, include_reward):
        owner = str(inter.author.id)
        guild_id = await asyncio.to_thread(_allowed, inter)
        snapshot = await game_account_readonly.read(owner, guild_id, _profile(), None, code, member_read=True)
        report = await asyncio.to_thread(
            farm_inventory.build_report, snapshot, targets, guide_id=guide_id,
            requirements=requirements, include_reward=include_reward,
        )
        accounts = await asyncio.to_thread(database.get_user_registrations, owner, guild_id=guild_id)
        player = next((name for ally_code, name, _ in accounts if str(ally_code) == code and name), "Игрок")
        portrait_id = guide_id or targets[0][0]
        images = await asyncio.to_thread(farm_report_image.render, report, player, portrait_id)
        for start in range(0, len(images), 10):
            files = [
                disnake.File(image, filename=f"farm-{index}.png")
                for index, image in enumerate(images[start:start + 10], start + 1)
            ]
            await inter.channel.send(files=files, allowed_mentions=disnake.AllowedMentions.none())
        await inter.edit_original_response(content="Отчёт опубликован в канале.")


async def _offer_connection(inter, owner, code):
    guild_id = await asyncio.to_thread(_allowed, inter)
    path = await asyncio.to_thread(game_account.link_path, owner, guild_id, code)
    view = disnake.ui.View()
    view.add_item(disnake.ui.Button(style=disnake.ButtonStyle.link, label="Подключить через EA",
                                    url=f"https://swgoh-sng.ru{path}"))
    await inter.edit_original_response(content="Подключите игровой аккаунт для расчёта.", view=view)


class FarmSessionView(disnake.ui.View):
    def __init__(self, cog, owner, code, targets, guide_id, requirements, include_reward):
        super().__init__(timeout=600)
        self.cog = cog
        self.owner = owner
        self.code = code
        self.plan = (targets, guide_id, requirements, include_reward)

    async def interaction_check(self, inter):
        if str(inter.author.id) == self.owner:
            return True
        await inter.response.send_message("Это подтверждение для автора команды.", ephemeral=True)
        return False

    @disnake.ui.button(label="Войти и рассчитать", style=disnake.ButtonStyle.success)
    async def confirm(self, button, inter):
        await inter.response.defer(with_message=False)
        if self.is_finished():
            return
        self.stop()
        try:
            await inter.edit_original_response(content="Восстанавливаю сессию и рассчитываю фарм…", view=None)
            guild_id = await asyncio.to_thread(_allowed, inter)
            await game_account.reconnect(self.owner, guild_id, _profile(), None, self.code, optimizer_read=True)
            await self.cog._send_report(inter, self.code, *self.plan)
        except game_account.ConnectionRequired:
            await _offer_connection(inter, self.owner, self.code)
        except game_account.AccountError as error:
            await inter.edit_original_response(content=str(error))
        except Exception as error:
            log.warning("Farm session recovery failed (%s)", type(error).__name__)
            await inter.edit_original_response(content="Не удалось восстановить игровую сессию. Попробуй ещё раз позже.")

    @disnake.ui.button(label="Отмена", style=disnake.ButtonStyle.secondary)
    async def cancel(self, button, inter):
        await inter.response.defer(with_message=False)
        if self.is_finished():
            return
        self.stop()
        await inter.edit_original_response(content="Расчёт отменён.", view=None)


def setup(bot):
    bot.add_cog(FarmLinkCog())
