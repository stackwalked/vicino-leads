"""Vicino Leads web app: find Milan venues with a weak online presence and track outreach."""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import hmac
import json
import os
import secrets
import threading
import time
import urllib.request
import uuid
from functools import wraps
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, request, send_from_directory, session

import db
import leads as L

HERE = Path(__file__).parent
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
PLACES_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")
MONTHLY_CAP = int(os.environ.get("MONTHLY_REQUEST_CAP", "900"))
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
RECHECK_DAYS = 30
STAGES = {"new", "contacted", "replied", "meeting", "client", "no"}

app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=bool(PUBLIC_URL),
    PERMANENT_SESSION_LIFETIME=dt.timedelta(days=60),
)

_scan_lock = threading.Lock()


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


db.init()
# A scan only runs inside this process, so any scan still marked running
# was cut off by a restart.
db.run("UPDATE scans SET status = 'interrupted', finished_at = %s WHERE status = 'running'", now())


def month_start() -> str:
    t = dt.datetime.now(dt.timezone.utc)
    return t.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def used_this_month() -> int:
    r = db.one("SELECT COALESCE(SUM(requests), 0) AS n FROM scans WHERE started_at >= %s", month_start())
    return int(r["n"])


# --------------------------------------------------------------------------
# Auth


def login_required(f):
    @wraps(f)
    def inner(*a, **kw):
        if not session.get("name"):
            if request.path.startswith("/api/"):
                return jsonify(error="Your session has ended. Log in again."), 401
            return redirect("/login")
        if request.method in ("POST", "PUT") and not request.is_json:
            abort(415)
        return f(*a, **kw)

    return inner


@app.get("/login")
def login_page():
    return send_from_directory(HERE / "static", "login.html")


@app.post("/login")
def login():
    name = (request.form.get("name") or "").strip()[:40]
    pw = request.form.get("password") or ""
    if not APP_PASSWORD:
        return redirect("/login?e=setup")
    if not name or not hmac.compare_digest(pw.encode(), APP_PASSWORD.encode()):
        time.sleep(1)
        return redirect("/login?e=bad")
    session.permanent = True
    session["name"] = name
    return redirect("/")


@app.post("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.get("/healthz")
def health():
    return "ok"


# --------------------------------------------------------------------------
# Pages and data


@app.get("/")
@login_required
def index():
    return send_from_directory(HERE / "static", "index.html")


@app.get("/api/me")
@login_required
def me():
    return jsonify(
        name=session["name"],
        used=used_this_month(),
        cap=MONTHLY_CAP,
        hasKey=bool(PLACES_KEY),
        zones=list(L.ZONES),
        categories=list(L.CATEGORIES),
        cityAreas=len(L.grid(L.MILAN, 6, 6)),
    )


@app.get("/api/leads")
@login_required
def get_leads():
    rows = db.select("SELECT id, data, first_seen FROM leads")
    pipe = {p["id"]: p for p in db.select("SELECT * FROM pipeline")}
    items = [dict(json.loads(r["data"]), firstSeen=r["first_seen"]) for r in rows]
    chains = L.find_chains(items)
    for i in items:
        i.setdefault("web", {"status": L.classify_url(i.get("website")) or "site"})
        i["score"], i["reasons"] = L.score(i, chains)
        i["tier"] = L.tier(i["score"])
        p = pipe.get(i["id"])
        if p:
            i["stage"], i["note"], i["updatedAt"], i["updatedBy"] = p["stage"], p["note"], p["updated_at"], p["updated_by"]
    items.sort(key=lambda i: -i["score"])
    last = db.one("SELECT started_at FROM scans WHERE status IN ('done', 'failed') ORDER BY started_at DESC LIMIT 1")
    return jsonify(leads=items, lastScanAt=last["started_at"] if last else None)


@app.put("/api/pipeline/<lead_id>")
@login_required
def put_pipeline(lead_id):
    body = request.get_json(silent=True) or {}
    cur = db.one("SELECT stage, note FROM pipeline WHERE id = %s", lead_id) or {"stage": "new", "note": ""}
    stage = body.get("stage", cur["stage"])
    note = body.get("note", cur["note"])
    if stage not in STAGES or not isinstance(note, str) or len(note) > 5000:
        return jsonify(error="That status or note isn't valid."), 400
    if not db.one("SELECT id FROM leads WHERE id = %s", lead_id):
        return jsonify(error="That lead no longer exists."), 404
    at = now()
    db.run(
        """INSERT INTO pipeline (id, stage, note, updated_at, updated_by) VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (id) DO UPDATE SET stage = excluded.stage, note = excluded.note,
             updated_at = excluded.updated_at, updated_by = excluded.updated_by""",
        lead_id, stage, note, at, session["name"],
    )
    return jsonify(stage=stage, note=note, updatedAt=at, updatedBy=session["name"])


# --------------------------------------------------------------------------
# Scans


def scan_row(r):
    if not r:
        return None
    return {k: r[k] for k in ("id", "started_at", "finished_at", "started_by", "status", "requests", "found", "added", "progress", "error")} | {
        "params": json.loads(r["params"])
    }


@app.get("/api/scans/latest")
@login_required
def latest_scan():
    return jsonify(scan=scan_row(db.one("SELECT * FROM scans ORDER BY started_at DESC LIMIT 1")), used=used_this_month(), cap=MONTHLY_CAP)


@app.post("/api/scans")
@login_required
def start_scan():
    if not PLACES_KEY:
        return jsonify(error="The Google Places key isn't set up on the server yet."), 400
    body = request.get_json(silent=True) or {}
    zones = body.get("zones") or []
    cats = body.get("categories") or []
    city = zones == "city"
    if not city and (not isinstance(zones, list) or not zones or any(z not in L.ZONES for z in zones)):
        return jsonify(error="Pick at least one neighbourhood."), 400
    if not isinstance(cats, list) or not cats or any(c not in L.CATEGORIES for c in cats):
        return jsonify(error="Pick at least one type of venue."), 400

    left = MONTHLY_CAP - used_this_month()
    areas = 36 if city else len(zones)
    if left < areas * len(cats):
        return jsonify(error=f"This search needs at least {areas * len(cats)} Google requests, and {max(left, 0)} are left this month. Pick fewer neighbourhoods or venue types."), 400

    with _scan_lock:
        if db.one("SELECT id FROM scans WHERE status = 'running'"):
            return jsonify(error="A search is already running. Wait for it to finish."), 409
        sid = uuid.uuid4().hex
        db.run(
            "INSERT INTO scans (id, started_at, started_by, status, params, progress) VALUES (%s, %s, %s, 'running', %s, %s)",
            sid, now(), session["name"], db.dumps({"zones": zones, "categories": cats}), "Starting",
        )
    threading.Thread(target=run_scan, args=(sid, zones, cats, left), daemon=True).start()
    return jsonify(scan=scan_row(db.one("SELECT * FROM scans WHERE id = %s", sid))), 202


def keep_awake(stop: threading.Event):
    """Render's free plan sleeps after 15 idle minutes; ping ourselves while a scan runs."""
    while PUBLIC_URL and not stop.wait(240):
        try:
            urllib.request.urlopen(PUBLIC_URL.rstrip("/") + "/healthz", timeout=20).read()
        except Exception:
            pass


def run_scan(sid, zones, cats, limit):
    stop = threading.Event()
    threading.Thread(target=keep_awake, args=(stop,), daemon=True).start()
    budget, found, error = L.Budget(limit), {}, None

    def progress(text):
        db.run("UPDATE scans SET progress = %s, requests = %s, found = %s WHERE id = %s", text, budget.used, len(found), sid)

    try:
        if zones == "city":
            boxes = [(f"area {i + 1}", b) for i, b in enumerate(L.grid(L.MILAN, 6, 6))]
            depth = 2
        else:
            boxes = [(z, L.zone_box(z)) for z in zones]
            depth = 1
        for i, (label, box) in enumerate(boxes, 1):
            progress(f"Searching {label} ({i} of {len(boxes)})")
            for c in cats:
                L.search_cell(PLACES_KEY, L.CATEGORIES[c], box, budget, 0, depth, found)
            if budget.used >= budget.limit:
                error = "Stopped early: this month's Google request budget ran out."
                break
    except L.PlacesError as e:
        error = str(e)
    except Exception as e:  # keep whatever was found
        error = f"Search failed: {e}"

    try:
        progress("Checking websites")
        photos_reported = any("photos" in p for p in found.values())
        fresh = [l for l in (L.to_lead(p, photos_reported) for p in found.values()) if l]
        existing = {r["id"]: r for r in db.select("SELECT id, data, web_checked FROM leads")}
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=RECHECK_DAYS)).isoformat()
        to_check = []
        for l in fresh:
            old = existing.get(l["id"])
            old_data = json.loads(old["data"]) if old else {}
            if old and old_data.get("website") == l["website"] and (old["web_checked"] or "") >= cutoff and "web" in old_data:
                l["web"] = old_data["web"]
                l["_checked"] = old["web_checked"]
            else:
                to_check.append(l)
        with cf.ThreadPoolExecutor(16) as ex:
            for l, w in zip(to_check, ex.map(lambda l: L.check_site(l["website"]), to_check)):
                l["web"], l["_checked"] = w, now()

        t = now()
        db.many(
            """INSERT INTO leads (id, data, first_seen, last_seen, web_checked) VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET data = excluded.data, last_seen = excluded.last_seen,
                 web_checked = excluded.web_checked""",
            [(l["id"], db.dumps({k: v for k, v in l.items() if k != "_checked"}), t, t, l["_checked"]) for l in fresh],
        )
        added = sum(1 for l in fresh if l["id"] not in existing)
        db.run(
            "UPDATE scans SET status = %s, finished_at = %s, requests = %s, found = %s, added = %s, progress = %s, error = %s WHERE id = %s",
            "failed" if error and not fresh else "done", now(), budget.used, len(fresh), added,
            f"Found {len(fresh)} venues, {added} new", error, sid,
        )
    except Exception as e:
        db.run("UPDATE scans SET status = 'failed', finished_at = %s, requests = %s, error = %s WHERE id = %s",
               now(), budget.used, f"Saving results failed: {e}", sid)
    finally:
        stop.set()


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
