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

# 3. Salva tutti i dati granulari in workouts.csv
fieldnames = [
    "id", "data", "ora", "nome", "sport", "indoor",
    "distanza_km", "durata_min", "tempo_totale_min",
    "passo_min_km", "vel_media_kmh", "vel_max_kmh",
    "dislivello_m", "quota_max_m",
    "fc_media", "fc_max",
    "potenza_media_w", "potenza_norm_w", "potenza_max_w", "lavoro_kj",
    "cadenza_media", "temp_media_c", "suffer_score", "pr_count"
]

RUN_SPORTS = {"Run", "TrailRun", "VirtualRun", "Walk", "Hike"}

with open("workouts.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    for a in activities:
        sport = a.get("sport_type") or a.get("type", "")
        dist_m = a.get("distance", 0) or 0
        dist_km = round(dist_m / 1000, 2)
        moving_sec = a.get("moving_time", 0) or 0
        elapsed_sec = a.get("elapsed_time", 0) or 0
        dur_min = round(moving_sec / 60, 1)
        tot_min = round(elapsed_sec / 60, 1)

        pace = round((moving_sec / 60) / dist_km, 2) if dist_km > 0 else ""
        avg_speed_kmh = round((a.get("average_speed", 0) or 0) * 3.6, 1)
        max_speed_kmh = round((a.get("max_speed", 0) or 0) * 3.6, 1)

        # Strava riporta la cadenza corsa su singolo piede: moltiplichiamo x2 per avere spm reali
        raw_cadence = a.get("average_cadence")
        if raw_cadence is not None:
            cadence = round(raw_cadence * 2, 0) if sport in RUN_SPORTS else round(raw_cadence, 0)
        else:
            cadence = ""

        start_local = a.get("start_date_local", "")

        writer.writerow({
            "id": a.get("id"),
            "data": start_local[:10],
            "ora": start_local[11:16] if len(start_local) >= 16 else "",
            "nome": a.get("name", ""),
            "sport": sport,
            "indoor": 1 if a.get("trainer") else 0,
            "distanza_km": dist_km,
            "durata_min": dur_min,
            "tempo_totale_min": tot_min,
            "passo_min_km": pace,
            "vel_media_kmh": avg_speed_kmh if avg_speed_kmh > 0 else "",
            "vel_max_kmh": max_speed_kmh if max_speed_kmh > 0 else "",
            "dislivello_m": round(a.get("total_elevation_gain", 0) or 0, 1),
            "quota_max_m": round(a.get("elev_high", 0), 0) if a.get("elev_high") is not None else "",
            "fc_media": round(a["average_heartrate"], 0) if a.get("average_heartrate") else "",
            "fc_max": round(a["max_heartrate"], 0) if a.get("max_heartrate") else "",
            "potenza_media_w": round(a["average_watts"], 0) if a.get("average_watts") else "",
            "potenza_norm_w": round(a["weighted_average_watts"], 0) if a.get("weighted_average_watts") else "",
            "potenza_max_w": round(a["max_watts"], 0) if a.get("max_watts") else "",
            "lavoro_kj": round(a["kilojoules"], 0) if a.get("kilojoules") else "",
            "cadenza_media": int(cadence) if cadence != "" else "",
            "temp_media_c": a.get("average_temp") if a.get("average_temp") is not None else "",
            "suffer_score": a.get("suffer_score") if a.get("suffer_score") is not None else "",
            "pr_count": a.get("pr_count", 0) or 0,
        })
