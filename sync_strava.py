import csv
import os
import requests

CLIENT_ID = os.environ["STRAVA_CLIENT_ID"]
CLIENT_SECRET = os.environ["STRAVA_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["STRAVA_REFRESH_TOKEN"]

# 1. Rinnova il token di accesso
auth_res = requests.post(
    "https://www.strava.com/oauth/token",
    data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "grant_type": "refresh_token",
    },
    timeout=30,
)
auth_res.raise_for_status()
access_token = auth_res.json()["access_token"]

# 2. Scarica tutto lo storico + nuovi allenamenti (200 per pagina)
headers = {"Authorization": f"Bearer {access_token}"}
activities = []
page = 1

while True:
    res = requests.get(
        "https://www.strava.com/api/v3/athlete/activities",
        headers=headers,
        params={"per_page": 200, "page": page},
        timeout=30,
    )
    res.raise_for_status()
    batch = res.json()
    if not batch:
        break
    activities.extend(batch)
    page += 1

# 3. Salva i dati puliti in workouts.csv
fieldnames = [
    "id", "data", "nome", "sport", "distanza_km",
    "durata_min", "passo_min_km", "dislivello_m", "fc_media", "fc_max"
]

with open("workouts.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    for a in activities:
        dist_km = round(a.get("distance", 0) / 1000, 2)
        moving_sec = a.get("moving_time", 0)
        dur_min = round(moving_sec / 60, 1)
        pace = round((moving_sec / 60) / dist_km, 2) if dist_km > 0 else ""

        writer.writerow({
            "id": a.get("id"),
            "data": a.get("start_date_local", "")[:10],
            "nome": a.get("name", ""),
            "sport": a.get("sport_type") or a.get("type", ""),
            "distanza_km": dist_km,
            "durata_min": dur_min,
            "passo_min_km": pace,
            "dislivello_m": round(a.get("total_elevation_gain", 0), 1),
            "fc_media": a.get("average_heartrate", ""),
            "fc_max": a.get("max_heartrate", ""),
        })
