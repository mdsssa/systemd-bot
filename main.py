import glob
import html
import json
import logging
import os
import shutil
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
PANEL_REFRESH = int(os.environ.get("PANEL_REFRESH", "0"))  # сек; 0 = главный экран обновляется только по событиям и кнопке

if PROXY:
    apihelper.proxy = {"http": PROXY, "https": PROXY}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

ICONS = {"active": "🟢", "activating": "🟡", "reloading": "🟡", "deactivating": "🟡",
         "inactive": "⚪", "failed": "🔴"}
STATE_RU = {"active": "active", "inactive": "остановлен", "failed": "упал",
            "activating": "запускается", "deactivating": "останавливается",
            "reloading": "перечитывает конфиг"}
ACTIONS = {"start": "Starting", "stop": "Terminating", "restart": "Restarting"}

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
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def line(svc: str, info: dict) -> str:
    if info["load"] == "not-found":
        return f"❓ <b>{esc(svc)}</b> unit not found"
    st = info["state"]
    return f"{ICONS.get(st, '❔')} <b>{esc(svc)}</b> · {st}"


def now_str() -> str:
    return time.strftime("%H:%M:%S")


def btn(text: str, data: str) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(text, callback_data=data)


# ---------- система (CPU / RAM / GPU / аптайм) ----------
cpu_pct: float | None = None


def read_cpu_times() -> tuple[int, int]:
    with open("/proc/stat") as f:
        vals = [int(x) for x in f.readline().split()[1:]]
    idle = vals[3] + vals[4]  # idle + iowait
    return sum(vals[:8]), idle


def cpu_sampler():
    """Раз в 2 секунды считает загрузку CPU по /proc/stat."""
    global cpu_pct
    prev = read_cpu_times()
    time.sleep(0.5)
    while True:
        cur = read_cpu_times()
        total, idle = cur[0] - prev[0], cur[1] - prev[1]
        if total > 0:
            cpu_pct = 100 * (1 - idle / total)
        prev = cur
        time.sleep(2)


def read_ram() -> tuple[float, float]:
    mem = {}
    with open("/proc/meminfo") as f:
        for l in f:
            k, v = l.split(":")
            mem[k] = int(v.split()[0])  # kB
    total = mem["MemTotal"]
    return (total - mem["MemAvailable"]) / 1048576, total / 1048576  # GiB


def read_uptime() -> float:
    with open("/proc/uptime") as f:
        return float(f.read().split()[0])


def read_gpu() -> tuple[float, str] | None:
    """(процент, пояснение) или None, если GPU не определился."""
    # NVIDIA
    if shutil.which("nvidia-smi"):
        try:
            out = run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
                       "--format=csv,noheader,nounits"], timeout=5).stdout.splitlines()[0]
            util, used, total = (float(x) for x in out.split(","))
            return util, f"{used / 1024:.1f}/{total / 1024:.1f} GB"
        except Exception:
            pass
    # AMD
    for p in glob.glob("/sys/class/drm/card*/device/gpu_busy_percent"):
        try:
            return float(Path(p).read_text()), ""
        except (OSError, ValueError):
            pass
    # Intel: реальной загрузки без root нет, поэтому берём текущую частоту GPU
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]")):
        candidates = [(f"{card}/gt_act_freq_mhz", f"{card}/gt_max_freq_mhz")]  # i915
        candidates += [(f"{g}/act_freq", f"{g}/max_freq")  # xe
                       for g in glob.glob(f"{card}/device/tile*/gt*/freq0")]
        for act_p, max_p in candidates:
            try:
                act, mx = int(Path(act_p).read_text()), int(Path(max_p).read_text())
                if mx > 0:
                    return 100 * act / mx, f"{act}/{mx} MHz"
            except (OSError, ValueError):
                pass
    return None


def bar(pct: float, width: int = 10) -> str:
    filled = max(0, min(width, round(pct / 100 * width)))
    return "▰" * filled + "▱" * (width - filled)


def render_system() -> str:
    try:
        rows = []
        if cpu_pct is None:
            rows.append("CPU    collecting…")
        else:
            rows.append(f"CPU    {bar(cpu_pct)} {cpu_pct:.0f}%")

        used, total = read_ram()
        rows.append(f"RAM    {bar(used / total * 100)} {used / total * 100:.0f}%  {used:.1f}/{total:.1f} GB")

        gpu = read_gpu()
        if gpu:
            pct, extra = gpu
            rows.append(f"GPU    {bar(pct)} " + (extra if extra and "MHz" in extra else f"{pct:.0f}%  {extra}".strip()))

        rows.append(f"Uptime {fmt_dur(read_uptime())}")
        return "<pre>" + "\n".join(rows) + "</pre>"
    except Exception:
        logging.exception("system stats failed")
        return ""


# ---------- экраны ----------
def render_main():
    infos = [get_info(s) for s in SERVICES]
    up = sum(i["state"] == "active" for i in infos)
    verb = "is" if up < 2 else "are"
    text = (f"<b>Services</b> — {up} from {len(SERVICES)} {verb} up\n\n"
            + "\n".join(line(s, i) for s, i in zip(SERVICES, infos))
            + f"\n\n{render_system()}"
            + f"\n<i>last update at {now_str()}</i>")

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(*[btn(f"{ICONS.get(i['state'], '❔')} {s}", f"s:{n}")
             for n, (s, i) in enumerate(zip(SERVICES, infos))])
    kb.row(btn("🔄 Update", "main"))
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
            kb.row(btn("🔁 Restart", f"restart:{n}"), btn("⏹️ Stop", f"ask:{n}"))
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
    last_refresh = 0.0
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
                    alert = (f"🔄 <b>{esc(svc)}</b> restarted "
                             f"(restarts: {esc(cur['restarts'])})")
                if alert:
                    changed = True
                    if time.time() > quiet_until.get(svc, 0):
                        notify(alert)
            except Exception:
                logging.exception("monitor error for %s", svc)

        due = (PANEL_REFRESH > 0 and current_view == "main"
               and time.time() - last_refresh >= PANEL_REFRESH)
        if (changed and (current_view == "main" or current_view.startswith("s:"))) or due:
            try:
                show(current_view)
                last_refresh = time.time()
            except Exception:
                logging.exception("panel refresh failed")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    if not SERVICES:
        raise SystemExit("SERVICES are empty: made an unit list in .env")
    threading.Thread(target=cpu_sampler, daemon=True).start()
    threading.Thread(target=monitor, daemon=True).start()
    time.sleep(0.8)  # дать cpu_sampler снять первый замер
    if state.get("panel"):
        show("main")
    else:
        send_panel()
    logging.info("bot started, services: %s", SERVICES)
    bot.infinity_polling(skip_pending=True)
