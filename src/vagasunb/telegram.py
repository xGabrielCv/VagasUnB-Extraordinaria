"""Telegram adapter. No connection is made by importing this module."""

import asyncio
import json
import time

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.request import BaseRequest
from telegram.request import HTTPXRequest


class BudgetRequest(HTTPXRequest):
    """All outgoing message flows use the same PostgreSQL budget, across processes."""

    def __init__(self, db, vault):
        super().__init__()
        from vagasunb.distribution import SendBudget

        self.budget, self.db, self.vault = SendBudget(db), db, vault
        self.unregistered = {}
        self.local_lock = asyncio.Lock()

    async def do_request(self, url, method, request_data=None, **kwargs):
        api = url.rsplit("/", 1)[-1]
        if api in {"sendMessage", "editMessageText"}:
            from vagasunb.distribution import send_permit

            params = request_data.parameters if request_data else {}
            permit = send_permit.get()
            prepaid = bool(
                api == "sendMessage"
                and permit
                and "chat_id" in params
                and permit[1] == self.vault.index(params["chat_id"])
            )
            if prepaid:
                send_permit.set(None)
            else:
                await self.acquire(params)
        result = await super().do_request(url, method, request_data, **kwargs)
        if result[0] == 429:
            try:
                delay = int(json.loads(result[1]).get("parameters", {}).get("retry_after", 60))
            except (ValueError, TypeError):
                delay = 60
            await self.budget.defer(max(1, delay))
        return result

    async def acquire(self, params):
        from sqlalchemy import select
        from vagasunb.db import Recipient

        async with self.db.transaction() as s:
            rid = (
                await s.scalar(
                    select(Recipient.id).where(
                        Recipient.lookup == self.vault.index(params["chat_id"])
                    )
                )
                if "chat_id" in params
                else None
            )
        if rid is None and "chat_id" in params:
            key = self.vault.index(params["chat_id"])
            while True:
                async with self.local_lock:
                    clock = time.monotonic()
                    self.unregistered = {k: v for k, v in self.unregistered.items() if v > clock}
                    delay = self.unregistered.get(key, 0) - clock
                    if delay <= 0:
                        self.unregistered[key] = clock + 1
                        break
                await asyncio.sleep(delay)
        if await self.budget.wait(rid) is False:
            raise Forbidden("Destinatário indisponível")


class DeliveryError(Exception):
    def __init__(self, code, retry_after=0):
        self.code, self.retry_after = code, retry_after
        super().__init__(code)


class FakeTransport:
    def __init__(self, record=True):
        self.views = [] if record else None  # Test-only capture; worker retains no messages.

    async def send(self, chat_id, view):
        if self.views is not None:
            self.views.append(view)


class FakeRequest(BaseRequest):
    """PTB protocol-level synthetic transport: never opens a socket."""

    @property
    def read_timeout(self):
        return 1

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        api = url.rsplit("/", 1)[-1]
        if api == "getMe":
            result = {"id": 1, "is_bot": True, "first_name": "Synthetic"}
        elif api == "answerCallbackQuery":
            result = True
        else:
            params = request_data.parameters if request_data else {}
            result = {
                "message_id": 1,
                "date": 0,
                "chat": {"id": int(params.get("chat_id", 1)), "type": "private"},
                "text": params.get("text", ""),
            }
        return 200, json.dumps({"ok": True, "result": result}).encode()


def markup(view):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(b["label"], url=b.get("url"), callback_data=b.get("callback"))]
            for b in view.buttons
        ]
    )


class TelegramTransport:
    real_enabled = True

    def __init__(self, bot: Bot):
        self.bot = bot

    async def send(self, chat_id, view):
        try:
            await self.bot.send_message(
                chat_id=int(chat_id),
                text=view.text,
                reply_markup=markup(view),
                parse_mode="HTML",
            )
        except RetryAfter as exc:
            delay = (
                exc.retry_after.total_seconds()
                if hasattr(exc.retry_after, "total_seconds")
                else exc.retry_after
            )
            raise DeliveryError("rate_limit", int(delay)) from None
        except Forbidden:
            raise DeliveryError("forbidden") from None
        except BadRequest:
            raise DeliveryError("invalid_destination") from None
        except (TimedOut, NetworkError):
            raise DeliveryError("uncertain") from None


async def handle_update(update, context, db, vault):
    chat, sender = update.effective_chat, update.effective_user
    if not chat or chat.type != "private" or not sender or sender.id != chat.id:
        return
    query = update.callback_query
    if query:
        await query.answer()  # Acknowledge promptly, before DB work.
    # Whitelist projection. No SDK object/profile is stored.
    command_text = update.message.text if update.message and update.message.text else ""
    callback = query.data if query else None
    from vagasunb.router import route

    async with db.transaction() as s:
        view = await route(
            s,
            vault,
            chat.id,
            command_text,
            callback,
            private=True,
            sender_matches=True,
            synthetic=False,
        )
    if query:
        try:
            await query.edit_message_text(view.text, reply_markup=markup(view), parse_mode="HTML")
            return
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
    await context.bot.send_message(
        chat_id=chat.id, text=view.text, reply_markup=markup(view), parse_mode="HTML"
    )
    if update.message and command_text and not command_text.startswith("/"):
        try:
            await update.message.delete()
        except (BadRequest, Forbidden):
            pass


def run_bot(settings):
    import os
    from functools import partial

    from telegram.ext import ApplicationBuilder, CallbackQueryHandler, MessageHandler, filters

    from vagasunb.crypto import Vault
    from vagasunb.db import Database

    if settings.mode != "public" or os.getenv("VAGAS_TELEGRAM_ENABLE") != "1":
        raise RuntimeError(
            "Telegram exige modo telegram e habilitação explícita; token sozinho não ativa"
        )
    token = (settings.secrets / "telegram_token").read_text().strip()
    db, vault = Database(settings.database_url), Vault(settings.secrets)
    state = {}

    async def startup(app):
        from sqlalchemy import text
        from vagasunb.services import verify_instance

        connection = await db.engine.connect()
        if not await connection.scalar(
            text("SELECT pg_try_advisory_lock(hashtextextended('bot:poller', 0))")
        ):
            await connection.close()
            raise RuntimeError("Já existe um receptor long polling nesta instância")
        await connection.commit()
        state["connection"] = connection
        async with db.transaction() as s:
            await verify_instance(s, vault, settings.mode)

        async def heartbeat():
            from pathlib import Path

            while True:
                directory = Path(os.getenv("VAGAS_HEARTBEAT_DIR", ".private"))
                directory.mkdir(exist_ok=True)
                (directory / "bot.heartbeat").write_text(str(time.time()))
                await asyncio.sleep(5)

        state["heartbeat"] = asyncio.create_task(heartbeat())

    async def shutdown(app):
        if "heartbeat" in state:
            state["heartbeat"].cancel()
            await asyncio.gather(state["heartbeat"], return_exceptions=True)
        if "connection" in state:
            await state["connection"].close()
        await db.close()

    app = (
        ApplicationBuilder()
        .token(token)
        .request(BudgetRequest(db, vault))
        .post_init(startup)
        .post_shutdown(shutdown)
        .build()
    )
    handler = partial(handle_update, db=db, vault=vault)
    app.add_handler(CallbackQueryHandler(handler))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, handler))
    # Commands are intentionally not registered remotely.
    app.run_polling(allowed_updates=["message", "callback_query"])
