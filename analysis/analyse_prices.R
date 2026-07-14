# =============================================================================
# Flight price pattern analysis
# Tests whether cheapest fares systematically dip at particular hours of the
# day or days of the week ("the Tuesday 3am" folklore), per route.
#
# Input:  prices.csv  (produced by export_to_csv.py)
# Output: several plots (PNG) + printed summary tables
#
# Usage:
#   Rscript analyse_prices.R
# or run interactively in RStudio.
# =============================================================================

library(tidyverse)
library(lubridate)

# ---- Config -----------------------------------------------------------------
# found_at timestamps are UTC. Hobart is UTC+10 (AEST) / UTC+11 (AEDT);
# lubridate handles the DST switch automatically with this tz name.
LOCAL_TZ <- "Australia/Hobart"
USD_TO_AUD <- 1.5          # rough; update to current rate if you want AUD
INPUT <- "prices.csv"

# ---- Load & clean -----------------------------------------------------------
raw <- read_csv(INPUT, show_col_types = FALSE)

df <- raw %>%
  # Genuine observed fares only - drop cache-filler rows
  filter(suspect == FALSE | tolower(as.character(suspect)) == "false") %>%
  filter(!is.na(price), !is.na(found_at)) %>%
  mutate(
    found_utc   = ymd_hms(found_at, tz = "UTC"),
    found_local = with_tz(found_utc, LOCAL_TZ),
    hour_local  = hour(found_local),
    # Order weekdays Monday-first for readable plots
    wday_local  = wday(found_local, label = TRUE, week_start = 1),
    price_aud   = price * USD_TO_AUD
  ) %>%
  filter(!is.na(found_local))

message("Loaded ", nrow(raw), " rows; ", nrow(df), " genuine observations after cleaning.")

if (nrow(df) < 50) {
  message("\n** Very little data so far. Results below are illustrative only.")
  message("** Let the tracker run for 4-6 weeks before trusting any pattern. **\n")
}

# Report observation counts so thin cells are visible
cat("\nObservations per route:\n")
df %>% count(route) %>% arrange(desc(n)) %>% print()

# =============================================================================
# 1. Hour-of-day: is any local hour systematically cheaper?
# =============================================================================
by_hour <- df %>%
  group_by(route, hour_local) %>%
  summarise(
    n           = n(),
    median_aud  = median(price_aud),
    mean_aud    = mean(price_aud),
    .groups = "drop"
  )

cat("\n--- Median price by local hour (per route) ---\n")
by_hour %>%
  select(route, hour_local, n, median_aud) %>%
  arrange(route, hour_local) %>%
  print(n = Inf)

p_hour <- ggplot(by_hour, aes(hour_local, median_aud)) +
  geom_col(aes(alpha = n), fill = "steelblue") +
  facet_wrap(~ route, scales = "free_y") +
  scale_x_continuous(breaks = seq(0, 23, 3)) +
  scale_alpha(range = c(0.35, 1), name = "obs count") +
  labs(
    title    = "Median fare by hour of day (local Hobart time)",
    subtitle = "Bar opacity = number of observations; faint bars are unreliable",
    x = "Hour of day (0-23)", y = "Median fare (AUD, approx)"
  ) +
  theme_minimal()

ggsave("plot_by_hour.png", p_hour, width = 10, height = 6, dpi = 120)

# =============================================================================
# 2. Day-of-week: is any weekday systematically cheaper?
# =============================================================================
by_wday <- df %>%
  group_by(route, wday_local) %>%
  summarise(
    n          = n(),
    median_aud = median(price_aud),
    .groups = "drop"
  )

cat("\n--- Median price by weekday (per route) ---\n")
by_wday %>% arrange(route, wday_local) %>% print(n = Inf)

p_wday <- ggplot(by_wday, aes(wday_local, median_aud)) +
  geom_col(aes(alpha = n), fill = "darkgreen") +
  facet_wrap(~ route, scales = "free_y") +
  scale_alpha(range = c(0.35, 1), name = "obs count") +
  labs(
    title    = "Median fare by day of week (local Hobart time)",
    subtitle = "Bar opacity = number of observations",
    x = NULL, y = "Median fare (AUD, approx)"
  ) +
  theme_minimal()

ggsave("plot_by_weekday.png", p_wday, width = 10, height = 6, dpi = 120)

# =============================================================================
# 3. Hour x weekday heatmap - the direct "Tuesday 3am" test
# =============================================================================
heat <- df %>%
  group_by(route, wday_local, hour_local) %>%
  summarise(median_aud = median(price_aud), n = n(), .groups = "drop")

p_heat <- ggplot(heat, aes(hour_local, wday_local, fill = median_aud)) +
  geom_tile() +
  facet_wrap(~ route) +
  scale_fill_viridis_c(option = "plasma", name = "Median AUD") +
  scale_x_continuous(breaks = seq(0, 23, 3)) +
  labs(
    title    = "Median fare by hour x weekday",
    subtitle = "Darker = cheaper. Look for consistent cool spots (e.g. Tue small hours)",
    x = "Hour of day (local)", y = NULL
  ) +
  theme_minimal()

ggsave("plot_heatmap.png", p_heat, width = 11, height = 6, dpi = 120)

# =============================================================================
# 4. Simple significance check: does hour/weekday explain price variation?
#    (Only meaningful once you have several weeks of data.)
# =============================================================================
cat("\n--- Does time explain price? (ANOVA, per route) ---\n")
for (r in unique(df$route)) {
  sub <- df %>% filter(route == r)
  if (nrow(sub) < 100 || n_distinct(sub$hour_local) < 3) {
    cat(sprintf("%s: not enough data for a meaningful test yet (n=%d)\n",
                r, nrow(sub)))
    next
  }
  fit <- aov(price_aud ~ factor(hour_local) + wday_local, data = sub)
  cat(sprintf("\n== %s ==\n", r))
  print(summary(fit))
}

# =============================================================================
# 5. Combined "cheapest slot" summary - one picture, all routes.
#    Shows, for each route, the median fare in every hour x weekday cell that
#    HAS DATA, and highlights the single cheapest reliable slot per route.
#    Reliability guard: a slot needs at least MIN_OBS observations to be
#    eligible as "the cheapest", so a lone fluke fare can't win.
# =============================================================================
MIN_OBS <- 3   # raise this as data grows (e.g. 5-10 after a few weeks)

slot <- df %>%
  group_by(route, wday_local, hour_local) %>%
  summarise(median_aud = median(price_aud), n = n(), .groups = "drop")

# The cheapest *reliable* slot per route (enough observations to trust)
cheapest_slot <- slot %>%
  filter(n >= MIN_OBS) %>%
  group_by(route) %>%
  slice_min(median_aud, n = 1, with_ties = FALSE) %>%
  ungroup()

cat("\n--- Cheapest reliable slot per route (>= ", MIN_OBS,
    " observations) ---\n", sep = "")
if (nrow(cheapest_slot) == 0) {
  cat("No slot yet has enough observations to call reliably.\n")
  cat("This is expected early on - let the data accumulate.\n")
} else {
  cheapest_slot %>%
    transmute(route, weekday = wday_local, hour_local,
              median_aud = round(median_aud), n) %>%
    arrange(median_aud) %>%
    print()
}

# Combined single-picture view: faceted heatmap with the cheapest reliable
# slot ringed, and observation counts printed in each cell so you can see
# at a glance which cells are trustworthy.
p_combined <- ggplot(slot, aes(hour_local, wday_local)) +
  geom_tile(aes(fill = median_aud)) +
  geom_text(aes(label = n), size = 2.6, colour = "grey20") +
  # Ring the cheapest reliable slot per route
  geom_tile(data = cheapest_slot, fill = NA, colour = "black", linewidth = 1.1) +
  facet_wrap(~ route) +
  scale_fill_viridis_c(option = "plasma", name = "Median AUD") +
  scale_x_continuous(breaks = seq(0, 23, 3)) +
  labs(
    title    = "Cheapest fare by hour x weekday (local Hobart time)",
    subtitle = paste0("Number in each cell = observations. Black ring = cheapest slot ",
                      "with >= ", MIN_OBS, " obs. Empty cells have no data yet."),
    x = "Hour of day (local)", y = NULL
  ) +
  theme_minimal()

ggsave("plot_cheapest_combined.png", p_combined, width = 11, height = 6, dpi = 120)

cat("\nDone. Plots written: plot_by_hour.png, plot_by_weekday.png, ",
    "plot_heatmap.png, plot_cheapest_combined.png\n", sep = "")
cat("Reminder: cells with few observations are noise. The black ring only\n")
cat("appears once a slot clears the MIN_OBS threshold - trust that, not the\n")
cat("brightest raw cell. Raise MIN_OBS as weeks of data accumulate.\n")
