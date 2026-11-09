"""
Об'єднаний сервер моніторингу принтерів HP + відеореєстраторів.

• Старі моделі принтерів (HTML) — опитування через /info_configuration.html
• Нові моделі принтерів (XML)  — опитування через /DevMgmt/*.xml (LEDM API)
• Hikvision DVR/NVR              — ISAPI
• Dahua DVR/NVR                  — CGI + RPC2
• Email/Telegram сповіщення     — при зміні статусу камер/реєстраторів

Запуск : python server.py
Браузер: http://localhost:5001
Потребує: pip install flask requests beautifulsoup4 lxml waitress
"""

import re
import json
import threading
import requests
import sys
import smtplib
import email.utils
import logging
from email.mime.text import MIMEText
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_from_directory, session, redirect, url_for
from functools import wraps
from concurrent.futures import ThreadPoolExecutor
import os

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

BASE_DIR   = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = sys._MEIPASS if getattr(sys, 'frozen', False) else BASE_DIR

app = Flask(__name__, static_folder=STATIC_DIR)
app.secret_key = "printer-monitor-secret-2026"

IP_RE      = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")
SAVE_FILE  = os.path.join(BASE_DIR, "printers.json")
executor   = ThreadPoolExecutor(max_workers=20)

printers: dict = {}
printers_lock  = threading.Lock()

SAVE_KEYS = ("location", "max_pages", "baseline", "model", "pages",
             "cartridge", "cartridge_index", "status", "since_replace", "type", "service_log")

# ===========================================================================
# СПОВІЩЕННЯ (Email + Telegram)
# ===========================================================================

ALERTS_FILE = os.path.join(BASE_DIR, "alerts.json")

_default_alerts = {
    "emails": ["towka8000@gmail.com"],
    "telegram_token": "",
    "telegram_chat_id": "",
    "smtp_user": "towka8000@gmail.com",
    "smtp_password": "ubbgknptdyjrqtkw",
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 465,
    "smtp_use_ssl": True,
    "enable_email": True,
    "enable_telegram": False,
}

alerts_config: dict = {}
alerts_lock = threading.Lock()

# Зберігає останній статус камер для визначення змін
camera_prev_status: dict = {}
camera_status_lock = threading.Lock()


def load_alerts_config():
    global alerts_config
    if os.path.exists(ALERTS_FILE):
        try:
            with open(ALERTS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            alerts_config = {**_default_alerts, **data}
            return
        except (OSError, json.JSONDecodeError):
            pass
    alerts_config = dict(_default_alerts)


def save_alerts_config():
    tmp = ALERTS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(alerts_config, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ALERTS_FILE)
    except OSError as e:
        logging.error(f"Не вдалося зберегти alerts.json: {e}")


def send_telegram(message: str):
    with alerts_lock:
        token   = alerts_config.get("telegram_token", "")
        chat_id = alerts_config.get("telegram_chat_id", "")
        enabled = alerts_config.get("enable_telegram", False)
    if not enabled or not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, data={"chat_id": chat_id, "text": message}, timeout=(3.0, 7.0))
    except Exception as e:
        logging.error(f"Помилка Telegram: {e}")


def send_email(message: str, subject: str = "Моніторинг відеореєстраторів"):
    with alerts_lock:
        enabled   = alerts_config.get("enable_email", True)
        recipients = list(dict.fromkeys(alerts_config.get("emails", [])))
        smtp_user  = alerts_config.get("smtp_user", "")
        smtp_pass  = alerts_config.get("smtp_password", "")
        smtp_srv   = alerts_config.get("smtp_server", "smtp.gmail.com")
        smtp_port  = alerts_config.get("smtp_port", 465)
        use_ssl    = alerts_config.get("smtp_use_ssl", True)
    if not enabled or not recipients or not smtp_user:
        return
    server = None
    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(smtp_srv, smtp_port, timeout=10)
        else:
            server = smtplib.SMTP(smtp_srv, smtp_port, timeout=10)
            server.starttls()
        server.login(smtp_user, smtp_pass)
        for to_email in recipients:
            try:
                msg = MIMEText(message, "plain", "utf-8")
                msg["Subject"]    = subject
                msg["From"]       = smtp_user
                msg["To"]         = to_email
                msg["Date"]       = email.utils.formatdate(localtime=True)
                msg["Message-ID"] = email.utils.make_msgid()
                server.sendmail(smtp_user, [to_email], msg.as_string())
                logging.info(f"Email надіслано на {to_email}")
            except Exception as e:
                logging.error(f"Помилка відправки на {to_email}: {e}")
    except Exception as e:
        logging.error(f"Помилка SMTP: {e}")
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass


def send_alert(message: str):
    """Надіслати сповіщення всіма каналами у фоновому потоці."""
    def _send():
        send_telegram(message)
        send_email(message)
    threading.Thread(target=_send, daemon=True).start()


def check_camera_status_change(ip: str, new_status: str, location: str, brand: str):
    """Порівнює новий статус з попереднім та надсилає сповіщення при зміні."""
    name = f"{ip} [{location}]" if location else ip
    brand_label = "Hikvision" if brand == "hikvision" else "Dahua"

    with camera_status_lock:
        prev = camera_prev_status.get(ip)
        camera_prev_status[ip] = new_status

    # Перший запуск — не сповіщаємо, просто запам'ятовуємо
    if prev is None:
        return

    # Статус не змінився
    if prev == new_status:
        return

    # Перехід у помилку
    is_error = any(w in new_status.lower() for w in ("недоступ", "помилка", "error", "offline", "внимание", "увага"))
    was_error = any(w in (prev or "").lower() for w in ("недоступ", "помилка", "error", "offline", "внимание", "увага"))

    if is_error and not was_error:
        msg = f"🔴 [{brand_label}] {name}\nСтатус: {new_status}\n(був: {prev})"
        send_alert(msg)
        logging.warning(f"ALERT: {msg}")
    elif not is_error and was_error:
        msg = f"🟢 [{brand_label}] {name}\nВідновлено! Статус: {new_status}\n(був: {prev})"
        send_alert(msg)
        logging.info(f"RECOVERY: {msg}")


# ===========================================================================
# АВТОРИЗАЦІЯ
# ===========================================================================

AUTH_LOGIN    = "admin"
AUTH_PASSWORD = "admin123"


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login_page():
    error = ""
    if request.method == "POST":
        if (request.form.get("username") == AUTH_LOGIN and
                request.form.get("password") == AUTH_PASSWORD):
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "Невірний логін або пароль"
    return f"""<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Вхід — Моніторинг</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #1a1d23; color: #e2e8f0;
         min-height: 100vh; display: flex; align-items: center; justify-content: center; }}
  .card {{ background: #111318; border: 1px solid #2d3348; border-radius: 12px;
           padding: 40px 36px; width: 320px; }}
  h2 {{ font-size: 18px; font-weight: 600; margin-bottom: 24px; text-align: center; color: #f1f5f9; }}
  label {{ font-size: 12px; color: #94a3b8; display: block; margin-bottom: 5px; }}
  input {{ width: 100%; background: #1e2330; border: 1px solid #2d3348; border-radius: 6px;
           color: #e2e8f0; font-size: 14px; padding: 9px 12px; outline: none; margin-bottom: 16px; }}
  input:focus {{ border-color: #3b82f6; }}
  button {{ width: 100%; background: #3b82f6; color: #fff; border: none; border-radius: 6px;
            font-size: 14px; font-weight: 500; padding: 10px; cursor: pointer; margin-top: 4px; }}
  button:hover {{ background: #2563eb; }}
  .error {{ color: #f87171; font-size: 13px; text-align: center; margin-bottom: 14px; }}
</style>
</head>
<body>
<div class="card">
  <h2>&#128424; Моніторинг</h2>
  {"<div class='error'>" + error + "</div>" if error else ""}
  <form method="POST">
    <label>Логін</label>
    <input type="text" name="username" autofocus autocomplete="username">
    <label>Пароль</label>
    <input type="password" name="password" autocomplete="current-password">
    <button type="submit">Увійти</button>
  </form>
</div>
</body>
</html>"""


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


# ===========================================================================
# ВАЛІДАЦІЯ
# ===========================================================================

def validate_ip(ip: str) -> bool:
    if not IP_RE.match(ip):
        return False
    return all(0 <= int(o) <= 255 for o in ip.split("."))


# ===========================================================================
# ПРИНТЕРИ — Збереження / Завантаження
# ===========================================================================

def save_printers():
    data = {}
    for ip, p in printers.items():
        data[ip] = {k: p.get(k) for k in SAVE_KEYS}
    tmp_file = SAVE_FILE + ".tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, SAVE_FILE)
    except OSError as e:
        logging.warning(f"Не вдалося зберегти {SAVE_FILE}: {e}")


def load_printers():
    if not os.path.exists(SAVE_FILE):
        return
    try:
        with open(SAVE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logging.warning(f"Не вдалося завантажити {SAVE_FILE}: {e}")
        return
    with printers_lock:
        for ip, saved in data.items():
            printers[ip] = {
                "location":        saved.get("location", ""),
                "model":           saved.get("model", "Опитування..."),
                "pages":           saved.get("pages", "..."),
                "cartridge":       saved.get("cartridge", "..."),
                "cartridge_index": saved.get("cartridge_index", "..."),
                "status":          saved.get("status", "Опитування..."),
                "baseline":        saved.get("baseline"),
                "since_replace":   saved.get("since_replace"),
                "max_pages":       saved.get("max_pages"),
                "type":            saved.get("type", "html"),
                "service_log":     saved.get("service_log", []),
                "polling":         True,
            }
    logging.info(f"Завантажено принтерів: {len(data)}")


# ===========================================================================
# ПРИНТЕРИ — Опитування HTML (старі моделі)
# ===========================================================================

def fetch_printer_html(ip: str):
    result = {
        "model":     "Недоступний",
        "pages":     "—",
        "cartridge": "—",
        "status":    "Недоступний",
        "pages_int": None,
    }

    r_cfg = [None]
    r_sts = [None]
    session_req = requests.Session()

    def get_cfg():
        try:
            r_cfg[0] = session_req.get(
                f"http://{ip}/info_configuration.html?tab=Home&menu=DevConfig", timeout=4)
        except Exception:
            pass

    def get_sts():
        try:
            r_sts[0] = session_req.get(
                f"http://{ip}/info_deviceStatus.html?tab=Home&menu=DevStatus", timeout=4)
        except Exception:
            pass

    t1 = threading.Thread(target=get_cfg)
    t2 = threading.Thread(target=get_sts)
    t1.start(); t2.start()
    t1.join();  t2.join()

    if r_cfg[0] is not None:
        try:
            r_cfg[0].raise_for_status()
            soup = BeautifulSoup(r_cfg[0].text, "html.parser")
            for td in soup.find_all("td"):
                text = td.get_text(strip=True)
                if "Назва продукту:" in text or "Название продукта:" in text:
                    nxt = td.find_next_sibling("td")
                    if nxt: result["model"] = nxt.get_text(strip=True)
                elif "Всього відбитків:" in text or "Всего оттисков:" in text:
                    nxt = td.find_next_sibling("td")
                    if nxt: result["pages"] = nxt.get_text(strip=True)
                elif any(x in text for x in ("Індекс картриджа:", "Индекс картриджа:", "Cartridge Index:", "Supply Index:")):
                    nxt = td.find_next_sibling("td")
                    if nxt: result["cartridge"] = nxt.get_text(strip=True)
            try:
                result["pages_int"] = int(result["pages"].replace(" ", "").replace(",", ""))
            except ValueError:
                pass
        except Exception:
            pass

    if r_sts[0] is not None:
        try:
            r_sts[0].raise_for_status()
            soup2 = BeautifulSoup(r_sts[0].text, "html.parser")
            cell  = soup2.find(id="deviceStatus_tableCell")
            if cell:
                for br in cell.find_all("br"):
                    br.replace_with(" ")
                result["status"] = re.sub(r"\s+", " ", cell.get_text(separator=" ", strip=True)).strip()
            else:
                for td in soup2.find_all("td"):
                    text = td.get_text(strip=True)
                    if "Стан:" in text or "Состояние:" in text:
                        after = text.split(":", 1)[-1].strip()
                        if after:
                            result["status"] = re.sub(r"\s+", " ", after)
                            break
                        nxt = td.find_next_sibling("td")
                        if nxt:
                            result["status"] = re.sub(r"\s+", " ", nxt.get_text(strip=True))
                            break
        except Exception:
            pass

    _apply_printer_result(ip, result, printer_type="html")


# ===========================================================================
# ПРИНТЕРИ — Опитування XML (нові моделі LEDM)
# ===========================================================================

def fetch_printer_xml(ip: str):
    result = {
        "model":           "Недоступний",
        "pages":           "—",
        "cartridge_index": "—",
        "status":          "Недоступний",
        "pages_int":       None,
    }

    r_cfg = [None]; r_usg = [None]; r_sts = [None]
    session_req = requests.Session()

    def get_cfg():
        try:
            r_cfg[0] = session_req.get(f"http://{ip}/DevMgmt/ProductConfigDyn.xml", timeout=4)
        except Exception:
            pass

    def get_usg():
        try:
            r_usg[0] = session_req.get(f"http://{ip}/DevMgmt/ProductUsageDyn.xml", timeout=4)
        except Exception:
            pass

    def get_sts():
        try:
            r_sts[0] = session_req.get(f"http://{ip}/DevMgmt/ProductStatusDyn.xml", timeout=4)
        except Exception:
            pass

    t1 = threading.Thread(target=get_cfg)
    t2 = threading.Thread(target=get_usg)
    t3 = threading.Thread(target=get_sts)
    t1.start(); t2.start(); t3.start()
    t1.join();  t2.join();  t3.join()

    if r_cfg[0] is not None:
        try:
            r_cfg[0].raise_for_status()
            r_cfg[0].encoding = "utf-8"
            soup = BeautifulSoup(r_cfg[0].text, "xml")
            tag  = soup.find("MakeAndModel")
            if tag: result["model"] = tag.get_text(strip=True)
        except Exception:
            pass

    if r_usg[0] is not None:
        try:
            r_usg[0].raise_for_status()
            r_usg[0].encoding = "utf-8"
            soup2 = BeautifulSoup(r_usg[0].text, "xml")
            psub  = soup2.find("PrinterSubunit")
            if psub:
                ti = psub.find("TotalImpressions")
                if ti:
                    result["pages"] = ti.get_text(strip=True)
                    try: result["pages_int"] = int(result["pages"])
                    except ValueError: pass
            idx = soup2.find("OriginalHPIndex")
            if idx: result["cartridge_index"] = idx.get_text(strip=True)
        except Exception:
            pass

    if r_sts[0] is not None:
        try:
            r_sts[0].raise_for_status()
            r_sts[0].encoding = "utf-8"
            soup3 = BeautifulSoup(r_sts[0].text, "xml")
            statuses_ru  = []
            statuses_any = []
            for tag in soup3.find_all("LocString"):
                text = tag.get_text(strip=True)
                if not text: continue
                if tag.get("lang") == "ru": statuses_ru.append(text)
                else: statuses_any.append(text)
            statuses = statuses_ru if statuses_ru else statuses_any
            if statuses: result["status"] = statuses[0]
        except Exception:
            pass

    _apply_printer_result(ip, result, printer_type="xml")


def _apply_printer_result(ip: str, result: dict, printer_type: str):
    with printers_lock:
        if ip not in printers:
            return
        p = printers[ip]
        p["model"]   = result["model"]
        p["pages"]   = result["pages"]
        p["status"]  = result["status"]
        p["polling"] = False
        if printer_type == "html":
            p["cartridge"]       = result.get("cartridge", "—")
            p["cartridge_index"] = "—"
        else:
            p["cartridge"]       = "—"
            p["cartridge_index"] = result.get("cartridge_index", "—")
        pages_int = result.get("pages_int")
        if pages_int is not None:
            if p["baseline"] is None:
                p["baseline"] = pages_int
            p["since_replace"] = pages_int - p["baseline"]
        else:
            p["since_replace"] = None
        save_printers()


def poll_printer(ip: str):
    p_type = printers.get(ip, {}).get("type", "html")
    target = fetch_printer_xml if p_type == "xml" else fetch_printer_html
    executor.submit(target, ip)


def _load_saved_printer(ip: str) -> dict:
    try:
        if os.path.exists(SAVE_FILE):
            with open(SAVE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return data.get(ip, {})
    except (OSError, json.JSONDecodeError):
        pass
    return {}


# ===========================================================================
# ПРИНТЕРИ — Flask API
# ===========================================================================

@app.route("/")
@login_required
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/printers", methods=["GET"])
@login_required
def get_printers():
    with printers_lock:
        result = []
        for ip, p in printers.items():
            max_pages     = p.get("max_pages")
            since_replace = p.get("since_replace")
            color = "none"
            if since_replace is not None and max_pages:
                pct = since_replace / max_pages
                color = "red" if pct >= 1.0 else "yellow" if pct >= 0.7 else "green"
            result.append({
                "ip":              ip,
                "location":        p.get("location", ""),
                "model":           p.get("model", "—"),
                "pages":           p.get("pages", "—"),
                "cartridge":       p.get("cartridge", "—"),
                "cartridge_index": p.get("cartridge_index", "—"),
                "status":          p.get("status", "—"),
                "since_replace":   since_replace if since_replace is not None else "—",
                "max_pages":       max_pages if max_pages else "—",
                "color":           color,
                "polling":         p.get("polling", False),
                "type":            p.get("type", "html"),
                "service_log":     p.get("service_log", []),
            })
    return jsonify(result)


@app.route("/add", methods=["POST"])
@login_required
def add_printer():
    ip       = request.form.get("ip", "").strip()
    location = request.form.get("location", "").strip()
    max_str  = request.form.get("max", "").strip()
    p_type   = request.form.get("type", "html").strip().lower()
    if not validate_ip(ip):
        return jsonify({"error": "Невірний формат IP"}), 400
    if p_type not in ("html", "xml"):
        return jsonify({"error": "Тип повинен бути html або xml"}), 400
    max_pages = None
    if max_str:
        try: max_pages = int(max_str)
        except ValueError: return jsonify({"error": "Макс. сторінок — ціле число"}), 400
    saved = _load_saved_printer(ip)  # читаємо файл ДО блокування
    with printers_lock:
        if ip in printers:
            return jsonify({"error": "Вже додано"}), 409
        printers[ip] = {
            "location":        location or saved.get("location", ""),
            "model":           saved.get("model", "Опитування..."),
            "pages":           saved.get("pages", "..."),
            "cartridge":       saved.get("cartridge", "..."),
            "cartridge_index": saved.get("cartridge_index", "..."),
            "status":          saved.get("status", "Опитування..."),
            "baseline":        saved.get("baseline"),
            "since_replace":   saved.get("since_replace"),
            "max_pages":       max_pages if max_pages is not None else saved.get("max_pages"),
            "type":            p_type,
            "service_log":     saved.get("service_log", []),
            "polling":         True,
        }
        save_printers()
    poll_printer(ip)
    return jsonify({"ok": True})


@app.route("/delete/<ip>", methods=["POST"])
@login_required
def delete_printer(ip):
    with printers_lock:
        printers.pop(ip, None)
        save_printers()
    return jsonify({"ok": True})


@app.route("/refresh", methods=["POST"])
@login_required
def refresh_all():
    with printers_lock:
        ips = list(printers.keys())
        for ip in ips:
            printers[ip]["polling"] = True
    for ip in ips:
        poll_printer(ip)
    return jsonify({"ok": True})


@app.route("/refresh/<ip>", methods=["POST"])
@login_required
def refresh_one(ip):
    with printers_lock:
        if ip not in printers:
            return jsonify({"error": "Не знайдено"}), 404
        printers[ip]["polling"] = True
    poll_printer(ip)
    return jsonify({"ok": True})


@app.route("/update/<ip>", methods=["POST"])
@login_required
def update_printer(ip):
    with printers_lock:
        if ip not in printers:
            return jsonify({"error": "Не знайдено"}), 404
        p = printers[ip]
        if "location" in request.form:
            p["location"] = request.form["location"].strip()
        if "max_pages" in request.form:
            val = request.form["max_pages"].strip()
            if val == "":
                p["max_pages"] = None
            else:
                try: p["max_pages"] = int(val)
                except ValueError: return jsonify({"error": "Макс. сторінок — ціле число"}), 400
        save_printers()
    return jsonify({"ok": True})


@app.route("/replace/<ip>", methods=["POST"])
@login_required
def replace_cartridge(ip):
    with printers_lock:
        if ip not in printers:
            return jsonify({"error": "Не знайдено"}), 404
        p = printers[ip]
        pages_raw = str(p.get("pages", "")).replace(" ", "").replace(",", "")
        try: current = int(pages_raw)
        except ValueError:
            return jsonify({"error": "Принтер недоступний або дані ще не отримано."}), 400
        p["baseline"]      = current
        p["since_replace"] = 0
        save_printers()
    return jsonify({"ok": True})


@app.route("/service/<ip>", methods=["POST"])
@login_required
def add_service(ip):
    with printers_lock:
        if ip not in printers:
            return jsonify({"error": "Не знайдено"}), 404
        entry = {
            "date":    request.form.get("date", "").strip(),
            "type":    request.form.get("type", "").strip(),
            "comment": request.form.get("comment", "").strip(),
        }
        if not entry["date"] or not entry["type"]:
            return jsonify({"error": "Дата і тип обов'язкові"}), 400
        log = printers[ip].setdefault("service_log", [])
        log.insert(0, entry)
        save_printers()
    return jsonify({"ok": True})


@app.route("/service/<ip>/<int:idx>", methods=["DELETE"])
@login_required
def delete_service(ip, idx):
    with printers_lock:
        if ip not in printers:
            return jsonify({"error": "Не знайдено"}), 404
        log = printers[ip].get("service_log", [])
        if idx < 0 or idx >= len(log):
            return jsonify({"error": "Запис не знайдено"}), 404
        log.pop(idx)
        save_printers()
    return jsonify({"ok": True})


# ===========================================================================
# ВІДЕОНАГЛАД — Збереження / Завантаження
# ===========================================================================

CAMERAS_FILE = os.path.join(BASE_DIR, "cameras.json")

cameras: dict = {}
cameras_lock  = threading.Lock()

CAM_SAVE_KEYS = ("location", "brand", "username", "password",
                 "model", "channels_total", "channels_online",
                 "disk_total_gb", "disk_used_gb", "status")


def save_cameras():
    data = {}
    for ip, c in cameras.items():
        data[ip] = {k: c.get(k) for k in CAM_SAVE_KEYS}
    tmp = CAMERAS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CAMERAS_FILE)
    except OSError as e:
        logging.warning(f"Не вдалося зберегти cameras.json: {e}")


def load_cameras():
    if not os.path.exists(CAMERAS_FILE):
        return
    try:
        with open(CAMERAS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logging.warning(f"Не вдалося завантажити cameras.json: {e}")
        return
    with cameras_lock:
        for ip, saved in data.items():
            cameras[ip] = {
                "location":        saved.get("location", ""),
                "brand":           saved.get("brand", "hikvision"),
                "username":        saved.get("username", "admin"),
                "password":        saved.get("password", ""),
                "model":           saved.get("model", "Опитування..."),
                "channels_total":  saved.get("channels_total"),
                "channels_online": saved.get("channels_online"),
                "disk_total_gb":   saved.get("disk_total_gb"),
                "disk_used_gb":    saved.get("disk_used_gb"),
                "status":          saved.get("status", "Опитування..."),
                "polling":         True,
            }
    logging.info(f"Завантажено камер/DVR: {len(data)}")


# ===========================================================================
# ВІДЕОНАГЛАД — Опитування Hikvision (ISAPI)
# ===========================================================================

def fetch_camera_hikvision(ip: str, username: str, password: str):
    from requests.auth import HTTPDigestAuth
    auth   = HTTPDigestAuth(username, password)
    result = {
        "model":           "Недоступний",
        "channels_total":  None,
        "channels_online": None,
        "disk_total_gb":   None,
        "disk_used_gb":    None,
        "status":          "Недоступний",
    }
    sess = requests.Session()

    def get_info():
        try:
            r = sess.get(f"http://{ip}/ISAPI/System/deviceInfo", auth=auth, timeout=5)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "xml")
                tag  = soup.find("deviceName") or soup.find("model")
                if tag: result["model"] = tag.get_text(strip=True)
                if result["status"] == "Недоступний":
                    result["status"] = "Онлайн"
        except Exception:
            pass

    def get_channels():
        # Спочатку пробуємо NVR endpoint (InputProxy) — повертає і total і online разом
        try:
            r = sess.get(f"http://{ip}/ISAPI/ContentMgmt/InputProxy/channels/status", auth=auth, timeout=5)
            if r.status_code == 200:
                soup  = BeautifulSoup(r.text, "xml")
                items = soup.find_all("InputProxyChannelStatus")
                if items:
                    result["channels_total"]  = len(items)
                    result["channels_online"] = sum(
                        1 for t in items
                        if t.find("online") and t.find("online").get_text(strip=True).lower() == "true"
                    )
                    return
        except Exception:
            pass
        # Fallback — DVR endpoint (Video inputs)
        try:
            r = sess.get(f"http://{ip}/ISAPI/System/Video/inputs/channels", auth=auth, timeout=5)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "xml")
                result["channels_total"] = len(soup.find_all("VideoInputChannel"))
        except Exception:
            pass

    def get_channels_status():
        # Якщо канали вже заповнені через InputProxy — пропускаємо
        if result.get("channels_online") is not None:
            return
        try:
            r = sess.get(f"http://{ip}/ISAPI/System/Video/inputs/channels/status", auth=auth, timeout=5)
            if r.status_code == 200:
                soup   = BeautifulSoup(r.text, "xml")
                online = sum(1 for t in soup.find_all("online")
                             if t.get_text(strip=True).lower() == "true")
                result["channels_online"] = online
        except Exception:
            pass

    def get_storage():
        try:
            r = sess.get(f"http://{ip}/ISAPI/ContentMgmt/Storage", auth=auth, timeout=5)
            if r.status_code == 200:
                soup      = BeautifulSoup(r.text, "xml")
                work_mode = soup.find("workMode")
                is_quota  = work_mode and work_mode.get_text(strip=True).lower() == "quota"
                total_mb  = sum(int(t.get_text()) for t in soup.find_all("capacity")  if t.get_text().isdigit())
                free_mb   = sum(int(t.get_text()) for t in soup.find_all("freeSpace") if t.get_text().isdigit())
                hdd_statuses = [t.get_text(strip=True).lower() for t in soup.find_all("status")]
                hdd_error    = any(s not in ("ok", "normal", "") for s in hdd_statuses)
                if total_mb:
                    result["disk_total_gb"] = round(total_mb / 1024)
                    if is_quota:
                        result["disk_used_gb"] = round(total_mb / 1024)
                    else:
                        result["disk_used_gb"] = round((total_mb - free_mb) / 1024)
                        used_pct = (total_mb - free_mb) / total_mb
                        if used_pct >= 0.95:
                            result["status"] = "Увага: диск заповнений"
                if hdd_error:
                    result["status"] = "Помилка диска"
        except Exception:
            pass

    threads = [
        threading.Thread(target=get_info),
        threading.Thread(target=get_channels),
        threading.Thread(target=get_channels_status),
        threading.Thread(target=get_storage),
    ]
    for t in threads: t.start()
    for t in threads: t.join()

    _apply_camera_result(ip, result)


# ===========================================================================
# ВІДЕОНАГЛАД — Опитування Dahua (CGI + RPC2)
# ===========================================================================

def fetch_camera_dahua(ip: str, username: str, password: str):
    result = {
        "model":           "Недоступний",
        "channels_total":  None,
        "channels_online": None,
        "disk_total_gb":   None,
        "disk_used_gb":    None,
        "status":          "Недоступний",
    }
    sess = requests.Session()

    def parse_kv(text):
        d = {}
        for line in text.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                d[k.strip()] = v.strip()
        return d

    def _req(url):
        from requests.auth import HTTPDigestAuth, HTTPBasicAuth
        try:
            r = sess.get(url, auth=HTTPDigestAuth(username, password), timeout=6)
            if r.status_code in (401, 403):
                r = sess.get(url, auth=HTTPBasicAuth(username, password), timeout=6)
            return r
        except Exception:
            return None

    def parse_mb(val: str) -> int:
        val = val.strip()
        if val.upper().endswith("KB"): return int(val[:-2]) // 1024
        if val.endswith("M"):          return int(val[:-1])
        v = int(val)
        # Значення > 1 GB — швидше за все байти (HCVR pre-2016 віддає байти)
        return v // (1024 * 1024) if v > 1_000_000_000 else v

    def _apply_disk(total_mb: int, free_mb: int, hdd_errors: bool = False):
        if total_mb <= 0:
            return
        result["disk_total_gb"] = round(total_mb / 1024)
        used_mb = total_mb - free_mb
        result["disk_used_gb"]  = round(used_mb / 1024)
        if hdd_errors:
            result["status"] = "Помилка диска"
        elif total_mb > 0 and used_mb / total_mb >= 0.95:
            result["status"] = "Увага: диск заповнений"

    def get_info():
        for url in [
            f"http://{ip}/cgi-bin/magicBox.cgi?action=getDeviceType",
            f"http://{ip}/cgi-bin/global.cgi?action=getMachineName",
            f"http://{ip}/cgi-bin/magicBox.cgi?action=getMachineName",
            f"http://{ip}/cgi-bin/magicBox.cgi?action=getSoftwareVersion",
        ]:
            r = _req(url)
            if r is not None and r.status_code == 200 and "error" not in r.text[:20].lower():
                kv    = parse_kv(r.text)
                model = kv.get("type") or kv.get("name") or kv.get("machineName") or kv.get("version")
                if model: result["model"] = model
                # Пристрій відповідає — він онлайн (встановлюємо безумовно)
                result["status"] = "Онлайн"
                return

    def get_channels():
        # 1. Получаем общее количество каналов (аналог + IP)
        r = _req(f"http://{ip}/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle")
        if r is not None and r.status_code == 200 and "error" not in r.text[:20].lower():
            result["status"] = "Онлайн"
            count = sum(1 for ln in r.text.splitlines() if "Name=" in ln)
            if count: result["channels_total"] = count

        # 2. Проверяем статус IP-камер (если добавлены)
        r_ip = _req(f"http://{ip}/cgi-bin/configManager.cgi?action=getConfig&name=RemoteDevice")
        ip_online = 0
        if r_ip is not None and r_ip.status_code == 200:
            # Ищем статус подключения, обычно 1 или 2 означают коннект
            ip_online = sum(1 for ln in r_ip.text.splitlines() if ".State=1" in ln or ".State=2" in ln or ".Status=1" in ln)

        # 3. Проверяем аналоговые каналы на Video Loss (потерю видео)
        r_loss = _req(f"http://{ip}/cgi-bin/devVideoInput.cgi?action=getMatrix")
        analog_online = 0
        if r_loss is not None and r_loss.status_code == 200:
            # Если вернулась матрица, считаем активные сигналы (не Loss)
            analog_online = sum(1 for ln in r_loss.text.splitlines() if "SignalLoss" not in ln and "State=1" in ln)
        else:
            # Запасной вариант для старых HCVR: проверяем битрейт (если > 0, камера показывает)
            r_stat = _req(f"http://{ip}/cgi-bin/media.cgi?action=getRealMediaStat")
            if r_stat is not None and r_stat.status_code == 200:
                analog_online = sum(1 for ln in r_stat.text.splitlines() if ".BitRate=" in ln and not ln.endswith("=0"))

        total_online = ip_online + analog_online

        # Защита от нулевого значения, если API не отдало статистику
        if total_online > 0:
            result["channels_online"] = total_online
        elif result.get("channels_total"):
            result["channels_online"] = result["channels_total"]  # Fallback

    def _rpc2_get_disk():
        import hashlib
        base = f"http://{ip}"

        def _try_login(hash_fn):
            try:
                rpc = requests.Session()
                r1  = rpc.post(f"{base}/RPC2_Login", json={
                    "method": "global.login",
                    "params": {"userName": username, "password": "",
                               "clientType": "Web3.0", "authorityType": "Default"},
                    "id": 1
                }, timeout=7)
                if r1.status_code != 200: return None, None
                d1   = r1.json()
                p1   = d1.get("params", {})
                rval = p1.get("random", ""); realm = p1.get("realm", "")
                s    = d1.get("session", 0)
                auth = hash_fn(rval, realm)
                r2   = rpc.post(f"{base}/RPC2_Login", json={
                    "method": "global.login",
                    "params": {"userName": username, "password": auth,
                               "clientType": "Web3.0", "authorityType": "Default"},
                    "id": 2, "session": s
                }, timeout=7)
                if r2.status_code != 200: return None, None
                d2 = r2.json()
                if d2.get("result"):
                    return rpc, d2.get("session", s)
            except Exception:
                pass
            return None, None

        def scheme1(rval, realm):
            h1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest().upper()
            return hashlib.md5(f"{username}:{rval}:{h1}".encode()).hexdigest().upper()

        def scheme2(rval, realm):
            h1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
            return hashlib.md5(f"{username}:{rval}:{h1}".encode()).hexdigest()

        rpc, sess = _try_login(scheme1)
        if rpc is None:
            rpc, sess = _try_login(scheme2)
        if rpc is None:
            return

        def _rpc2_get_disk():
            try:
                r = rpc.post(f"{base}/RPC2", json={
                    "method": "storage.getDeviceAllInfo",
                    "params": None, "id": 10, "session": sess
                }, timeout=7)
                if r.status_code == 200:
                    d = r.json()
                    if d.get("result"):
                        return d.get("params", {}).get("info", d.get("params"))
            except Exception:
                pass
            return None

        storage_urls = [
            # HCVR5108H-S2 / HCVR7116H (прошивки 2014-2016) — основні ендпоінти
            f"http://{ip}/cgi-bin/storageDevice.cgi?action=getDeviceAllInfo",
            f"http://{ip}/cgi-bin/devStorage.cgi?action=getDeviceInfo",
            f"http://{ip}/cgi-bin/devStorage.cgi?action=getDeviceAllInfo",
            f"http://{ip}/cgi-bin/devStorage.cgi?action=getInfo",
            # Сучасні Dahua DVR/NVR
            f"http://{ip}/cgi-bin/storageDevice.cgi?action=getHddInfo",
            f"http://{ip}/cgi-bin/storageDevice.cgi?action=getHddInfoAll",
            f"http://{ip}/cgi-bin/storageDevice.cgi?action=getStorageDeviceInfo",
            f"http://{ip}/cgi-bin/storageDevice.cgi?action=getCapacity",
            f"http://{ip}/cgi-bin/configManager.cgi?action=getConfig&name=HddManager",
            f"http://{ip}/cgi-bin/configManager.cgi?action=getConfig&name=StorageGlobal",
            # Старі HCVR5 (pre-2016 прошивки)
            f"http://{ip}/cgi-bin/configManager.cgi?action=getConfig&name=Storage",
            f"http://{ip}/cgi-bin/configManager.cgi?action=getConfig&name=StorageInfo",
            f"http://{ip}/cgi-bin/configManager.cgi?action=getConfig&name=HddInfo",
            f"http://{ip}/cgi-bin/configManager.cgi?action=getConfig&name=LogicInfo",
            f"http://{ip}/cgi-bin/configManager.cgi?action=getConfig&name=Disk",
            f"http://{ip}/cgi-bin/magicBox.cgi?action=getSystemInfo",
        ]
        cgi_ok = False
        for url in storage_urls:
            r = _req(url)
            if r is None or r.status_code != 200 or not r.text.strip():
                continue
            body = r.text.strip()
            # Деякі прошивки повертають "Error" або "No Permission" в тілі при HTTP 200
            first = body[:40].lower()
            if first.startswith("error") or first.startswith("no permission"):
                continue
            # --- Пряма перевірка Status=Error / Status=None ---
            if "Status=Error" in r.text or "Status=None" in r.text:
                result["status"] = "Помилка диска"
                cgi_ok = True
                break

            kv = parse_kv(r.text)
            total_mb = free_mb = 0; hdd_errors = False
            # Підтримуємо всі можливі префікси ключів (різні прошивки)
            for prefix in ("hdd", "disk", "storage", "Disk", "HDD",
                           "table.hdd", "table.disk"):  # HCVR pre-2016 може мати "table." префікс
                idx = 0; found = False
                while True:
                    # Формат prefix[N].TotalSpace
                    tk = f"{prefix}[{idx}].TotalSpace"
                    fk = f"{prefix}[{idx}].FreeSpace"
                    sk = f"{prefix}[{idx}].Status"
                    # Формат prefix[N].TotalBytes (байти, деякі прошивки)
                    tb = f"{prefix}[{idx}].TotalBytes"
                    fb = f"{prefix}[{idx}].FreeBytes"
                    if tk not in kv and tb not in kv:
                        # Формат без індексу: prefix.TotalSpace
                        bare_tk = f"{prefix}.TotalSpace"
                        bare_tb = f"{prefix}.TotalBytes"
                        if idx == 0:
                            if bare_tk in kv:
                                try:
                                    total_mb += parse_mb(kv[bare_tk])
                                    free_mb  += parse_mb(kv.get(f"{prefix}.FreeSpace", "0"))
                                    found = True
                                except (ValueError, KeyError): pass
                            elif bare_tb in kv:
                                try:
                                    total_mb += parse_mb(kv[bare_tb])
                                    free_mb  += parse_mb(kv.get(f"{prefix}.FreeBytes", "0"))
                                    found = True
                                except (ValueError, KeyError): pass
                        break
                    found = True
                    try:
                        if tk in kv:
                            total_mb += parse_mb(kv[tk])
                            free_mb  += parse_mb(kv.get(fk, "0"))
                        elif tb in kv:
                            total_mb += parse_mb(kv[tb])
                            free_mb  += parse_mb(kv.get(fb, "0"))
                    except (ValueError, KeyError): pass
                    st = kv.get(sk, "").lower()
                    if st and st not in ("normal", "ok", ""):
                        hdd_errors = True
                    idx += 1
                if found: break
            if total_mb:
                _apply_disk(total_mb, free_mb, hdd_errors)
                cgi_ok = True
                break

        if cgi_ok: return

        rpc_data = _rpc2_get_disk()
        if rpc_data is None: return

        total_mb = free_mb = 0
        if isinstance(rpc_data, list):
            for disk in rpc_data:
                try:
                    total_mb += int(disk.get("totalBytes", disk.get("totalSpace", 0))) // (1024 * 1024)
                    free_mb  += int(disk.get("freeBytes",  disk.get("freeSpace",  0))) // (1024 * 1024)
                except (TypeError, ValueError): pass
        elif isinstance(rpc_data, dict):
            try:
                total_mb = int(rpc_data.get("totalBytes", rpc_data.get("total", 0))) // (1024 * 1024)
                free_mb  = int(rpc_data.get("freeBytes",  rpc_data.get("free",  0))) // (1024 * 1024)
            except (TypeError, ValueError): pass

        if total_mb:
            _apply_disk(total_mb, free_mb)

    threads = [
        threading.Thread(target=get_info),
        threading.Thread(target=get_channels),
        threading.Thread(target=get_storage),
    ]
    for t in threads: t.start()
    for t in threads: t.join()

    _apply_camera_result(ip, result)


def _apply_camera_result(ip: str, result: dict):
    with cameras_lock:
        if ip not in cameras:
            return
        c        = cameras[ip]
        location = c.get("location", "")
        brand    = c.get("brand", "hikvision")
        new_status = result["status"]
        c.update({
            "model":           result["model"],
            "channels_total":  result["channels_total"],
            "channels_online": result["channels_online"],
            "disk_total_gb":   result["disk_total_gb"],
            "disk_used_gb":    result["disk_used_gb"],
            "status":          new_status,
            "polling":         False,
        })
        save_cameras()

    # Перевіряємо зміну статусу та надсилаємо сповіщення (поза блокуванням)
    check_camera_status_change(ip, new_status, location, brand)


def poll_camera(ip: str):
    with cameras_lock:
        c     = cameras.get(ip, {})
        brand = c.get("brand", "hikvision")
        user  = c.get("username", "admin")
        pwd   = c.get("password", "")
    if brand == "dahua":
        executor.submit(fetch_camera_dahua, ip, user, pwd)
    else:
        executor.submit(fetch_camera_hikvision, ip, user, pwd)


# ===========================================================================
# ВІДЕОНАГЛАД — Flask API
# ===========================================================================

@app.route("/cameras")
@login_required
def cameras_page():
    return send_from_directory(STATIC_DIR, "cameras.html")


@app.route("/cameras/list", methods=["GET"])
@login_required
def get_cameras():
    with cameras_lock:
        result = []
        for ip, c in cameras.items():
            result.append({
                "ip":             ip,
                "location":       c.get("location", ""),
                "brand":          c.get("brand", "hikvision"),
                "username":       c.get("username", ""),
                "model":          c.get("model", "—"),
                "channels_total": c.get("channels_total"),
                "channels_online":c.get("channels_online"),
                "disk_total_gb":  c.get("disk_total_gb"),
                "disk_used_gb":   c.get("disk_used_gb"),
                "status":         c.get("status", "—"),
                "polling":        c.get("polling", False),
            })
    return jsonify(result)


@app.route("/cameras/add", methods=["POST"])
@login_required
def add_camera():
    ip       = request.form.get("ip", "").strip()
    location = request.form.get("location", "").strip()
    brand    = request.form.get("brand", "hikvision").strip().lower()
    username = request.form.get("username", "admin").strip()
    password = request.form.get("password", "").strip()
    if not validate_ip(ip):
        return jsonify({"error": "Невірний формат IP"}), 400
    if brand not in ("hikvision", "dahua"):
        return jsonify({"error": "Бренд повинен бути hikvision або dahua"}), 400
    if not username:
        return jsonify({"error": "Введіть логін"}), 400
    if not password:
        return jsonify({"error": "Введіть пароль"}), 400
    with cameras_lock:
        if ip in cameras:
            return jsonify({"error": "Вже додано"}), 409
        cameras[ip] = {
            "location":        location,
            "brand":           brand,
            "username":        username,
            "password":        password,
            "model":           "Опитування...",
            "channels_total":  None,
            "channels_online": None,
            "disk_total_gb":   None,
            "disk_used_gb":    None,
            "status":          "Опитування...",
            "polling":         True,
        }
        save_cameras()
    poll_camera(ip)
    return jsonify({"ok": True})


@app.route("/cameras/delete/<ip>", methods=["POST"])
@login_required
def delete_camera(ip):
    with cameras_lock:
        cameras.pop(ip, None)
        save_cameras()
    with camera_status_lock:
        camera_prev_status.pop(ip, None)
    return jsonify({"ok": True})


@app.route("/cameras/refresh", methods=["POST"])
@login_required
def refresh_cameras_all():
    with cameras_lock:
        ips = list(cameras.keys())
        for ip in ips:
            cameras[ip]["polling"] = True
    for ip in ips:
        poll_camera(ip)
    return jsonify({"ok": True})


@app.route("/cameras/refresh/<ip>", methods=["POST"])
@login_required
def refresh_camera_one(ip):
    with cameras_lock:
        if ip not in cameras:
            return jsonify({"error": "Не знайдено"}), 404
        cameras[ip]["polling"] = True
    poll_camera(ip)
    return jsonify({"ok": True})


@app.route("/cameras/update/<ip>", methods=["POST"])
@login_required
def update_camera(ip):
    with cameras_lock:
        if ip not in cameras:
            return jsonify({"error": "Не знайдено"}), 404
        c = cameras[ip]
        if "location" in request.form:
            c["location"] = request.form["location"].strip()
        if "brand" in request.form:
            b = request.form["brand"].strip().lower()
            if b in ("hikvision", "dahua"): c["brand"] = b
        if "username" in request.form:
            c["username"] = request.form["username"].strip()
        if "password" in request.form and request.form["password"].strip():
            c["password"] = request.form["password"].strip()
        save_cameras()
    return jsonify({"ok": True})


# ===========================================================================
# СПОВІЩЕННЯ — Flask API
# ===========================================================================

@app.route("/alerts")
@login_required
def alerts_page():
    return send_from_directory(STATIC_DIR, "alerts.html")


@app.route("/alerts/config", methods=["GET"])
@login_required
def get_alerts_config():
    with alerts_lock:
        # Повертаємо конфіг без пароля SMTP
        safe = {k: v for k, v in alerts_config.items() if k != "smtp_password"}
        safe["smtp_password_set"] = bool(alerts_config.get("smtp_password"))
    return jsonify(safe)


@app.route("/alerts/config", methods=["POST"])
@login_required
def update_alerts_config():
    with alerts_lock:
        data = request.get_json(silent=True) or {}
        if "emails" in data:
            emails = data["emails"]
            if isinstance(emails, list):
                valid = [e for e in emails if re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', str(e))]
                alerts_config["emails"] = valid
        if "telegram_token"   in data: alerts_config["telegram_token"]   = str(data["telegram_token"]).strip()
        if "telegram_chat_id" in data: alerts_config["telegram_chat_id"] = str(data["telegram_chat_id"]).strip()
        if "smtp_user"        in data: alerts_config["smtp_user"]        = str(data["smtp_user"]).strip()
        if "smtp_password"    in data and data["smtp_password"]:
            alerts_config["smtp_password"] = str(data["smtp_password"]).strip()
        if "smtp_server"      in data: alerts_config["smtp_server"]      = str(data["smtp_server"]).strip()
        if "smtp_port"        in data:
            try: alerts_config["smtp_port"] = int(data["smtp_port"])
            except (ValueError, TypeError): pass
        if "smtp_use_ssl"     in data: alerts_config["smtp_use_ssl"]     = bool(data["smtp_use_ssl"])
        if "enable_email"     in data: alerts_config["enable_email"]     = bool(data["enable_email"])
        if "enable_telegram"  in data: alerts_config["enable_telegram"]  = bool(data["enable_telegram"])
        save_alerts_config()
    return jsonify({"ok": True})


@app.route("/alerts/test", methods=["POST"])
@login_required
def test_alert():
    """Надіслати тестове сповіщення."""
    channel = request.get_json(silent=True, force=True) or {}
    ch = channel.get("channel", "all")
    msg = "✅ Тестове сповіщення від системи моніторингу."
    if ch == "email":
        threading.Thread(target=send_email, args=(msg,), daemon=True).start()
    elif ch == "telegram":
        threading.Thread(target=send_telegram, args=(msg,), daemon=True).start()
    else:
        send_alert(msg)
    return jsonify({"ok": True})


# ===========================================================================
# ЗАПУСК
# ===========================================================================

if __name__ == "__main__":
    load_alerts_config()
    load_printers()
    load_cameras()

    with printers_lock:
        ips = list(printers.keys())
    for ip in ips:
        poll_printer(ip)

    with cameras_lock:
        cam_ips = list(cameras.keys())
    for ip in cam_ips:
        poll_camera(ip)

    # Надсилаємо стартовий звіт
    def _startup_report():
        import time; time.sleep(15)  # Чекаємо поки перший цикл опитування завершиться
        with cameras_lock:
            lines = []
            for ip, c in cameras.items():
                loc   = f" [{c['location']}]" if c.get("location") else ""
                brand = "Hikvision" if c.get("brand") == "hikvision" else "Dahua"
                st    = c.get("status", "—")
                icon  = "🟢" if st in ("Онлайн", "Online") else "🔴"
                lines.append(f"{icon} {ip}{loc} ({brand}): {st}")
                # Ініціалізуємо попередній статус
                with camera_status_lock:
                    camera_prev_status[ip] = st
        if lines:
            report = "🚀 Запуск моніторингу відеореєстраторів:\n\n" + "\n".join(lines)
            send_alert(report)

    threading.Thread(target=_startup_report, daemon=True).start()

    logging.info("Сервер запущено: http://localhost:5001")
    try:
        from waitress import serve
        logging.info("Використовується production-сервер waitress")
        serve(app, host="0.0.0.0", port=5001)
    except ImportError:
        logging.warning("waitress не встановлено, використовується вбудований Flask")
        app.run(host="0.0.0.0", port=5001, debug=False)
