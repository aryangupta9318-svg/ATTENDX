"""
ATTENDX - Student Attendance Tracking System (Python web app, v2)

Python runs the server, validates every action and saves everything to
attendx_data.json. Standard library only - no installs.

Run:   python app.py
Open:  http://localhost:8000
Phone: python app.py --network   (then open http://<your-pc-ip>:8000)

Attendance rules (shown in the UI too)
  Present, Late = 1 day attended | Half-day = 0.5 | Absent = 0
  Leave (approved) is NOT counted in the percentage.
  Percentage = attended / (Present+Late+Half+Absent) x 100
"""

import csv
import io
import json
import shutil
import sys
import threading
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent
DATA_FILE = BASE / "attendx_data.json"
INDEX_FILE = BASE / "index.html"
PORT = 8000
LOCK = threading.Lock()
STATUS = ("P", "A", "L", "H")          # Present, Absent, Late, Half-day
LEAVE_STATES = ("Pending", "Approved", "Rejected")
NAMES = {"P": "Present", "A": "Absent", "L": "Late", "H": "Half-day", "V": "Leave"}


# ---------------------------------------------------------------- helpers
def today():
    return date.today().isoformat()


def pdate(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def clean(body, key, limit=60):
    return str(body.get(key, "")).strip()[:limit]


def note(data, msg):
    data["log"].insert(0, {"t": datetime.now().strftime("%d %b, %H:%M"), "msg": msg})
    del data["log"][30:]


def find(data, roll):
    return next((s for s in data["students"] if s["roll"] == roll), None)


def pick_date(body):
    """Chosen date (default today). Future dates are not allowed."""
    s = str(body.get("date") or today())[:10]
    d = pdate(s)
    return s if d and d <= date.today() else None


# ---------------------------------------------------------------- storage
def norm(d):
    """Make any loaded/restored data valid. Old files (v1) upgrade automatically."""
    if not isinstance(d, dict):
        d = {}
    students = []
    for s in d.get("students", []):
        if isinstance(s, dict) and s.get("roll") and s.get("name"):
            students.append({
                "roll": str(s["roll"])[:20], "name": str(s["name"])[:60],
                "cls": str(s.get("cls", ""))[:20], "section": str(s.get("section", ""))[:20],
                "phone": str(s.get("phone", ""))[:20], "email": str(s.get("email", ""))[:60]})
    att = {}
    for day, rec in (d.get("att") or {}).items():
        if pdate(day) and isinstance(rec, dict):
            rec = {str(r): v for r, v in rec.items() if v in STATUS}
            if rec:
                att[day[:10]] = rec
    leaves = []
    for l in d.get("leaves") or []:
        try:
            a, b = pdate(l["start"]), pdate(l["end"])
            if a and b and a <= b and l.get("status") in LEAVE_STATES:
                leaves.append({"id": int(l["id"]), "roll": str(l["roll"]), "start": a.isoformat(),
                               "end": b.isoformat(), "reason": str(l.get("reason", ""))[:120],
                               "status": l["status"]})
        except (KeyError, TypeError, ValueError):
            pass
    st = d.get("settings") if isinstance(d.get("settings"), dict) else {}
    try:
        threshold = min(100, max(1, int(st.get("threshold", 75))))
    except (TypeError, ValueError):
        threshold = 75
    log = [x for x in d.get("log", []) if isinstance(x, dict) and "msg" in x][:30]
    return {"students": students, "att": att, "leaves": leaves, "log": log,
            "settings": {"threshold": threshold, "admin": str(st.get("admin", "Admin"))[:40] or "Admin"}}


def load_data():
    if DATA_FILE.exists():
        try:
            return norm(json.loads(DATA_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            print("Warning: data file unreadable, starting empty.")
    return norm({})


def save_data(data):
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(DATA_FILE)


def statuses(data, roll):
    """{date: status} for one student. Approved leave shows as V unless marked."""
    out = {}
    for l in data["leaves"]:
        if l["roll"] == roll and l["status"] == "Approved":
            d, end = pdate(l["start"]), pdate(l["end"])
            while d <= end and (d - pdate(l["start"])).days < 366:
                out[d.isoformat()] = "V"
                d += timedelta(days=1)
    for day, rec in data["att"].items():
        if roll in rec:
            out[day] = rec[roll]
    return out


def payload(data):
    eff = {s["roll"]: statuses(data, s["roll"]) for s in data["students"]}
    return {**data, "eff": eff, "today": today()}


# ---------------------------------------------------------------- actions
# Each action changes `data` and returns an error message, or None if OK.
def add_student(data, body):
    roll, name, cls = clean(body, "roll", 20), clean(body, "name"), clean(body, "cls", 20)
    email = clean(body, "email")
    if not (roll and name and cls):
        return "Fill in roll number, name and class."
    if any(s["roll"].lower() == roll.lower() for s in data["students"]):
        return "That roll number already exists."
    if email and "@" not in email:
        return "Enter a valid email address or leave it empty."
    data["students"].append({"roll": roll, "name": name, "cls": cls,
                             "section": clean(body, "section", 20),
                             "phone": clean(body, "phone", 20), "email": email})
    note(data, f"{name} was added to class {cls}")


def update_student(data, body):
    s = find(data, clean(body, "roll", 20))
    name, cls, email = clean(body, "name"), clean(body, "cls", 20), clean(body, "email")
    if not s:
        return "Student not found."
    if not (name and cls):
        return "Name and class cannot be empty."
    if email and "@" not in email:
        return "Enter a valid email address or leave it empty."
    s.update(name=name, cls=cls, section=clean(body, "section", 20),
             phone=clean(body, "phone", 20), email=email)
    note(data, f"{name}'s details were updated")


def delete_student(data, body):
    roll = clean(body, "roll", 20)
    s = find(data, roll)
    if not s:
        return "Student not found."
    data["students"].remove(s)
    for d in list(data["att"]):
        data["att"][d].pop(roll, None)
        if not data["att"][d]:
            del data["att"][d]
    data["leaves"] = [l for l in data["leaves"] if l["roll"] != roll]
    note(data, f"{s['name']} was deleted")


def mark(data, body):
    roll, status, day = clean(body, "roll", 20), clean(body, "status", 1), pick_date(body)
    s = find(data, roll)
    if not s:
        return "Student not found."
    if status not in STATUS:
        return "Choose Present, Absent, Late or Half-day."
    if not day:
        return "Choose a valid date that is not in the future."
    data["att"].setdefault(day, {})[roll] = status   # one record per student per date
    note(data, f"{s['name']} marked {NAMES[status]} for {day}")


def mark_all(data, body):
    day = pick_date(body)
    if not day:
        return "Choose a valid date that is not in the future."
    rec = data["att"].setdefault(day, {})
    count = 0
    for roll in body.get("rolls", []):
        if find(data, str(roll)) and roll not in rec:
            rec[roll] = "P"
            count += 1
    if not rec:
        del data["att"][day]
    if count:
        note(data, f"{count} student(s) marked Present for {day}")


def undo(data, body):
    day = pick_date(body)
    if not day:
        return "Choose a valid date."
    rec = data["att"].get(day, {})
    rec.pop(clean(body, "roll", 20), None)
    if not rec:
        data["att"].pop(day, None)


def leave_add(data, body):
    roll, reason = clean(body, "roll", 20), clean(body, "reason", 120)
    a, b = pdate(body.get("start", "")), pdate(body.get("end", ""))
    status = clean(body, "status", 10) or "Pending"
    s = find(data, roll)
    if not s:
        return "Choose a student."
    if not (a and b) or a > b:
        return "Enter a valid date range (start on or before end)."
    if (b - a).days > 365:
        return "A leave can be at most one year long."
    if status not in LEAVE_STATES:
        return "Invalid leave status."
    new_id = max([l["id"] for l in data["leaves"]], default=0) + 1
    data["leaves"].append({"id": new_id, "roll": roll, "start": a.isoformat(),
                           "end": b.isoformat(), "reason": reason, "status": status})
    note(data, f"Leave ({status}) added for {s['name']}")


def leave_set(data, body):
    l = next((x for x in data["leaves"] if x["id"] == body.get("id")), None)
    status = clean(body, "status", 10)
    if not l or status not in LEAVE_STATES:
        return "Leave entry not found."
    l["status"] = status


def leave_del(data, body):
    data["leaves"] = [l for l in data["leaves"] if l["id"] != body.get("id")]


def settings(data, body):
    try:
        t = int(body.get("threshold"))
    except (TypeError, ValueError):
        return "Threshold must be a number from 1 to 100."
    if not 1 <= t <= 100:
        return "Threshold must be a number from 1 to 100."
    data["settings"] = {"threshold": t, "admin": clean(body, "admin", 40) or "Admin"}
    note(data, "Settings saved")


def restore(data, body):
    raw = body.get("data")
    if not isinstance(raw, dict) or not isinstance(raw.get("students"), list) \
            or not isinstance(raw.get("att"), dict):
        return "That file is not a valid ATTENDX backup."
    data.clear()
    data.update(norm(raw))
    note(data, "Data restored from backup")


ACTIONS = {
    "add": add_student, "update": update_student, "delete": delete_student,
    "mark": mark, "mark_all": mark_all, "undo": undo,
    "leave_add": leave_add, "leave_set": leave_set, "leave_del": leave_del,
    "settings": settings, "restore": restore,
}


# ------------------------------------------------------------------- CSV
def tally(data, roll, start="", end=""):
    c = dict.fromkeys("PALHV", 0)
    for day, s in statuses(data, roll).items():
        if (not start or day >= start) and (not end or day <= end):
            c[s] += 1
    n = c["P"] + c["A"] + c["L"] + c["H"]
    c["pct"] = (c["P"] + c["L"] + c["H"] * .5) / n * 100 if n else None
    return c


def safe(x):  # stop spreadsheet formula injection
    x = str(x)
    return "'" + x if x and x[0] in "=+-@" else x


def export_csv(data, q):
    cls, start, end = (q.get(k, [""])[0] for k in ("cls", "from", "to"))
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Roll no", "Name", "Class", "Section", "Present", "Late", "Half-day",
                "Absent", "Leave", "Attendance %"])
    for s in sorted(data["students"], key=lambda s: (s["cls"], s["roll"])):
        if cls and s["cls"] != cls:
            continue
        c = tally(data, s["roll"], start, end)
        w.writerow([safe(s["roll"]), safe(s["name"]), safe(s["cls"]), safe(s["section"]),
                    c["P"], c["L"], c["H"], c["A"], c["V"],
                    "" if c["pct"] is None else f"{c['pct']:.1f}"])
    return out.getvalue().encode("utf-8-sig")  # opens cleanly in Excel


# ----------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    def reply(self, code, payload_, ctype="application/json", extra=None):
        body = payload_ if isinstance(payload_, bytes) else json.dumps(payload_).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            self.reply(200, INDEX_FILE.read_bytes(), "text/html; charset=utf-8")
        elif u.path == "/api/data":
            with LOCK:
                data = load_data()
            self.reply(200, payload(data))
        elif u.path == "/api/export.csv":
            with LOCK:
                data = load_data()
            self.reply(200, export_csv(data, parse_qs(u.query)), "text/csv; charset=utf-8",
                       {"Content-Disposition": f'attachment; filename="attendx_report_{today()}.csv"'})
        elif u.path == "/api/backup":
            with LOCK:
                data = load_data()
            self.reply(200, json.dumps(data, indent=2).encode(), "application/json",
                       {"Content-Disposition": f'attachment; filename="attendx_backup_{today()}.json"'})
        else:
            self.reply(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        action = ACTIONS.get(path.removeprefix("/api/"))
        if not path.startswith("/api/") or action is None:
            return self.reply(404, {"ok": False, "error": "Unknown action"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            return self.reply(400, {"ok": False, "error": "Bad request"})
        with LOCK:
            data = load_data()
            error = action(data, body)
            if error:
                return self.reply(200, {"ok": False, "error": error})
            save_data(data)
        self.reply(200, {"ok": True, "data": payload(data)})

    def log_message(self, *args):  # keep the console quiet
        pass


def main():
    if DATA_FILE.exists():  # safety copy every time the server starts
        shutil.copy2(DATA_FILE, DATA_FILE.with_name("attendx_data.backup.json"))
    host = "0.0.0.0" if "--network" in sys.argv else "127.0.0.1"
    server = ThreadingHTTPServer((host, PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"ATTENDX is running at {url}")
    print("Press Ctrl+C to stop.")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nATTENDX stopped. Data is saved in attendx_data.json.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
