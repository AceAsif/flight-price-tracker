"""
ONE-OFF cleanup: delete the retired HBA-OOL route from Firestore.

We swapped OOL (Gold Coast) out of the tracker in favour of BNE (Brisbane)
because OOL had almost no cached data. This removes the leftover OOL documents
so the database only holds routes we still track.

This is a manual, run-once script - it is NOT part of the hourly job and will
never run automatically. It asks for typed confirmation before deleting.

Usage (from the analysis folder, with serviceAccount.json present):
  python delete_ool.py
"""

import os

import firebase_admin
from firebase_admin import credentials, firestore

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_PATH = os.path.join(HERE, "serviceAccount.json")

ROUTE_TO_DELETE = "HBA-OOL"   # explicit: only ever this route


def init_firestore():
    if not os.path.exists(KEY_PATH):
        raise SystemExit(f"Service account key not found at {KEY_PATH}")
    cred = credentials.Certificate(KEY_PATH)
    firebase_admin.initialize_app(cred)
    return firestore.client()


def main():
    db = init_firestore()
    route_ref = db.collection("price_snapshots").document(ROUTE_TO_DELETE)
    polls_ref = route_ref.collection("polls")

    polls = list(polls_ref.list_documents())
    print(f"About to delete route '{ROUTE_TO_DELETE}' and its "
          f"{len(polls)} poll document(s).")
    if not polls:
        print("(No poll documents found - the route may already be clean.)")

    confirm = input(f"Type '{ROUTE_TO_DELETE}' to confirm deletion: ").strip()
    if confirm != ROUTE_TO_DELETE:
        print("Confirmation did not match. Nothing deleted.")
        return

    # Delete all poll documents first (subcollections aren't auto-removed)
    deleted = 0
    for poll in polls:
        poll.delete()
        deleted += 1
        if deleted % 20 == 0:
            print(f"  deleted {deleted}/{len(polls)} polls...")

    # Then delete the (phantom) parent route document
    route_ref.delete()

    print(f"Done. Deleted {deleted} poll document(s) and the "
          f"'{ROUTE_TO_DELETE}' route document.")
    print("Your remaining routes (HBA-SYD, HBA-MEL, HBA-BNE) are untouched.")


if __name__ == "__main__":
    main()
