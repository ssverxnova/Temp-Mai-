import os
import re
import secrets
from dataclasses import dataclass
from typing import Optional, Dict, Any
from datetime import datetime

import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BASE = os.getenv("MAILTM_BASE", "https://api.mail.tm").strip().rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")

# ===== НАСТРОЙКИ =====
CODE_REGEX = re.compile(r"\b(\d{6})\b")

SERVICE_RULES = {
    "AdGuard VPN": ["adguard"],
    "Юбуст": ["youbust", "юбуст", "ubust"],
}

# ===== СЕССИИ =====
@dataclass
class Session:
    address: str
    password: str
    token: str
    account_id: str

SESSIONS: Dict[int, Session] = {}

# ===== MAIL.TM CLIENT =====
class MailTmClient:
    def __init__(self, base: str):
        self.base = base

    async def _request(self, method, path, token=None, json=None):
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.request(method, f"{self.base}{path}", headers=headers, json=json)
            r.raise_for_status()
            return r.json() if r.content else None

    async def get_domains(self):
        d = await self._request("GET", "/domains?page=1")
        return [x["domain"] for x in d["hydra:member"]]

    async def create_account(self, address, password):
        return await self._request("POST", "/accounts", json={"address": address, "password": password})

    async def get_token(self, address, password):
        return (await self._request("POST", "/token", json={"address": address, "password": password}))["token"]

    async def me(self, token):
        return await self._request("GET", "/me", token=token)

    async def list_messages(self, token):
        return (await self._request("GET", "/messages?page=1", token=token))["hydra:member"]

    async def get_message(self, token, mid):
        return await self._request("GET", f"/messages/{mid}", token=token)

# ===== HELPERS =====
def detect_service(text: str) -> str:
    t = text.lower()
    for name, keys in SERVICE_RULES.items():
        if any(k in t for k in keys):
            return name
    return "Неизвестный сервис"

def extract_code(text: str) -> Optional[str]:
    if not text:
        return None
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"(\d)\s+(\d)", r"\1\2", clean)
    m = CODE_REGEX.search(clean)
    return m.group(1) if m else None

def normalize_body(full: dict) -> str:
    text = full.get("text") or ""
    html = full.get("html")
    if isinstance(html, list):
        html = " ".join(html)
    return text + (html or "")

# ===== КЛАВИАТУРЫ =====
def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🆕 Новая почта", callback_data="new")],
        [InlineKeyboardButton("📮 Текущая почта", callback_data="current")],
        [InlineKeyboardButton("🔐 Получить код", callback_data="code")],
    ])

def back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu")]
    ])

# ===== HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Выбери действие.", reply_markup=main_keyboard())

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    client = MailTmClient(BASE)

    if q.data == "menu":
        await q.edit_message_text("Выбери действие.", reply_markup=main_keyboard())

    elif q.data == "new":
        domain = (await client.get_domains())[0]
        address = f"tg{secrets.token_hex(5)}@{domain}"
        password = secrets.token_urlsafe(12)

        await client.create_account(address, password)
        token = await client.get_token(address, password)
        me = await client.me(token)

        SESSIONS[user_id] = Session(address, password, token, me["id"])
        await q.edit_message_text(
            f"Почта создана:\n\n`{address}`",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )

    elif q.data == "current":
        s = SESSIONS.get(user_id)
        if not s:
            await q.edit_message_text("Почта не создана.", reply_markup=main_keyboard())
        else:
            await q.edit_message_text(
                f"Текущая почта:\n\n`{s.address}`",
                parse_mode="Markdown",
                reply_markup=main_keyboard()
            )

    elif q.data == "code":
        s = SESSIONS.get(user_id)
        if not s:
            await q.edit_message_text("Сначала создай почту.", reply_markup=main_keyboard())
            return

        msgs = await client.list_messages(s.token)
        if not msgs:
            await q.edit_message_text("Писем пока нет.", reply_markup=main_keyboard())
            return

        lines = ["🔐 Коды из писем:\n"]
        for m in msgs[:5]:
            full = await client.get_message(s.token, m["id"])
            subject = full.get("subject", "Без темы")
            body = normalize_body(full)
            code = extract_code(body)
            service = detect_service(subject + body)
            time_str = datetime.fromisoformat(full["createdAt"].replace("Z","")).strftime("%H:%M")

            code_text = f"`{code}`" if code else "—"
            lines.append(
                f"🏷 {service}\n"
                f"🧾 {subject}\n"
                f"🕒 {time_str}\n"
                f"🔐 {code_text}\n"
            )

        await q.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=back_keyboard()
        )

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_buttons))
    app.run_polling()

if __name__ == "__main__":
    main()
