"""End-to-end check with fake Google responses: login, search, leads, pipeline, budget.

    .venv/bin/python tests/smoke_test.py
"""

import os
import sys
import tempfile
import time
from pathlib import Path

tmp = tempfile.mkdtemp()
os.environ.update(SQLITE_PATH=f"{tmp}/test.db", APP_PASSWORD="pw", GOOGLE_PLACES_API_KEY="fake", MONTHLY_REQUEST_CAP="40")
# Set TEST_DATABASE_URL to run against Postgres instead of a temporary SQLite file.
os.environ.pop("DATABASE_URL", None)
if os.environ.get("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as A  # noqa: E402
import leads as L  # noqa: E402

calls = []


def fake_search(key, query, box, budget, page_token=None):
    """Two pages per search: 20 + 5 places, a few with websites, one chain."""
    calls.append(query)
    page = 1 if page_token else 0
    n = 5 if page else 20
    lat, lng = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    places = []
    for i in range(n):
        k = f"{query[:4]}-{round(lat, 3)}-{page}-{i}"
        places.append({
            "id": k.replace(".", "_").replace(" ", "_"),
            "displayName": {"text": "Catena Burger" if i == 0 else f"Locale {k}"},
            "formattedAddress": f"Via Test {i}, Milano",
            "location": {"latitude": lat, "longitude": lng},
            "primaryTypeDisplayName": {"text": "Ristorante"},
            "rating": 4.5 if i % 2 else 3.9,
            "userRatingCount": 10 * i,
            "websiteUri": "https://catenaburger.it/" if i == 0 else ["", "https://instagram.com/x", "https://example.it/"][i % 3],
            "photos": [{}] * (i % 11),
            "businessStatus": "CLOSED_PERMANENTLY" if i == 19 else "OPERATIONAL",
        })
    return {"places": places, **({} if page else {"nextPageToken": "p2"})}


L.search = fake_search
L.check_site = lambda url: {"status": L.classify_url(url) or "site", "https": True, "mobile": False, "year": 2019, "menu": True}

c = A.app.test_client()
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or cond
results = []

results.append(ok(c.get("/").status_code == 302, "unauthenticated / redirects to login"))
results.append(ok(c.get("/api/leads").status_code == 401, "unauthenticated API returns 401"))
r = c.post("/login", data={"name": "Marco", "password": "nope"})
results.append(ok("e=bad" in r.headers["Location"], "wrong password rejected"))
c.post("/login", data={"name": "Marco", "password": "pw"})
results.append(ok(c.get("/").status_code == 200, "logged in sees app"))

me = c.get("/api/me").get_json()
results.append(ok(me["name"] == "Marco" and me["cap"] == 40 and me["used"] == 0, "me reports name and budget"))

r = c.post("/api/scans", json={"zones": "city", "categories": ["ristorante", "pizzeria"]})
results.append(ok(r.status_code == 400 and "left this month" in r.get_json()["error"], "city scan over budget is refused"))
r = c.post("/api/scans", json={"zones": ["Bogus"], "categories": ["ristorante"]})
results.append(ok(r.status_code == 400, "unknown zone refused"))
r = c.post("/api/scans", data="x")
results.append(ok(r.status_code == 415, "non-JSON POST refused"))

r = c.post("/api/scans", json={"zones": ["Navigli", "Isola"], "categories": ["ristorante", "pizzeria"]})
results.append(ok(r.status_code == 202, "scan starts"))
for _ in range(100):
    s = c.get("/api/scans/latest").get_json()["scan"]
    if s["status"] != "running":
        break
    time.sleep(0.1)
results.append(ok(s["status"] == "done", f"scan finishes ({s['status']}, {s['progress']}, error={s['error']})"))
results.append(ok(s["requests"] == 8, f"2 zones x 2 categories x 2 pages = 8 requests (got {s['requests']})"))

data = c.get("/api/leads").get_json()
ls = data["leads"]
results.append(ok(len(ls) == s["found"] == s["added"] and len(ls) > 0, f"{len(ls)} leads stored"))
results.append(ok(not any(l["name"] == "Locale" and False for l in ls) and all("score" in l and "reasons" in l for l in ls), "leads carry score and reasons"))
chain = [l for l in ls if l["name"] == "Catena Burger"]
results.append(ok(chain and all(any(r["code"] == "chain" for r in l["reasons"]) for l in chain), "repeated name scored as chain"))
results.append(ok(ls == sorted(ls, key=lambda l: -l["score"]), "leads sorted by score"))
results.append(ok(all(l["zone"] in ("Navigli", "Isola") for l in ls), "zone labels match searched areas"))

lid = ls[0]["id"]
r = c.put(f"/api/pipeline/{lid}", json={"stage": "contacted", "note": "Chiamato, richiamare lunedì"})
results.append(ok(r.status_code == 200 and r.get_json()["updatedBy"] == "Marco", "stage + note saved"))
r = c.put(f"/api/pipeline/{lid}", json={"stage": "bogus"})
results.append(ok(r.status_code == 400, "bad stage refused"))
r = c.put("/api/pipeline/nope", json={"stage": "client"})
results.append(ok(r.status_code == 404, "unknown lead refused"))

# Re-run the same search: leads update, pipeline survives, nothing double-counted.
r = c.post("/api/scans", json={"zones": ["Navigli", "Isola"], "categories": ["ristorante", "pizzeria"]})
for _ in range(100):
    s = c.get("/api/scans/latest").get_json()["scan"]
    if s["status"] != "running":
        break
    time.sleep(0.1)
ls2 = c.get("/api/leads").get_json()["leads"]
again = next(l for l in ls2 if l["id"] == lid)
results.append(ok(len(ls2) == len(ls) and s["added"] == 0, f"rescan adds no duplicates ({len(ls)} -> {len(ls2)}, added={s['added']}, status={s['status']})"))
results.append(ok(again.get("stage") == "contacted" and again.get("note") == "Chiamato, richiamare lunedì", "stage and note survive a rescan"))
used = c.get("/api/me").get_json()["used"]
results.append(ok(used == 16, f"budget counts both scans (used={used})"))

r = c.post("/api/scans", json={"zones": list(L.ZONES)[:10], "categories": ["ristorante", "pizzeria", "bar"]})
results.append(ok(r.status_code == 400, "scan that can't fit the remaining budget is refused"))

generic = [{"name": "Bar Sport", "website": w} for w in ["", "", "https://barsport-navigli.it/"]]
results.append(ok(L.find_chains(generic) == set(), "common name without a shared website isn't a chain"))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
