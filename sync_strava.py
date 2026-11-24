import csv
import os
import requests
import time

CLIENT_ID = os.environ.get("STRAVA_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "")
REFRESH_TOKEN = os.environ.get("STRAVA_REFRESH_TOKEN", "")

# Parametri fisiologici di riferimento (aggiornabili qui o via GitHub Secrets/Env)
FTP = float(os.environ.get("ATHLETE_FTP", 280))
FC_MAX = float(os.environ.get("ATHLETE_FCMAX", 204))

# Limite chiamate per singola esecuzione (2 API call per attività: streams + laps)
# Strava consente 100 req / 15 min -> 30 attività = max 60 req (margine sicuro)
MAX_ENRICH_PER_RUN = 30


def get_peak_power(time_arr, watts_arr, window_sec):
    max_avg = 0
    current_sum = 0
    left = 0
    for right in range(len(time_arr)):
        current_sum += watts_arr[right]
        while time_arr[right] - time_arr[left] > window_sec:
            current_sum -= watts_arr[left]
            left += 1
        if time_arr[right] - time_arr[left] >= window_sec * 0.9:
            avg = current_sum / (right - left + 1)
            if avg > max_avg:
                max_avg = avg
    return round(max_avg)


def compute_np_30s(time_arr, watts_arr):
    if not time_arr or not watts_arr or len(watts_arr) < 30:
        return ""
    rolling_4th = []
    window_sum = 0.0
    left = 0
    t0 = time_arr[0]
    for right in range(len(time_arr)):
        window_sum += watts_arr[right]
        while time_arr[right] - time_arr[left] >= 30:
            window_sum -= watts_arr[left]
            left += 1
        count = right - left + 1
        if time_arr[right] - t0 >= 29 and count > 0:
            avg30 = window_sum / count
            rolling_4th.append(avg30 ** 4)
    if not rolling_4th:
        return ""
    return round((sum(rolling_4th) / len(rolling_4th)) ** 0.25, 1)


def calculate_decoupling(time_arr, watts_arr, hr_arr):
    if not time_arr or not watts_arr or not hr_arr or len(time_arr) < 600:
        return "", "", ""

    total_time = time_arr[-1] - time_arr[0]
    mid_time = time_arr[0] + total_time / 2

    w1, h1, c1 = 0, 0, 0
    w2, h2, c2 = 0, 0, 0

    for t, w, h in zip(time_arr, watts_arr, hr_arr):
        if t < mid_time:
            w1 += w
            h1 += h
            c1 += 1
        else:
            w2 += w
            h2 += h
            c2 += 1

    if c1 == 0 or c2 == 0 or h1 == 0 or h2 == 0:
        return "", "", ""

    ef1 = (w1 / c1) / (h1 / c1)
    ef2 = (w2 / c2) / (h2 / c2)
    dec = ((ef1 - ef2) / ef1) * 100 if ef1 > 0 else 0

    return round(ef1, 2), round(ef2, 2), round(dec, 1)


def calculate_clean_decoupling(time_arr, watts_arr, hr_arr):
    """Decoupling escludendo i primi 10' di riscaldamento e anomalie sensore cardio."""
    if not time_arr or not watts_arr or not hr_arr or len(time_arr) < 1200:
        return ""

    t_start = time_arr[0] + 600  # esclude i primi 10 minuti
    valid = [
        (t, w, h)
        for t, w, h in zip(time_arr, watts_arr, hr_arr)
        if t >= t_start and h is not None and h > 40 and not (w > 100 and h < 90)
    ]
    if len(valid) < 600:
        return ""

    mid_t = valid[0][0] + (valid[-1][0] - valid[0][0]) / 2
    p1 = [x for x in valid if x[0] < mid_t]
    p2 = [x for x in valid if x[0] >= mid_t]
    if not p1 or not p2:
        return ""

    avg_h1 = sum(x[2] for x in p1) / len(p1)
    avg_h2 = sum(x[2] for x in p2) / len(p2)
    if avg_h1 <= 0 or avg_h2 <= 0:
        return ""

    ef1 = (sum(x[1] for x in p1) / len(p1)) / avg_h1
    ef2 = (sum(x[1] for x in p2) / len(p2)) / avg_h2
    if ef1 <= 0:
        return ""
    return round(((ef1 - ef2) / ef1) * 100, 1)


def compute_stream_telemetry(time_arr, watts_arr, hr_arr, cad_arr, moving_sec):
    """Calcola TiZ Potenza/FC, cadenza lavoro/facile, coasting, dropout cardio e carico reale."""
    out = {}
    n = len(time_arr)
    if n < 2:
        return out

    # Calcola delta temporale per ogni campione (gestisce pause o smart recording)
    dts = [1]
    for i in range(1, n):
        dt = time_arr[i] - time_arr[i - 1]
        dts.append(dt if 0 < dt <= 5 else 1)

    total_active_sec = sum(dts)

    # 1) Carico reale (NP 30s, IF, VI, TSS) & TiZ Potenza
    if watts_arr and len(watts_arr) == n:
        np_30s = compute_np_30s(time_arr, watts_arr)
        avg_w = sum(w * dt for w, dt in zip(watts_arr, dts)) / total_active_sec if total_active_sec > 0 else 0
        if np_30s != "" and FTP > 0:
            if_val = round(np_30s / FTP, 3)
            vi_val = round(np_30s / avg_w, 2) if avg_w > 0 else ""
            dur_s = moving_sec if moving_sec > 0 else total_active_sec
            tss_val = round((dur_s * (np_30s ** 2)) / ((FTP ** 2) * 3600) * 100, 1)
            out["np_30s_w"] = np_30s
            out["if_val"] = if_val
            out["vi_val"] = vi_val
            out["tss"] = tss_val

        z1 = z2 = z3 = z4 = z5 = z6 = 0
        coasting_s = 0
        p_z1, p_z2, p_z3, p_z4, p_z5 = 0.55 * FTP, 0.75 * FTP, 0.90 * FTP, 1.05 * FTP, 1.20 * FTP
        for w, dt in zip(watts_arr, dts):
            if w == 0:
                coasting_s += dt
            if w < p_z1:
                z1 += dt
            elif w < p_z2:
                z2 += dt
            elif w < p_z3:
                z3 += dt
            elif w < p_z4:
                z4 += dt
            elif w < p_z5:
                z5 += dt
            else:
                z6 += dt

        out["tiz_p_z1_m"] = round(z1 / 60, 1)
        out["tiz_p_z2_m"] = round(z2 / 60, 1)
        out["tiz_p_z3_m"] = round(z3 / 60, 1)
        out["tiz_p_z4_m"] = round(z4 / 60, 1)
        out["tiz_p_z5_m"] = round(z5 / 60, 1)
        out["tiz_p_z6_m"] = round(z6 / 60, 1)
        out["coasting_pct"] = round((coasting_s / total_active_sec) * 100, 1) if total_active_sec > 0 else ""

    # 2) TiZ Frequenza Cardiaca & Dropout Sensore
    if hr_arr and len(hr_arr) == n:
        hr_z4_low = 0.80 * FC_MAX   # ~163 bpm
        hr_85 = 0.85 * FC_MAX       # ~173 bpm
        hr_z5_low = 0.90 * FC_MAX   # ~184 bpm
        s_z4 = s_85 = s_z5 = dropout_s = 0
        for i, (h, dt) in enumerate(zip(hr_arr, dts)):
            w = watts_arr[i] if (watts_arr and len(watts_arr) == n) else 1
            if h is None or h <= 40:
                if w > 0:
                    dropout_s += dt
                continue
            if hr_z4_low <= h < hr_z5_low:
                s_z4 += dt
            if h >= hr_85:
                s_85 += dt
            if h >= hr_z5_low:
                s_z5 += dt

        out["tiz_hr_z4_m"] = round(s_z4 / 60, 1)
        out["tiz_hr_85_m"] = round(s_85 / 60, 1)
        out["tiz_hr_z5_m"] = round(s_z5 / 60, 1)
        out["hr_dropout_sec"] = int(dropout_s)

    # 3) Cadenza nei blocchi di lavoro (>=90% FTP) vs facile (<75% FTP)
    if cad_arr and len(cad_arr) == n and watts_arr and len(watts_arr) == n:
        work_cads = [c for w, c in zip(watts_arr, cad_arr) if w >= 0.90 * FTP and c is not None and c > 0]
        easy_cads = [c for w, c in zip(watts_arr, cad_arr) if 0 < w < 0.75 * FTP and c is not None and c > 0]
        out["cad_work_rpm"] = round(sum(work_cads) / len(work_cads), 1) if work_cads else ""
        out["cad_easy_rpm"] = round(sum(easy_cads) / len(easy_cads), 1) if easy_cads else ""

    return out


def compute_lap_metrics(laps_data, time_arr, hr_arr):
    """Sintetizza i lap di lavoro (>=85% FTP) e il recupero cardiaco HRR 60s."""
    out = {
        "work_laps_n": "",
        "work_laps_avg_w": "",
        "work_laps_fade_pct": "",
        "hrr_60s_avg": "",
    }
    if not laps_data:
        return out

    work_laps = [
        lp for lp in laps_data
        if (lp.get("average_watts") or 0) >= 0.85 * FTP and (lp.get("moving_time") or 0) >= 15
    ]
    if not work_laps:
        return out

    out["work_laps_n"] = len(work_laps)
    avg_w_list = [float(lp["average_watts"]) for lp in work_laps]
    out["work_laps_avg_w"] = round(sum(avg_w_list) / len(avg_w_list), 1)

    if len(avg_w_list) >= 2:
        k = max(1, len(avg_w_list) // 3)
        first_avg = sum(avg_w_list[:k]) / k
        last_avg = sum(avg_w_list[-k:]) / k
        if first_avg > 0:
            # Positivo = negative split (in crescita), Negativo = power fade (calo)
            out["work_laps_fade_pct"] = round(((last_avg - first_avg) / first_avg) * 100, 1)

    # Calcolo HRR 60s sui lap di recupero (>=60s, <65% FTP) subito dopo un lap di lavoro
    if time_arr and hr_arr and len(time_arr) == len(hr_arr):
        hrr_drops = []
        cum_t = 0
        prev_was_work = False
        for lp in laps_data:
            lp_dur = lp.get("elapsed_time") or lp.get("moving_time") or 0
            lp_w = lp.get("average_watts") or 0
            is_work = lp_w >= 0.85 * FTP and lp_dur >= 15
            is_recovery = prev_was_work and lp_dur >= 60 and lp_w < 0.65 * FTP

            if is_recovery:
                # Trova FC a inizio recupero e a +60s
                hr_start = None
                hr_60 = None
                for t, h in zip(time_arr, hr_arr):
                    if h is None or h <= 40:
                        continue
                    if hr_start is None and t >= cum_t:
                        hr_start = h
                    if t >= cum_t + 60:
                        hr_60 = h
                        break
                if hr_start is not None and hr_60 is not None and hr_start >= 130:
                    hrr_drops.append(hr_start - hr_60)

            cum_t += lp_dur
            prev_was_work = is_work

        if hrr_drops:
            out["hrr_60s_avg"] = round(sum(hrr_drops) / len(hrr_drops), 1)

    return out


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

    existing_data = {}
    csv_file = "workouts.csv"

    enriched_cols = [
        "ef_1st_half", "ef_2nd_half", "decoupling_pct",
        "peak_5s_w", "peak_15s_w", "peak_30s_w", "peak_1m_w", "peak_2m_w",
        "peak_3m_w", "peak_5m_w", "peak_10m_w", "peak_15m_w", "peak_20m_w",
        "peak_30m_w", "peak_45m_w", "peak_60m_w",
        # Nuove colonne telemetriche (stile .FIT)
        "np_30s_w", "if_val", "vi_val", "tss",
        "tiz_p_z1_m", "tiz_p_z2_m", "tiz_p_z3_m", "tiz_p_z4_m", "tiz_p_z5_m", "tiz_p_z6_m",
        "tiz_hr_z4_m", "tiz_hr_85_m", "tiz_hr_z5_m",
        "cad_work_rpm", "cad_easy_rpm", "coasting_pct",
        "hr_dropout_sec", "decoupling_clean_pct",
        "work_laps_n", "work_laps_avg_w", "work_laps_fade_pct", "hrr_60s_avg",
    ]

    fieldnames = [
        "id", "data", "ora", "nome", "sport", "indoor",
        "distanza_km", "durata_min", "tempo_totale_min",
        "passo_min_km", "vel_media_kmh", "vel_max_kmh",
        "dislivello_m", "quota_max_m",
        "fc_media", "fc_max",
        "potenza_media_w", "potenza_norm_w", "potenza_max_w", "lavoro_kj",
        "cadenza_media", "temp_media_c", "suffer_score", "pr_count",
    ] + enriched_cols

    if os.path.exists(csv_file):
        with open(csv_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing_data[row["id"]] = row

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
        if page > 5:
            break

    streams_fetched = 0
    RUN_SPORTS = {"Run", "TrailRun", "VirtualRun", "Walk", "Hike"}
    INDOOR_SPORTS = {"VirtualRide", "VirtualRun"}

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
            "indoor": 1 if (a.get("trainer") or sport in INDOOR_SPORTS) else 0,
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
        }
        for k in enriched_cols:
            row[k] = ""

        if a_id in existing_data:
            for k in enriched_cols:
                row[k] = existing_data[a_id].get(k, "")

        needs_streams = (row["peak_5s_w"] == "" or row["decoupling_pct"] == "" or row["tss"] == "")
        has_sensors = a.get("device_watts")

        if needs_streams and has_sensors and streams_fetched < MAX_ENRICH_PER_RUN:
            try:
                s_res = requests.get(
                    f"https://www.strava.com/api/v3/activities/{a_id}/streams"
                    f"?keys=time,watts,heartrate,cadence&key_by_type=true",
                    headers=headers,
                    timeout=15,
                )
                t_arr, w_arr, h_arr, c_arr = [], [], [], []
                if s_res.status_code == 200:
                    s_data = s_res.json()
                    t_arr = s_data.get("time", {}).get("data", [])
                    w_arr = s_data.get("watts", {}).get("data", [])
                    h_arr = s_data.get("heartrate", {}).get("data", [])
                    c_arr = s_data.get("cadence", {}).get("data", [])

                    if t_arr and w_arr:
                        windows = {
                            "peak_5s_w": 5, "peak_15s_w": 15, "peak_30s_w": 30,
                            "peak_1m_w": 60, "peak_2m_w": 120, "peak_3m_w": 180,
                            "peak_5m_w": 300, "peak_10m_w": 600, "peak_15m_w": 900,
                            "peak_20m_w": 1200, "peak_30m_w": 1800, "peak_45m_w": 2700, "peak_60m_w": 3600,
                        }
                        for key, sec in windows.items():
                            if moving_sec >= sec:
                                row[key] = get_peak_power(t_arr, w_arr, sec)

                    if t_arr and w_arr and h_arr:
                        ef1, ef2, dec = calculate_decoupling(t_arr, w_arr, h_arr)
                        row["ef_1st_half"] = ef1
                        row["ef_2nd_half"] = ef2
                        row["decoupling_pct"] = dec
                        row["decoupling_clean_pct"] = calculate_clean_decoupling(t_arr, w_arr, h_arr)

                    if t_arr:
                        tel = compute_stream_telemetry(t_arr, w_arr, h_arr, c_arr, moving_sec)
                        row.update(tel)

                # Chiamata /laps per analisi blocchi di lavoro, fade e HRR 60s
                l_res = requests.get(
                    f"https://www.strava.com/api/v3/activities/{a_id}/laps",
                    headers=headers,
                    timeout=15,
                )
                if l_res.status_code == 200:
                    lap_metrics = compute_lap_metrics(l_res.json(), t_arr, h_arr)
                    row.update(lap_metrics)

                streams_fetched += 1
                time.sleep(0.25)
            except Exception:
                pass

        final_rows.append(row)

    fetched_ids = {r["id"] for r in final_rows}
    for a_id, old_row in existing_data.items():
        if a_id not in fetched_ids:
            for f in fieldnames:
                if f not in old_row:
                    old_row[f] = ""
            final_rows.append(old_row)

    final_rows.sort(key=lambda x: x["data"] + x["ora"], reverse=True)
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(final_rows)


if __name__ == "__main__":
    main()
