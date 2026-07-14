# Analysis

Two-step pipeline: a Python exporter pulls Firestore data into a flat CSV, then an R script does the pattern analysis. Run this on your own machine, not in GitHub Actions.

## When to run

Wait until you have **4-6 weeks** of data. Before that, there aren't enough genuine observations per hour/weekday cell for patterns to separate from noise — the scripts will run, but the results won't mean anything. The R script prints a warning when data is thin.

## Step 1 — Export from Firestore to CSV (Python)

1. Download your Firebase service account key (Firebase console → Project settings → Service accounts → Generate new private key) and save it in this folder as `serviceAccount.json`. **Never commit this file** — it's gitignored.
2. Install the client: `pip install firebase-admin`
3. Run: `python export_to_csv.py`

This produces `prices.csv` — one row per genuine-or-suspect price observation, deduplicated across overlapping polls on `(route, departure_date, found_at, price, gate)`.

## Step 2 — Analyse (R)

1. Install packages (once): `install.packages(c("tidyverse", "lubridate"))`
2. Run: `Rscript analyse_prices.R` (or open in RStudio and run interactively)

Outputs:
- `plot_by_hour.png` — median fare by local hour, per route
- `plot_by_weekday.png` — median fare by weekday, per route
- `plot_heatmap.png` — hour × weekday heatmap (the direct "Tuesday 3am" test)
- `plot_cheapest_combined.png` — combined answer: cheapest slot per route, with the cheapest reliable slot ringed in black and the observation count printed in each cell
- printed summary tables + an ANOVA testing whether hour/weekday explain price

## How to read a ringed cell (IMPORTANT: one ring ≠ proof)

The black ring on `plot_cheapest_combined.png` marks the cheapest slot that has at least `MIN_OBS` observations. That threshold is the **minimum to be worth looking at, not evidence that the pattern is real.** A ring sitting on a cell with just 3-4 observations is a *hypothesis to watch*, not a finding — a couple of stray cheap fares landing in one cell is enough to create it.

The only real test is **persistence**: a genuine pattern stays in the same cell (and gets more clearly ringed) as weeks of data accumulate; a fluke drifts elsewhere or vanishes. So before believing any "cheapest = <day> <hour>" claim: (1) check the observation count in that cell is high, not just ≥ MIN_OBS; (2) re-run a few weeks later and confirm the ring is still there; (3) check the ANOVA actually reports a significant time effect for that route. Until all three hold, treat a ring as "interesting," not "true."

## Reading the results

- The script filters to **genuine** fares only (drops cache-filler) and converts USD→AUD with a rough 1.5 rate — update `USD_TO_AUD` in the script for a current rate.
- `found_at` is converted from UTC to `Australia/Hobart` (handles AEST/AEDT automatically). All hour/weekday grouping is in local time, so "Tuesday 3am" means Tuesday 3am in Hobart.
- **Watch observation counts.** A cheap-looking hour with only 2-3 observations is noise. Plots encode count as bar opacity; faint bars are unreliable. The ANOVA only runs once a route has enough data.
- Routes with little Aviasales search traffic may return sparse data. The analysis simply focuses on whichever routes have enough genuine observations.

## Note on the currency rate

The tracker records prices in USD (the API ignores the AUD request). The `USD_TO_AUD` constant is a convenience for readable AUD figures; it does not affect pattern detection, which is about relative changes.
