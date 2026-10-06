# Vicino Leads

A small web app that finds Milan restaurants, cafés and bars with a weak online presence, ranks them as leads, and tracks outreach.

- **Find leads**: pick neighbourhoods and venue types. The app searches Google Places, checks each website, and adds the venues to the list.
- **Ranked list**: every venue gets a 0–100 score with the reasons shown. Filter by neighbourhood, venue type, website problem and stage.
- **Pipeline**: New → Contacted → Replied → Meeting → Client / Not interested, with notes. Shared by everyone who logs in, and kept across searches.
- **First message**: ready-to-edit Italian templates for WhatsApp, email, Instagram and walk-ins, built from each venue's gaps.
- **Budget guard**: the server refuses or stops searches before the monthly Google limit (`MONTHLY_REQUEST_CAP`, default 900).

## Deploy (free: Render + Neon)

1. **Database.** Create a free project at [neon.com](https://neon.com) (region: Frankfurt) and copy its connection string (`postgresql://…`).
2. **Google key.** In [Google Cloud Console](https://console.cloud.google.com), create a project, enable billing, enable **Places API (New)**, and create an API key. Restrict the key to Places API (New).
3. **Code on GitHub.** Push this folder to a GitHub repository (private is fine).
4. **Render.** At [render.com](https://render.com), choose **New → Blueprint**, pick the repository, and fill in:
   - `APP_PASSWORD`: the shared password you'll give your friend
   - `GOOGLE_PLACES_API_KEY`: from step 2
   - `DATABASE_URL`: from step 1

   `SECRET_KEY` is generated for you.
5. Open the `https://vicino-leads-….onrender.com` address Render gives you, log in, and click **Find leads**.

The free Render plan sleeps after 15 minutes without visitors, so the first visit after a break takes about a minute to load. A running search keeps the app awake until it finishes.

## Costs

The fields the app asks Google for (website, rating, hours, phone) are billed as Text Search Enterprise: 1,000 free requests a month, then about $35 per 1,000. Each search page is one request (up to 20 venues). One neighbourhood × one venue type uses 1–15 requests depending on how busy the area is. The cap of 900 keeps you inside the free allowance. Raise `MONTHLY_REQUEST_CAP` in Render if you want to pay for more.

## Scoring

| Signal | Points |
|---|---|
| No website / only a social page / only a booking page / site doesn't load | 35 / 30 / 28 / 30 |
| Site without HTTPS, not mobile-friendly, © 3+ years old, no menu | 10, 12, 8, 4 (max 25) |
| Under 3 photos (under 10), no hours, no phone, under 20 reviews (under 50) | 12 (5), 10, 6, 8 (4) (max 30). Photos only count when Google reports them, which Text Search often doesn't. |
| Rated 4.3+ with 30+ reviews / rated 4.0+ / rated under 3.5 | +15 / +8 / −10 |
| Same name and website domain on 3+ venues (a chain) | −40 |

Hot is 50 or more, warm is 30–49, cool is under 30. Scores are recalculated each time the list loads, so changing `leads.py` re-ranks everything without a new search. Websites are re-checked when a venue turns up in a search more than 30 days after its last check.

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
APP_PASSWORD=pw GOOGLE_PLACES_API_KEY=... .venv/bin/python app.py   # http://localhost:5000, SQLite in vicino.db
.venv/bin/python tests/smoke_test.py                                 # end-to-end test with fake Google data
```
