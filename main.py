
import html
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import telebot
from telebot import apihelper, types
from telebot.apihelper import ApiTelegramException

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------- конфиг ----------
TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
SERVICES = [s.strip() for s in os.environ.get("SERVICES", "").split(",") if s.strip()]
INTERVAL = int(os.environ.get("CHECK_INTERVAL", "15"))
PROXY = os.environ.get("PROXY")

if PROXY:
    apihelper.proxy = {"http": PROXY, "https": PROXY}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

ICONS = {"active": "🟢", "activating": "🟡", "reloading": "🟡", "deactivating": "🟡",
         "inactive": "⚪", "failed": "🔴"}
STATE_RU = {"active": "active", "inactive": "остановлен", "failed": "упал",
            "activating": "запускается", "deactivating": "останавливается",
            "reloading": "перечитывает конфиг"}
ACTIONS = {"start": "Запускаю…", "stop": "Останавливаю…", "restart": "Перезапускаю…"}

# ---------- состояние (id сообщений переживает перезапуск бота) ----------
STATE_FILE = Path(__file__).with_name("state.json")
state = {"panel": None, "alert": None}
if STATE_FILE.exists():
    try:
        state.update(json.loads(STATE_FILE.read_text()))
    except Exception:
        pass

lock = threading.RLock()
current_view = "main"
quiet_until: dict[str, float] = {}  # чтобы не слать алерт на действия, сделанные из панели


def save_state():
    STATE_FILE.write_text(json.dumps(state))


# ---------- systemd ----------
def run(cmd: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def get_info(svc: str) -> dict:
    out = run(["systemctl", "--user", "show", svc,
               "-p", "ActiveState", "-p", "SubState", "-p", "LoadState",
               "-p", "ActiveEnterTimestampMonotonic",
               "-p", "InactiveEnterTimestampMonotonic",
               "-p", "NRestarts"]).stdout
    info = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return {
        "state": info.get("ActiveState", "unknown"),
        "sub": info.get("SubState", ""),
        "load": info.get("LoadState", ""),
        "enter": info.get("ActiveEnterTimestampMonotonic", "0"),
        "inactive_enter": info.get("InactiveEnterTimestampMonotonic", "0"),
        "restarts": info.get("NRestarts", "0"),
    }


def systemctl_action(action: str, svc: str) -> tuple[bool, str]:
    r = run(["systemctl", "--user", action, svc], timeout=60)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def esc(s: str) -> str:
    return html.escape(s)


def fmt_dur(sec: float) -> str:
    sec = max(int(sec), 0)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}д {h}ч"
    if h:
        return f"{h}ч {m}м"
    if m:
        return f"{m}м {s}с"
    return f"{s}с"


def line(svc: str, info: dict) -> str:
    if info["load"] == "not-found":
        return f"❓ <b>{esc(svc)}</b> unit not found"
    st = info["state"]
    return f"{ICONS.get(st, '❔')} <b>{esc(svc)}</b> · {st}"


def now_str() -> str:
    return time.strftime("%H:%M:%S")


def btn(text: str, data: str) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(text, callback_data=data)


# ---------- экраны ----------
def render_main():
    infos = [get_info(s) for s in SERVICES]
    up = sum(i["state"] == "active" for i in infos)
    if up in range(0, 2):
        text = (f"<b>Services</b> — {up} from {len(SERVICES)} is up\n\n"
                + "\n".join(line(s, i) for s, i in zip(SERVICES, infos))
                + f"\n\n<i>обновлено в {now_str()}</i>")
    else:
        text = (f"<b>Services</b> — {up} from {len(SERVICES)} are up\n\n"
                + "\n".join(line(s, i) for s, i in zip(SERVICES, infos))
                + f"\n\n<i>обновлено в {now_str()}</i>")

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(*[btn(f"{ICONS.get(i['state'], '❔')} {s}", f"s:{n}")
             for n, (s, i) in enumerate(zip(SERVICES, infos))])
    kb.row(btn("🔄 Reload", "main"))
    return text, kb


def render_service(n: int, note: str = ""):
    svc = SERVICES[n]
    info = get_info(svc)
    kb = types.InlineKeyboardMarkup()
    lines = [line(svc, info)]

    if info["load"] != "not-found":
        st = info["state"]
        lines.append(f"State: {st} ({esc(info['sub'])})")
        if st == "active":
            since = int(info["enter"])
            label = "Uptime"
        else:
            since = int(info["inactive_enter"])
            label = "Isn't running"
        if since > 0:
            lines.append(f"{label}: {fmt_dur(time.monotonic() - since / 1e6)}")
        lines.append(f"Restarts: {esc(info['restarts'])}")

        if st in ("active", "activating", "reloading"):
            kb.row(btn("🔁 Restart", f"restart:{n}"), btn("⏹ Стоп", f"ask:{n}"))
        else:
            kb.row(btn("▶️ Start", f"start:{n}"))
        kb.row(btn("📜 Logs", f"l:{n}"), btn("🔄 Update", f"s:{n}"))
    kb.row(btn("⬅️ Back", "main"))

    text = "\n".join(lines)
    if note:
        text += f"\n\n{note}"
    text += f"\n\n<i>Last update at {now_str()}</i>"
    return text, kb


def render_ask_stop(n: int):
    kb = types.InlineKeyboardMarkup()
    kb.row(btn("✅ Stop", f"stop:{n}"), btn("✖️ Do not", f"s:{n}"))
    return f"⏹ Stop <b>{esc(SERVICES[n])}</b>?", kb


def render_logs(n: int):
    svc = SERVICES[n]
    out = run(["journalctl", "--user-unit", svc, "-n", "25",
               "--no-pager", "-o", "short"]).stdout.strip()
    if out:
        body = "\n".join(l[:160] for l in out.splitlines())[-3000:]
    else:
        body = "Logs are empty"
    text = f"📜 <b>{esc(svc)}</b>\n<pre>{esc(body)}</pre>\n<i>last update at {now_str()}</i>"
    kb = types.InlineKeyboardMarkup()
    kb.row(btn("🔄 Update", f"l:{n}"), btn("⬅️ Back to service", f"s:{n}"))
    return text, kb


def render(view: str, note: str = ""):
    cmd, _, arg = view.partition(":")
    try:
        n = int(arg) if arg else None
        if n is not None and not 0 <= n < len(SERVICES):
            raise ValueError
        if cmd == "s":
            return render_service(n, note)
        if cmd == "l":
            return render_logs(n)
        if cmd == "ask":
            return render_ask_stop(n)
    except (ValueError, TypeError):
        pass
    return render_main()


# ---------- работа с панелью (единственное сообщение) ----------
def safe_delete(message_id: int):
    try:
        bot.delete_message(ADMIN_ID, message_id)
    except Exception:
        pass


def send_panel(view: str = "main"):
    """Новая панель внизу чата, старая удаляется."""
    global current_view
    with lock:
        current_view = view
        text, kb = render(view)
        old = state.get("panel")
        msg = bot.send_message(ADMIN_ID, text, reply_markup=kb)
        state["panel"] = msg.message_id
        save_state()
        if old:
            safe_delete(old)


def edit_panel(text: str, kb):
    with lock:
        if not state.get("panel"):
            return send_panel(current_view)
        try:
            bot.edit_message_text(text, ADMIN_ID, state["panel"], reply_markup=kb)
        except ApiTelegramException as e:
            err = str(e)
            if "message is not modified" in err or "Too Many Requests" in err:
                return
            logging.warning("edit failed (%s), sending new panel", err)
            send_panel(current_view)


def show(view: str, note: str = ""):
    global current_view
    with lock:
        current_view = view
        text, kb = render(view, note)
        edit_panel(text, kb)


def notify(text: str):
    """Алерт отдельным сообщением (чтобы пришло уведомление), хранится только последний."""
    with lock:
        old = state.get("alert")
        msg = bot.send_message(ADMIN_ID, text)
        state["alert"] = msg.message_id
        save_state()
        if old:
            safe_delete(old)


# ---------- хендлеры ----------
@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID, content_types=["text"])
def on_text(m):
    # любое сообщение (/start и т.п.) -> панель переезжает вниз, сообщение пользователя удаляется
    safe_delete(m.message_id)
    send_panel()


@bot.callback_query_handler(func=lambda c: c.from_user.id == ADMIN_ID)
def on_button(c):
    cmd, _, arg = c.data.partition(":")
    if cmd in ACTIONS and arg.isdigit() and int(arg) < len(SERVICES):
        n = int(arg)
        bot.answer_callback_query(c.id, ACTIONS[cmd])
        quiet_until[SERVICES[n]] = time.time() + INTERVAL + 5
        ok, msg = systemctl_action(cmd, SERVICES[n])
        time.sleep(1.5)
        show(f"s:{n}", note="" if ok else f"⚠️ <code>{esc(msg)}</code>")
    else:
        bot.answer_callback_query(c.id)
        show(c.data)


# ---------- мониторинг ----------
def monitor():
    last: dict[str, dict] = {}
    while True:
        changed = False
        for svc in SERVICES:
            try:
                cur = get_info(svc)
                prev = last.get(svc)
                last[svc] = cur
                if prev is None:
                    continue
                alert = None
                if cur["state"] != prev["state"]:
                    alert = (f"{ICONS.get(cur['state'], '❔')} <b>{esc(svc)}</b>: "
                             f"{cur['state']}\n"
                             f"<i>previous : {prev['state']}</i>")
                elif cur["state"] == "active" and cur["enter"] != prev["enter"]:
                    alert = (f"🔄 <b>{esc(svc)}</b> is restarted"
                             f"(Restats: {esc(cur['restarts'])})")
                if alert:
                    changed = True
                    if time.time() > quiet_until.get(svc, 0):
                        notify(alert)
            except Exception:
                logging.exception("monitor error for %s", svc)

        if changed and (current_view == "main" or current_view.startswith("s:")):
            try:
                show(current_view)
            except Exception:
                logging.exception("panel refresh failed")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    if not SERVICES:
        raise SystemExit("SERVICES are empty: made an unit list in .env")
    threading.Thread(target=monitor, daemon=True).start()
    if state.get("panel"):
        show("main")
    else:
        send_panel()
    logging.info("bot started, services: %s", SERVICES)
    bot.infinity_polling(skip_pending=True)
