"""
Export flight-price snapshots from Firestore to a flat CSV for analysis in R.

Reads every poll under price_snapshots/{ROUTE}/polls/*, unpacks the
`latest_prices` arrays into one row per price observation, deduplicates on
(route, departure_date, found_at, price, gate), and writes analysis/prices.csv.

Each output row is a single observed fare with everything R needs to test the
time-of-day / day-of-week hypothesis.

Run locally (not in GitHub Actions):
  1. Put your Firebase service account JSON next to this script as
     serviceAccount.json   (it is gitignored - never commit it)
  2. pip install firebase-admin
  3. python export_to_csv.py

Output: analysis/prices.csv
"""

import csv
import os
from datetime import datetime

import firebase_admin
from firebase_admin import credentials, firestore

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_PATH = os.path.join(HERE, "serviceAccount.json")
OUT_PATH = os.path.join(HERE, "prices.csv")


def init_firestore():
    if not os.path.exists(KEY_PATH):
        raise SystemExit(
            f"Service account key not found at {KEY_PATH}.\n"
            "Download it from Firebase console > Project settings > "
            "Service accounts > Generate new private key, and save it here "
            "as serviceAccount.json"
        )
    cred = credentials.Certificate(KEY_PATH)
    firebase_admin.initialize_app(cred)
    return firestore.client()


def export(db):
    rows = []
    seen = set()  # dedupe key across overlapping polls

    snapshots = db.collection("price_snapshots").stream()
    for route_doc in snapshots:
        route_key = route_doc.id  # e.g. "HBA-SYD"
        polls = (db.collection("price_snapshots")
                   .document(route_key)
                   .collection("polls")
                   .stream())

        for poll in polls:
            data = poll.to_dict()
            polled_at = data.get("polled_at")
            currency = data.get("currency", "usd")

            for entry in data.get("latest_prices", []):
                found_at = entry.get("found_at")
                price = entry.get("price")
                gate = entry.get("gate") or ""
                dep = entry.get("departure_date")

                # One physical observation may appear in many polls - dedupe.
                key = (route_key, dep, found_at, price, gate)
                if key in seen:
                    continue
                seen.add(key)

                rows.append({
                    "route": route_key,
                    "origin": data.get("origin"),
                    "destination": data.get("destination"),
                    "price": price,
                    "currency": currency,
                    "departure_date": dep,
                    "found_at": found_at,
                    "gate": gate,
                    "num_changes": entry.get("num_changes"),
                    "distance_km": entry.get("distance_km"),
                    "trip_class": entry.get("trip_class"),
                    "suspect": entry.get("suspect", False),
                    # poll metadata (kept for reference; analyse on found_at)
                    "polled_at": polled_at.isoformat() if polled_at else None,
                })

    # Stable sort: route, then when the fare was observed
    rows.sort(key=lambda r: (r["route"], r["found_at"] or ""))
    return rows


def main():
    db = init_firestore()
    rows = export(db)

    if not rows:
        print("No data found. Has the tracker run and written snapshots yet?")
        return

    fieldnames = [
        "route", "origin", "destination", "price", "currency",
        "departure_date", "found_at", "gate", "num_changes",
        "distance_km", "trip_class", "suspect", "polled_at",
    ]
    with open(OUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    genuine = sum(1 for r in rows if not r["suspect"])
    print(f"Wrote {len(rows)} rows to {OUT_PATH}")
    print(f"  genuine (suspect=False): {genuine}")
    print(f"  suspect (cache-filler):  {len(rows) - genuine}")
    by_route = {}
    for r in rows:
        by_route.setdefault(r["route"], [0, 0])
        by_route[r["route"]][0 if not r["suspect"] else 1] += 1
    for route, (g, s) in sorted(by_route.items()):
        print(f"  {route}: {g} genuine, {s} suspect")


if __name__ == "__main__":
    main()
