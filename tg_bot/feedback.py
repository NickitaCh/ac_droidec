"""Обратная связь в личке Telegram: команда, затем ответ на приглашение."""
from services import feedback
from tg_bot.core import Ctx, command, TIER_ANY


@command("feedback", "Обратная связь: ошибка, предложение или вопрос Nod", tier=TIER_ANY,
         aliases=("ос", "обратная_связь"), where="private")
async def cmd_feedback(ctx: Ctx):
    if not ctx.args:
        await ctx.api.call("sendMessage", chat_id=ctx.chat_id,
                           text="Ответьте на это сообщение: опишите ошибку, предложение или вопрос для Nod (до 4000 символов).",
                           reply_markup={"force_reply": True, "selective": True,
                                         "input_field_placeholder": "Ваше обращение для Nod"})
        return
    try:
        report_id = feedback.create(ctx.owner_id, ctx.user.get("username") or ctx.user.get("first_name"),
                                    ctx.access.get("guild_id"), ctx.args, user_agent="Telegram")
    except ValueError as e:
        await ctx.reply(str(e))
        return
    await ctx.reply(f"Обращение №{report_id} отправлено Nod. Ответ придёт в эту личку.")
    await feedback.notify(report_id)


async def handle_reply(app, message):
    replied = message.get("reply_to_message") or {}
    if (message["chat"].get("type") != "private"
            or (replied.get("from") or {}).get("id") != app.bot_id
            or not (replied.get("text") or "").startswith("Ответьте на это сообщение: опишите ошибку")):
        return False
    await cmd_feedback(Ctx(app, message, message["from"], message.get("text") or ""))
    return True
