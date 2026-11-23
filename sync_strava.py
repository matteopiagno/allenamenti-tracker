import csv
import os
import requests
import time

CLIENT_ID = os.environ.get("STRAVA_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "")
REFRESH_TOKEN = os.environ.get("STRAVA_REFRESH_TOKEN", "")

def get_peak_power(time_arr, watts_arr, window_sec=1200):
    max_avg = 0
    current_sum = 0
    left = 0
    for right in range(len(time_arr)):
        current_sum += watts_arr[right]
        while time_arr[right] - time_arr[left] > window_sec:
            current_sum -= watts_arr[left]
            left += 1
        # allow small tolerance
        if time_arr[right] - time_arr[left] >= window_sec - 5:
            avg = current_sum / (right - left + 1)
            if avg > max_avg:
                max_avg = avg
    return round(max_avg)

def calculate_decoupling(time_arr, watts_arr, hr_arr):
    if not time_arr or not watts_arr or not hr_arr or len(time_arr) < 600:
        return "", "", ""
    
    total_time = time_arr[-1] - time_arr[0]
    mid_time = time_arr[0] + total_time / 2
    
    w1, h1, c1 = 0, 0, 0
    w2, h2, c2 = 0, 0, 0
    
    for t, w, h in zip(time_arr, watts_arr, hr_arr):
        if t < mid_time:
            w1+=w; h1+=h; c1+=1
        else:
            w2+=w; h2+=h; c2+=1
            
    if c1 == 0 or c2 == 0 or h1 == 0 or h2 == 0:
        return "", "", ""
        
    ef1 = (w1/c1)/(h1/c1)
    ef2 = (w2/c2)/(h2/c2)
    dec = ((ef1 - ef2) / ef1) * 100 if ef1 > 0 else 0
    
    return round(ef1, 2), round(ef2, 2), round(dec, 1)

def main():
    if not CLIENT_ID:
        return
        
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
    headers = {"Authorization": f"Bearer {access_token}"}

    # Leggi il CSV esistente per capire quali stream abbiamo già scaricato
    existing_data = {}
    csv_file = "workouts.csv"
    
    fieldnames = [
        "id", "data", "ora", "nome", "sport", "indoor",
        "distanza_km", "durata_min", "tempo_totale_min",
        "passo_min_km", "vel_media_kmh", "vel_max_kmh",
        "dislivello_m", "quota_max_m",
        "fc_media", "fc_max",
        "potenza_media_w", "potenza_norm_w", "potenza_max_w", "lavoro_kj",
        "cadenza_media", "temp_media_c", "suffer_score", "pr_count",
        # Nuove metriche avanzate:
        "peak_20m_w", "ef_1st_half", "ef_2nd_half", "decoupling_pct"
    ]

    if os.path.exists(csv_file):
        with open(csv_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing_data[row["id"]] = row

    # Scarica lista attività
    activities = []
    page = 1
    while True:
        res = requests.get(
            "https://www.strava.com/api/v3/athlete/activities",
            headers=headers,
            params={"per_page": 100, "page": page},
            timeout=30,
        )
        res.raise_for_status()
        batch = res.json()
        if not batch:
            break
        activities.extend(batch)
        page += 1

    # Estrai Streams per le nuove attività con potenza e cardio (Max 40 per non superare il rate limit)
    streams_fetched = 0
    RUN_SPORTS = {"Run", "TrailRun", "VirtualRun", "Walk", "Hike"}
    
    final_rows = []
    
    for a in activities:
        a_id = str(a.get("id"))
        sport = a.get("sport_type") or a.get("type", "")
        
        dist_km = round((a.get("distance", 0) or 0) / 1000, 2)
        moving_sec = a.get("moving_time", 0) or 0
        dur_min = round(moving_sec / 60, 1)
        tot_min = round((a.get("elapsed_time", 0) or 0) / 60, 1)
        pace = round((moving_sec / 60) / dist_km, 2) if dist_km > 0 else ""
        avg_speed_kmh = round((a.get("average_speed", 0) or 0) * 3.6, 1)
        
        raw_cadence = a.get("average_cadence")
        if raw_cadence is not None:
            cadence = round(raw_cadence * 2, 0) if sport in RUN_SPORTS else round(raw_cadence, 0)
        else:
            cadence = ""
            
        start_local = a.get("start_date_local", "")
        
        row = {
            "id": a_id,
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
            "vel_max_kmh": round((a.get("max_speed", 0) or 0) * 3.6, 1) if (a.get("max_speed", 0) or 0) > 0 else "",
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
            "peak_20m_w": "",
            "ef_1st_half": "",
            "ef_2nd_half": "",
            "decoupling_pct": ""
        }
        
        if a_id in existing_data:
            row["peak_20m_w"] = existing_data[a_id].get("peak_20m_w", "")
            row["ef_1st_half"] = existing_data[a_id].get("ef_1st_half", "")
            row["ef_2nd_half"] = existing_data[a_id].get("ef_2nd_half", "")
            row["decoupling_pct"] = existing_data[a_id].get("decoupling_pct", "")
            
        # Se mancano i dati avanzati, ci sono sensori di watt e cardio, e l'allenamento dura >= 20 min
        needs_streams = (row["peak_20m_w"] == "" or row["decoupling_pct"] == "")
        has_sensors = a.get("device_watts") and a.get("has_heartrate")
        is_long_enough = moving_sec >= 1200
        
        if needs_streams and has_sensors and is_long_enough and streams_fetched < 40:
            try:
                s_res = requests.get(
                    f"https://www.strava.com/api/v3/activities/{a_id}/streams?keys=time,watts,heartrate&key_by_type=true",
                    headers=headers,
                    timeout=10
                )
                if s_res.status_code == 200:
                    s_data = s_res.json()
                    t_arr = s_data.get("time", {}).get("data", [])
                    w_arr = s_data.get("watts", {}).get("data", [])
                    h_arr = s_data.get("heartrate", {}).get("data", [])
                    
                    if t_arr and w_arr:
                        row["peak_20m_w"] = get_peak_power(t_arr, w_arr, 1200)
                    if t_arr and w_arr and h_arr:
                        ef1, ef2, dec = calculate_decoupling(t_arr, w_arr, h_arr)
                        row["ef_1st_half"] = ef1
                        row["ef_2nd_half"] = ef2
                        row["decoupling_pct"] = dec
                        
                streams_fetched += 1
                time.sleep(0.2)
            except Exception as e:
                pass

        final_rows.append(row)

    fetched_ids = {r["id"] for r in final_rows}
    for a_id, old_row in existing_data.items():
        if a_id not in fetched_ids:
            for f in fieldnames:
                if f not in old_row: old_row[f] = ""
            final_rows.append(old_row)

    final_rows.sort(key=lambda x: x["data"] + x["ora"], reverse=True)
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(final_rows)

if __name__ == "__main__":
    main()
