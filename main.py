import html
import logging
import os
import subprocess
import threading
import time

import telebot
from telebot import apihelper, types
from dotenv import load_dotenv



load_dotenv()

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
SERVICES = [s.strip() for s in os.environ.get("SERVICES", "nginx,ssh").split(",") if s.strip()]
INTERVAL = int(os.environ.get("CHECK_INTERVAL", "15"))
USE_SUDO = os.environ.get("USE_SUDO", "0") == "1"
PROXY = os.environ.get("PROXY")

if PROXY:
    apihelper.proxy = {"http": PROXY, "https": PROXY}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

ICONS = {"active": "🟢", "activating": "🟡", "reloading": "🟡", "deactivating": "🟡",
         "inactive": "⚪", "failed": "🔴"}


# ---------- работа с systemd ----------
def run(cmd: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def get_info(svc: str) -> dict:
    """Возвращает ActiveState и время последнего входа в active (для детекта рестартов)."""
    out = run(["systemctl", "show", svc,
               "-p", "ActiveState", "-p", "SubState",
               "-p", "ActiveEnterTimestampMonotonic", "-p", "NRestarts"]).stdout
    info = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return {
        "state": info.get("ActiveState", "unknown"),
        "sub": info.get("SubState", ""),
        "enter": info.get("ActiveEnterTimestampMonotonic", "0"),
        "restarts": info.get("NRestarts", "0"),
    }


def systemctl_action(action: str, svc: str) -> str:
    cmd = ["systemctl", action, svc]
    if USE_SUDO:
        cmd = ["sudo", "-n"] + cmd
    r = run(cmd, timeout=60)
    return (r.stderr or r.stdout).strip() or "ok"


def fmt_status(svc: str, info: dict) -> str:
    icon = ICONS.get(info["state"], "❔")
    return f"{icon} <b>{html.escape(svc)}</b> — {info['state']} ({info['sub']})"


# ---------- доступ ----------
def is_admin(obj) -> bool:
    return obj.from_user.id == ADMIN_ID


# ---------- команды ----------
@bot.message_handler(commands=["start", "help"], func=is_admin)
def cmd_help(m):
    bot.reply_to(m, (
        "/status — состояние всех сервисов\n"
        "/logs [сервис] — последние 30 строк журнала\n"
        "/restart [сервис]\n/stop [сервис]\n/startsvc [сервис]\n\n"
        "Без аргумента покажу кнопки с выбором сервиса."
    ))


@bot.message_handler(commands=["status"], func=is_admin)
def cmd_status(m):
    lines = [fmt_status(s, get_info(s)) for s in SERVICES]
    bot.reply_to(m, "\n".join(lines))


def pick_keyboard(action: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(*[types.InlineKeyboardButton(s, callback_data=f"{action}:{s}") for s in SERVICES])
    return kb


def handle_action(action: str, svc: str) -> str:
    if svc not in SERVICES:
        return "Такого сервиса нет в списке."
    if action == "logs":
        out = run(["journalctl", "-u", svc, "-n", "30", "--no-pager", "-o", "short"]).stdout
        return f"<b>{html.escape(svc)}</b>\n<pre>{html.escape(out[-3500:])}</pre>"
    real = "start" if action == "startsvc" else action
    res = systemctl_action(real, svc)
    time.sleep(1)
    return f"{fmt_status(svc, get_info(svc))}\n<code>{html.escape(res)}</code>"


def make_command(action: str):
    @bot.message_handler(commands=[action], func=is_admin)
    def _handler(m):
        parts = m.text.split(maxsplit=1)
        if len(parts) == 1:
            bot.reply_to(m, "Выбери сервис:", reply_markup=pick_keyboard(action))
        else:
            bot.reply_to(m, handle_action(action, parts[1].strip()))


for _a in ("logs", "restart", "stop", "startsvc"):
    make_command(_a)


@bot.callback_query_handler(func=lambda c: c.from_user.id == ADMIN_ID)
def on_button(c):
    action, svc = c.data.split(":", 1)
    bot.answer_callback_query(c.id)
    bot.send_message(c.message.chat.id, handle_action(action, svc))



def monitor():
    last: dict[str, dict] = {}
    while True:
        for svc in SERVICES:
            try:
                cur = get_info(svc)
                prev = last.get(svc)
                if prev is not None:
                    msg = None
                    if cur["state"] != prev["state"]:
                        msg = f"{fmt_status(svc, cur)}\nбыло: {prev['state']}"
                    elif cur["state"] == "active" and cur["enter"] != prev["enter"]:
                        msg = f"🔄 <b>{html.escape(svc)}</b> перезапустился (рестартов: {cur['restarts']})"
                    if msg:
                        bot.send_message(ADMIN_ID, msg)
                last[svc] = cur
            except Exception:
                logging.exception("monitor error for %s", svc)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    threading.Thread(target=monitor, daemon=True).start()
    logging.info("bot started, services: %s", SERVICES)
    bot.infinity_polling(skip_pending=True)
