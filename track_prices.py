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
import random
import sys
import time
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

# NOTE ON CURRENCY: the v2 latest-prices endpoint frequently ignores the
# currency parameter and returns USD (confirmed empirically for HBA routes:
# gates like Clickavia/Kiwi/Trip.com quote USD, and the magnitudes match USD
# not AUD). We therefore request USD explicitly so the label matches reality,
# and stamp every record with this value. Convert to AUD at analysis time if
# you want AUD figures. Pattern analysis (hour/day) is unaffected either way,
# since the currency is consistent across all polls.
CURRENCY_REQUESTED = "usd"
CURRENCY = CURRENCY_REQUESTED  # used in the request params
API_BASE = "https://api.travelpayouts.com"

# How many of the cheapest cached entries to keep per poll
MAX_ENTRIES = 30

# Rate-limit handling. Travelpayouts throttles per minute and returns 420/429
# when too many requests arrive too quickly (the shared GitHub Actions runner
# IP can also be throttled). We pause between routes and retry with backoff.
DELAY_BETWEEN_CALLS = 3      # seconds to wait between each API call
MAX_RETRIES = 4             # attempts per call before giving up
RETRY_BACKOFF_BASE = 5      # seconds; grows 5, 10, 20, 40 with jitter
RATE_LIMIT_CODES = {420, 429}


# ---------------------------------------------------------------------------
# Travelpayouts Data API
# ---------------------------------------------------------------------------

def _get_with_retry(url: str, token: str, params: dict) -> dict:
    """GET a Data API endpoint with retry/backoff on rate-limit responses.

    Travelpayouts returns 420/429 when requests arrive too fast. We honour the
    Retry-After header if present, otherwise back off exponentially with jitter.
    Raises on non-rate-limit HTTP errors and after exhausting retries.
    """
    last_exc = None
    for attempt in range(MAX_RETRIES):
        resp = requests.get(url, headers={"X-Access-Token": token},
                            params=params, timeout=60)
        if resp.status_code in RATE_LIMIT_CODES:
            # Prefer server-suggested wait; else exponential backoff + jitter
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                wait = int(retry_after)
            else:
                wait = RETRY_BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 2)
            print(f"    rate-limited ({resp.status_code}); "
                  f"retry {attempt + 1}/{MAX_RETRIES} in {wait:.0f}s", file=sys.stderr)
            last_exc = requests.HTTPError(f"{resp.status_code} rate limited")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("success", False):
            raise RuntimeError(f"API reported failure: {payload}")
        return payload
    # Exhausted retries
    raise last_exc if last_exc else RuntimeError("request failed")


def fetch_latest_prices(token: str, origin: str, destination: str) -> list[dict]:
    """v2 'latest prices' endpoint.

    Returns cached prices observed during the last 48 hours, each with a
    `found_at` timestamp - the key field for repricing analysis.
    """
    payload = _get_with_retry(
        f"{API_BASE}/v2/prices/latest",
        token,
        {
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
    )
    return payload.get("data", [])


def fetch_prices_for_dates(token: str, origin: str, destination: str) -> list[dict]:
    """v3 'prices_for_dates' endpoint - cheapest cached fare per departure
    date. Complements the latest-prices view with a calendar-style series."""
    payload = _get_with_retry(
        f"{API_BASE}/aviasales/v3/prices_for_dates",
        token,
        {
            "origin": origin,
            "destination": destination,
            "currency": CURRENCY,
            "one_way": "true",
            "direct": "false",
            "sorting": "price",
            "limit": MAX_ENTRIES,
        },
    )
    return payload.get("data", [])


def _is_suspect(entry: dict) -> bool:
    """Flag likely cache-filler / calendar-estimate rows rather than genuine
    observed fares. These share a signature: no source agency, zero distance,
    and a round-midnight found_at (a daily bulk cache refresh rather than a
    real user search). Kept in the data but marked so analysis can exclude
    them and work only from genuine observations.
    """
    gate = (entry.get("gate") or "").strip()
    distance = entry.get("distance") or 0
    found_at = entry.get("found_at") or ""
    midnight = found_at.endswith("T00:00:00") or found_at.endswith("00:00:00Z")
    return gate == "" and distance == 0 and midnight


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
            "currency": CURRENCY_REQUESTED,  # what we asked for; see note below
            "departure_date": e.get("depart_date"),
            "found_at": e.get("found_at"),  # when this price was observed
            "trip_class": e.get("trip_class"),
            "gate": e.get("gate"),          # which agency/source showed it
            "num_changes": e.get("number_of_changes"),
            "distance_km": e.get("distance"),
            "actual": e.get("actual"),
            # True = likely cache-filler, not a genuine observed fare.
            # Exclude these when analysing time-of-day patterns.
            "suspect": _is_suspect(e),
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

    # Genuine = observed fares from a real gate, not midnight cache-filler.
    genuine = [e for e in latest if not e.get("suspect")]
    suspect_count = len(latest) - len(genuine)

    # Headline cheapest is taken from GENUINE fares only, so a filler row
    # can't masquerade as the cheapest price. Fall back to all entries only
    # if every row was suspect (keeps the field populated).
    pool = genuine if genuine else latest
    cheapest = min((e["price"] for e in pool if e.get("price")), default=None)

    doc = {
        "origin": route["origin"],
        "destination": route["destination"],
        "currency": CURRENCY_REQUESTED,
        # Actual call time, not the scheduled cron time (Actions can lag)
        "polled_at": polled_at,
        "polled_hour_utc": polled_at.hour,
        "polled_weekday_utc": polled_at.strftime("%A"),
        "cheapest_price": cheapest,          # from genuine fares only
        "latest_prices": latest,             # each entry has found_at + suspect flag
        "prices_by_date": for_dates,
        "latest_count": len(latest),
        "genuine_count": len(genuine),
        "suspect_count": suspect_count,
    }

    (db.collection("price_snapshots")
       .document(route_key)
       .collection("polls")
       .document(doc_id)
       .set(doc))

    return cheapest, len(genuine), suspect_count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    polled_at = datetime.now(timezone.utc)
    token = os.environ["TRAVELPAYOUTS_TOKEN"]
    db = init_firestore()

    errors = []

    for i, route in enumerate(ROUTES):
        label = f"{route['origin']}->{route['destination']}"
        try:
            latest = parse_latest(fetch_latest_prices(token, **route))
            time.sleep(DELAY_BETWEEN_CALLS)  # space the two calls apart
            for_dates = parse_for_dates(fetch_prices_for_dates(token, **route))
            cheapest, genuine_n, suspect_n = write_snapshot(
                db, route, latest, for_dates, polled_at)
            print(f"[OK] {label}: cheapest {cheapest} {CURRENCY_REQUESTED.upper()} "
                  f"({genuine_n} genuine, {suspect_n} suspect, "
                  f"{len(for_dates)} date entries)")
        except Exception as exc:  # log and continue with other routes
            errors.append(f"{label}: {exc}")
            print(f"[ERROR] {label}: {exc}", file=sys.stderr)

        # Pause between routes so we stay well under the per-minute limit
        if i < len(ROUTES) - 1:
            time.sleep(DELAY_BETWEEN_CALLS)

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
