"""
Flight Price Tracker (Travelpayouts Data API - free tier)
Polls cached Aviasales price data for HBA-SYD and HBA-OOL and logs
snapshots to Firestore for time-of-day / day-of-week analysis.

IMPORTANT: This API returns CACHED prices from real user searches on
Aviasales, not live airline quotes. Every record carries a `found_at`
timestamp (when the price was actually observed). Analysis must be done
on `found_at`, not on poll time - see README.

Designed to run hourly via GitHub Actions.

Required environment variables (set as GitHub Actions secrets):
  TRAVELPAYOUTS_TOKEN       (API token from the Travelpayouts dashboard)
  FIREBASE_SERVICE_ACCOUNT  (full JSON of the Firebase service account key)
"""

import json
import os
import sys
from datetime import datetime, timezone

import requests
import firebase_admin
from firebase_admin import credentials, firestore

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROUTES = [
    {"origin": "HBA", "destination": "SYD"},
    {"origin": "HBA", "destination": "OOL"},
    {"origin": "HBA", "destination": "MEL"},
]

CURRENCY = "aud"
API_BASE = "https://api.travelpayouts.com"

# How many of the cheapest cached entries to keep per poll
MAX_ENTRIES = 30


# ---------------------------------------------------------------------------
# Travelpayouts Data API
# ---------------------------------------------------------------------------

def fetch_latest_prices(token: str, origin: str, destination: str) -> list[dict]:
    """v2 'latest prices' endpoint.

    Returns cached prices observed during the last 48 hours, each with a
    `found_at` timestamp - the key field for repricing analysis.
    """
    resp = requests.get(
        f"{API_BASE}/v2/prices/latest",
        headers={"X-Access-Token": token},
        params={
            "origin": origin,
            "destination": destination,
            "currency": CURRENCY,
            "period_type": "year",   # any departure date in the coming year
            "one_way": "true",
            "page": 1,
            "limit": MAX_ENTRIES,
            "sorting": "price",
            "trip_class": 0,         # 0 = economy
        },
        timeout=60,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success", False):
        raise RuntimeError(f"API reported failure: {payload}")
    return payload.get("data", [])


def fetch_prices_for_dates(token: str, origin: str, destination: str) -> list[dict]:
    """v3 'prices_for_dates' endpoint - cheapest cached fare per departure
    date. Complements the latest-prices view with a calendar-style series."""
    resp = requests.get(
        f"{API_BASE}/aviasales/v3/prices_for_dates",
        headers={"X-Access-Token": token},
        params={
            "origin": origin,
            "destination": destination,
            "currency": CURRENCY,
            "one_way": "true",
            "direct": "false",
            "sorting": "price",
            "limit": MAX_ENTRIES,
        },
        timeout=60,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success", False):
        raise RuntimeError(f"API reported failure: {payload}")
    return payload.get("data", [])


def parse_latest(entries) -> list[dict]:
    """Normalise v2 latest-prices records.

    v2 returns `data` as a flat list. Guard against an unexpected dict shape
    (older/alternate responses sometimes nest by destination) so the run
    fails loudly rather than silently.
    """
    if isinstance(entries, dict):
        # Flatten one level of nesting if the API returns a keyed structure
        flattened = []
        for v in entries.values():
            if isinstance(v, dict):
                flattened.extend(v.values())
            else:
                flattened.append(v)
        entries = flattened
    if not isinstance(entries, list):
        return []

    parsed = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        parsed.append({
            "price": e.get("value"),
            "departure_date": e.get("depart_date"),
            "found_at": e.get("found_at"),  # when this price was observed
            "trip_class": e.get("trip_class"),
            "gate": e.get("gate"),          # which agency/source showed it
            "num_changes": e.get("number_of_changes"),
            "distance_km": e.get("distance"),
            "actual": e.get("actual"),
        })
    return parsed


def parse_for_dates(entries: list[dict]) -> list[dict]:
    """Normalise v3 prices_for_dates records."""
    parsed = []
    for e in entries:
        parsed.append({
            "price": e.get("price"),
            "departure_date": e.get("departure_at"),
            "airline": e.get("airline"),
            "flight_number": e.get("flight_number"),
            "transfers": e.get("transfers"),
        })
    return parsed


# ---------------------------------------------------------------------------
# Firestore
# ---------------------------------------------------------------------------

def init_firestore():
    sa_json = json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"])
    cred = credentials.Certificate(sa_json)
    firebase_admin.initialize_app(cred)
    return firestore.client()


def write_snapshot(db, route: dict, latest: list[dict], for_dates: list[dict],
                   polled_at: datetime):
    """One document per poll per route.

    Collection layout:
      price_snapshots/{ORIGIN-DEST}/polls/{ISO_TIMESTAMP}
    """
    route_key = f"{route['origin']}-{route['destination']}"
    doc_id = polled_at.strftime("%Y-%m-%dT%H-%M-%SZ")

    cheapest = min((e["price"] for e in latest if e.get("price")), default=None)

    doc = {
        "origin": route["origin"],
        "destination": route["destination"],
        # Actual call time, not the scheduled cron time (Actions can lag)
        "polled_at": polled_at,
        "polled_hour_utc": polled_at.hour,
        "polled_weekday_utc": polled_at.strftime("%A"),
        "cheapest_price": cheapest,
        "latest_prices": latest,       # each entry has its own found_at
        "prices_by_date": for_dates,
        "latest_count": len(latest),
    }

    (db.collection("price_snapshots")
       .document(route_key)
       .collection("polls")
       .document(doc_id)
       .set(doc))

    return cheapest


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    polled_at = datetime.now(timezone.utc)
    token = os.environ["TRAVELPAYOUTS_TOKEN"]
    db = init_firestore()

    errors = []

    for route in ROUTES:
        label = f"{route['origin']}->{route['destination']}"
        try:
            latest = parse_latest(fetch_latest_prices(token, **route))
            for_dates = parse_for_dates(fetch_prices_for_dates(token, **route))
            cheapest = write_snapshot(db, route, latest, for_dates, polled_at)
            print(f"[OK] {label}: cheapest {cheapest} {CURRENCY.upper()} "
                  f"({len(latest)} latest entries, {len(for_dates)} date entries)")
        except Exception as exc:  # log and continue with other routes
            errors.append(f"{label}: {exc}")
            print(f"[ERROR] {label}: {exc}", file=sys.stderr)

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
