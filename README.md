# Flight Price Tracker (Free — Travelpayouts Data API)

Hourly tracking of cached flight prices for HBA→SYD (competitive route) and HBA→OOL (single-carrier route), to look for day-of-week and time-of-day price patterns.

## Important: what this data is (and is not)

The Travelpayouts Data API returns **cached prices from real user searches on Aviasales**, not live airline quotes. Every record in the `latest_prices` list has a `found_at` timestamp — the moment that price was actually observed by a real search.

Consequences for the experiment:

- **Analyse on `found_at`, not poll time.** The hourly poll just harvests whatever new cache entries appeared; `found_at` tells you when a price actually existed.
- **Day-of-week and trend analysis: solid.** Enough data accumulates over weeks to compare weekdays and track volatility.
- **Hour-of-day analysis: indicative only.** On thin routes (HBA→OOL especially) there may be hours with no searches, so overnight windows can be blind. Treat hourly conclusions as suggestive, not definitive.
- Deduplicate: the same `found_at` entry will appear in multiple consecutive polls (the endpoint returns ~48h of observations). Dedupe on `(departure_date, found_at, price)` at analysis time.

## How it works

- A GitHub Actions cron job runs `track_prices.py` every hour
- For each route it calls two endpoints:
  - `v1/prices/latest` — recent price observations with `found_at` (the analytical core)
  - `v3/prices_for_dates` — cheapest cached fare per departure date, with airline and flight number (calendar view)
- Snapshots are written to Firestore

## Setup

### 1. Travelpayouts token

1. Register free at https://www.travelpayouts.com (choose the affiliate signup — no website traffic is required to use the Data API)
2. In the dashboard, find your **API token** (Profile / API access section)

### 2. Firebase service account

1. Firebase console → your project → Project settings → Service accounts → Generate new private key
2. Keep the downloaded JSON

### 3. GitHub repository secrets

Settings → Secrets and variables → Actions → New repository secret

| Secret name | Value |
|---|---|
| `TRAVELPAYOUTS_TOKEN` | Your Travelpayouts API token |
| `FIREBASE_SERVICE_ACCOUNT` | Entire contents of the service account JSON |

### 4. Test it

Actions tab → Track Flight Prices → Run workflow. Check the logs for `[OK]` lines and verify documents in Firestore under `price_snapshots`.

## Firestore layout

```
price_snapshots/
  HBA-SYD/
    polls/
      2026-07-11T03-00-12Z/
        cheapest_price: 189.0
        polled_at, polled_hour_utc, polled_weekday_utc
        latest_prices: [ {price, departure_date, found_at, gate, num_changes, ...} ]
        prices_by_date: [ {price, departure_date, airline, flight_number, transfers} ]
```

## Analysis notes

- **Timezone**: `found_at` timestamps are in the API's timezone (treat as UTC unless docs state otherwise, then convert). Hobart is UTC+10 (AEST) / UTC+11 (AEDT) — convert before testing the "Tuesday 3am" hypothesis
- Build the analysis dataset from deduplicated `latest_prices` entries across all polls, keyed on `found_at`
- Group by local hour-of-day and day-of-week of `found_at`; compare medians. Also check *observation counts* per hour — a cheap price at 3am means little if there were only two observations at 3am all month
- The `gate` field shows which agency displayed the price — useful for spotting outliers from a single discount agency rather than genuine airline repricing
- Compare routes: HBA-SYD (Qantas/Jetstar/Virgin) vs HBA-OOL (Jetstar direct). Jetstar sale launches are the closest real-world event to the "random dip" folklore
- Run for at least 4–6 weeks; the cached nature of the data means patience matters even more here

## Extending

- Add a price-drop alert (email or Telegram) when `cheapest_price` falls below a threshold
- Build a React dashboard reading from Firestore to chart the series
- If the cached data proves too sparse for the hour-of-day question, the upgrade path is a paid live source (e.g. an Apify Google Flights actor) for a small targeted window of hours
