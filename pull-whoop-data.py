#!/usr/bin/env python3
"""
WHOOP API v2 Data Puller â GitHub Actions version
Pulls recovery, sleep, workout, and strain data.
Appends to whoop-daily-log.json.
Updates GitHub Secrets with refreshed tokens via gh CLI.
"""

import json
import os
import subprocess
import sys
import time
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ââ Configuration from env ââââââââââââââââââââââââââââââââââââââââââââââââââââ

CLIENT_ID     = os.environ["WHOOP_CLIENT_ID"]
CLIENT_SECRET = os.environ["WHOOP_CLIENT_SECRET"]
ACCESS_TOKEN  = os.environ["WHOOP_ACCESS_TOKEN"]
REFRESH_TOKEN = os.environ["WHOOP_REFRESH_TOKEN"]
GH_REPO       = os.environ.get("GITHUB_REPOSITORY", "")

BASE_URL  = "https://api.prod.whoop.com/developer/v2"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
LOG_FILE  = Path("whoop-daily-log.json")

# ââ Token refresh âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def refresh_access_token():
    """Refresh the access token and update GitHub Secrets via gh CLI."""
    global ACCESS_TOKEN, REFRESH_TOKEN
    resp = requests.post(TOKEN_URL, data={
        "grant_type":    "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })
    resp.raise_for_status()
    tokens = resp.json()
    ACCESS_TOKEN  = tokens["access_token"]
    REFRESH_TOKEN = tokens["refresh_token"]

    # Update GitHub Secrets so next run has fresh tokens
    if GH_REPO:
        try:
            subprocess.run(
                ["gh", "secret", "set", "WHOOP_ACCESS_TOKEN", "--body", ACCESS_TOKEN, "--repo", GH_REPO],
                check=True, capture_output=True
            )
            subprocess.run(
                ["gh", "secret", "set", "WHOOP_REFRESH_TOKEN", "--body", REFRESH_TOKEN, "--repo", GH_REPO],
                check=True, capture_output=True
            )
            print("[OK] GitHub Secrets updated with new tokens")
        except Exception as e:
            print(f"[WARN] Failed to update GitHub Secrets: {e}")

    return ACCESS_TOKEN

# ââ WHOOP API calls âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def whoop_get(endpoint, params=None):
    url = f"{BASE_URL}/{endpoint}"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    resp = requests.get(url, headers=headers, params=params or {})
    if resp.status_code == 401:
        raise PermissionError("401 Unauthorized")
    resp.raise_for_status()
    return resp.json()

def iso_window():
    now   = datetime.now(timezone.utc)
    start = now - timedelta(hours=36)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%dT%H:%M:%SZ")

def safe(fn, label):
    try:
        return fn()
    except Exception as e:
        print(f"[ERROR] {label}: {e}")
        return None

# ââ Data extraction âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def extract_recovery():
    start, end = iso_window()
    data = whoop_get("recovery", {"start": start, "end": end, "limit": 5})
    records = data.get("records", [])
    if not records:
        return None
    score = records[0].get("score") or {}
    return {
        "score":      score.get("recovery_score"),
        "hrv_ms":     round(score.get("hrv_rmssd_milli", 0), 1),
        "resting_hr": score.get("resting_heart_rate"),
        "spo2":       round(score.get("spo2_percentage", 0), 1),
    }

def extract_sleep():
    start, end = iso_window()
    data = whoop_get("activity/sleep", {"start": start, "end": end, "limit": 5})
    records = data.get("records", [])
    if not records:
        return None
    score = records[0].get("score") or {}
    stages = score.get("stage_summary") or {}
    ms_to_min = lambda k: round((stages.get(k) or 0) / 60000, 1)
    total_ms = sum(stages.get(k) or 0 for k in [
        "total_slow_wave_sleep_time_milli",
        "total_rem_sleep_time_milli",
        "total_light_sleep_time_milli",
        "total_awake_time_milli",
    ])
    return {
        "total_hours":      round(total_ms / 3600000, 2),
        "efficiency":       round(score.get("sleep_efficiency_percentage", 0) / 100, 3),
        "deep_min":         ms_to_min("total_slow_wave_sleep_time_milli"),
        "rem_min":          ms_to_min("total_rem_sleep_time_milli"),
        "light_min":        ms_to_min("total_light_sleep_time_milli"),
        "awake_min":        ms_to_min("total_awake_time_milli"),
        "respiratory_rate": round(score.get("respiratory_rate", 0), 1),
    }

def extract_workouts():
    start, end = iso_window()
    data = whoop_get("activity/workout", {"start": start, "end": end, "limit": 10})
    records = data.get("records", [])
    workouts = []
    for w in records:
        score = w.get("score") or {}
        zones = score.get("zone_duration") or {}
        ms_to_min = lambda k: round((zones.get(k) or 0) / 60000, 1)

        duration = 0
        try:
            if w.get("start") and w.get("end"):
                s = datetime.fromisoformat(w["start"].replace("Z", "+00:00"))
                e = datetime.fromisoformat(w["end"].replace("Z", "+00:00"))
                duration = round((e - s).total_seconds() / 60, 1)
        except:
            pass

        workouts.append({
            "activity":      w.get("sport_id"),
            "calories_kcal": round(score.get("kilojoule", 0) / 4.184, 1),
            "strain":        round(score.get("strain", 0), 2),
            "duration_min":  duration,
            "hr_zones": {
                "zone1_min": ms_to_min("zone_one_milli"),
                "zone2_min": ms_to_min("zone_two_milli"),
                "zone3_min": ms_to_min("zone_three_milli"),
                "zone4_min": ms_to_min("zone_four_milli"),
                "zone5_min": ms_to_min("zone_five_milli"),
            },
        })
    return workouts

def extract_strain():
    start, end = iso_window()
    data = whoop_get("cycle", {"start": start, "end": end, "limit": 5})
    records = data.get("records", [])
    if not records:
        return None
    score = records[0].get("score") or {}
    return {
        "daily_strain":        round(score.get("strain", 0), 2),
        "avg_hr":              round(score.get("average_heart_rate", 0)),
        "total_calories_kcal": round(score.get("kilojoule", 0) / 4.184, 1),
    }

# ââ Main ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def main():
    global ACCESS_TOKEN

    # Always refresh token to keep it alive
    print("Refreshing WHOOP token...")
    try:
        ACCESS_TOKEN = refresh_access_token()
    except Exception as e:
        print(f"[FATAL] Token refresh failed: {e}")
        sys.exit(1)

    print("Pulling WHOOP data...")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    recovery = safe(extract_recovery, "recovery")
    sleep    = safe(extract_sleep, "sleep")
    workouts = safe(extract_workouts, "workouts")
    strain   = safe(extract_strain, "strain")

    entry = {
        "date":      today,
        "pulled_at": datetime.now(timezone.utc).isoformat(),
        "recovery":  recovery,
        "sleep":     sleep,
        "workouts":  workouts or [],
        "strain":    strain,
    }

    # Load existing log
    if LOG_FILE.exists():
        with open(LOG_FILE) as f:
            log = json.load(f)
    else:
        log = []

    # Replace today's entry if exists, otherwise append
    log = [e for e in log if e.get("date") != today]
    log.append(entry)
    log.sort(key=lambda e: e.get("date", ""))

    with open(LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)

    print(f"\n[OK] Data saved for {today}")
    if recovery:
        print(f"  Recovery: {recovery['score']}% | HRV: {recovery['hrv_ms']}ms | RHR: {recovery['resting_hr']}")
    if sleep:
        print(f"  Sleep: {sleep['total_hours']}h | Efficiency: {sleep['efficiency']*100:.1f}%")
    if strain:
        print(f"  Strain: {strain['daily_strain']} | Calories: {strain['total_calories_kcal']}")

if __name__ == "__main__":
    main()
