import csv
from datetime import date, datetime, timedelta
import os
import time
from typing import Any, Dict, List
import pandas as pd
import requests

# ----------------------------------------------------------------------
# Configuration & Target Competitor Listings (Stone Manor Motel)
# ----------------------------------------------------------------------
LISTINGS = {
    "1697396664319697096": "Room 5 (Queen)",
    "1697399341843035717": "Room 6 (Queen)",
    "1695167216674688814": "Room 7 (Queen/Trundle)",
    "1679897808235823780": "Room 8 (King/Twins)",
}

DATA_DIR = "data"
CSV_FILE = os.path.join(DATA_DIR, "daily_snapshots.csv")
SUMMARY_FILE = os.path.join(DATA_DIR, "latest_summary.md")
FORWARD_DAYS = 90

# Standard public client API key used by Airbnb's web frontend
AIRBNB_API_KEY = "d306zoyjsyarp7ifhu67rjxn52tv0t20"


# ----------------------------------------------------------------------
# Data Ingestion: Public Calendar Scraping
# ----------------------------------------------------------------------
def fetch_calendar_for_listing(
    listing_id: str, start_date: date
) -> List[Dict[str, Any]]:
  """Queries Airbnb's public calendar endpoint for pricing and availability."""
  endpoint = "https://www.airbnb.com/api/v2/calendar_months"
  params = {
      "_format": "with_conditions",
      "count": 4,  # Fetch 3-4 consecutive calendar months
      "currency": "USD",
      "key": AIRBNB_API_KEY,
      "listing_id": listing_id,
      "locale": "en",
      "month": start_date.month,
      "year": start_date.year,
  }

  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
          " like Gecko) Chrome/128.0.0.0 Safari/537.36"
      ),
      "Accept": "application/json",
      "Referer": f"https://www.airbnb.com/rooms/{listing_id}",
  }

  try:
    response = requests.get(
        endpoint, params=params, headers=headers, timeout=20
    )
    if response.status_code == 200:
      payload = response.json()
      calendar_days = []
      for month in payload.get("calendar_months", []):
        for day in month.get("days", []):
          calendar_days.append(day)
      return calendar_days
    else:
      print(f"Warning: HTTP {response.status_code} for listing {listing_id}.")
  except Exception as err:
    print(f"Network error requesting listing {listing_id}: {err}")

  return []


def parse_day_entry(
    day_data: Dict[str, Any], snapshot_dt: str, listing_id: str, room_name: str
) -> Dict[str, Any]:
  """Normalizes calendar day responses into structured records."""
  stay_date = day_data.get("date")
  is_available = 1 if day_data.get("available") is True else 0

  price = None
  price_info = day_data.get("price", {})
  if isinstance(price_info, dict):
    price = price_info.get("local_price") or price_info.get("native_price")

  min_nights = (
      day_data.get("min_nights")
      or day_data.get("min_nights_input_value")
      or None
  )

  return {
      "snapshot_date": snapshot_dt,
      "stay_date": stay_date,
      "listing_id": listing_id,
      "room_name": room_name,
      "is_available": is_available,
      "price_usd": price,
      "min_nights": min_nights,
  }


# ----------------------------------------------------------------------
# Historical Analytics Engine
# ----------------------------------------------------------------------
def analyze_historical_performance(
    df: pd.DataFrame, today: date
) -> Dict[str, Any]:
  """Analyzes realized occupancy, historical pricing, and booking lead times."""
  metrics = {
      "has_history": False,
      "past_7d_occ": None,
      "past_30d_occ": None,
      "past_30d_adr": None,
      "avg_lead_time_days": None,
      "total_past_stays_tracked": 0,
  }

  if df.empty:
    return metrics

  # Filter for stay dates that have already occurred
  past_df = df[df["stay_date"] < pd.to_datetime(today)].copy()
  if past_df.empty:
    return metrics

  metrics["has_history"] = True

  # 1. Final Realized Status: take the latest snapshot recorded prior to or on the stay date
  latest_past = (
      past_df.sort_values("snapshot_date")
      .groupby(["stay_date", "listing_id"])
      .last()
      .reset_index()
  )

  # Aggregate by stay date
  daily_past = latest_past.groupby("stay_date").agg(
      total_rooms=("listing_id", "count"),
      available_rooms=("is_available", "sum"),
      avg_rate=("price_usd", "mean"),
  )
  daily_past["booked_rooms"] = (
      daily_past["total_rooms"] - daily_past["available_rooms"]
  )
  daily_past["occ_pct"] = (
      daily_past["booked_rooms"] / daily_past["total_rooms"]
  ) * 100

  # Past 7 Days
  p7_start = pd.to_datetime(today - timedelta(days=7))
  p7 = daily_past[daily_past.index >= p7_start]
  if not p7.empty:
    metrics["past_7d_occ"] = round(p7["occ_pct"].mean(), 1)

  # Past 30 Days
  p30_start = pd.to_datetime(today - timedelta(days=30))
  p30 = daily_past[daily_past.index >= p30_start]
  if not p30.empty:
    metrics["past_30d_occ"] = round(p30["occ_pct"].mean(), 1)
    metrics["past_30d_adr"] = round(p30["avg_rate"].mean(), 2)
    metrics["total_past_stays_tracked"] = len(p30) * 4

  # 2. Historical Booking Lead Time Analysis (Pacing)
  # Detect when a listing flipped from available (1) to booked (0) across consecutive snapshots
  sorted_df = df.sort_values(["listing_id", "stay_date", "snapshot_date"])
  sorted_df["prev_avail"] = sorted_df.groupby(["listing_id", "stay_date"])[
      "is_available"
  ].shift(1)

  # A booking event occurs when prev_avail == 1 and is_available == 0
  bookings = sorted_df[
      (sorted_df["prev_avail"] == 1) & (sorted_df["is_available"] == 0)
  ].copy()
  if not bookings.empty:
    bookings["lead_time"] = (
        bookings["stay_date"] - bookings["snapshot_date"]
    ).dt.days
    positive_leads = bookings[bookings["lead_time"] >= 0]["lead_time"]
    if not positive_leads.empty:
      metrics["avg_lead_time_days"] = round(positive_leads.median(), 1)

  return metrics


# ----------------------------------------------------------------------
# Forward Forecast & Compression Engine
# ----------------------------------------------------------------------
def analyze_forward_pacing(
    current_rows: List[Dict[str, Any]], today: date
) -> Dict[str, Any]:
  """Computes forward occupancy windows and detects high-compression dates."""
  dates: Dict[str, Dict[str, Any]] = {}
  for r in current_rows:
    sd = r["stay_date"]
    if sd not in dates:
      dates[sd] = {"total_rooms": 0, "available_rooms": 0, "prices": []}
    dates[sd]["total_rooms"] += 1
    if r["is_available"] == 1:
      dates[sd]["available_rooms"] += 1
    if r["price_usd"] is not None:
      dates[sd]["prices"].append(float(r["price_usd"]))

  def calc_window(start_off: int, end_off: int):
    t_start = today + timedelta(days=start_off)
    t_end = today + timedelta(days=end_off)
    capacity = 0
    booked = 0
    for d_str, data in dates.items():
      d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
      if t_start <= d_obj <= t_end:
        capacity += 4
        booked += max(0, 4 - data["available_rooms"])
    return round((booked / capacity * 100), 1) if capacity > 0 else 0.0

  compression = []
  for d_str in sorted(dates.keys()):
    d_data = dates[d_str]
    booked_count = 4 - d_data["available_rooms"]
    if booked_count >= 3:
      compression.append((d_str, booked_count))

  return {
      "occ_7d": calc_window(0, 7),
      "occ_30d": calc_window(0, 30),
      "occ_60d": calc_window(0, 60),
      "compression_dates": compression,
  }


# ----------------------------------------------------------------------
# Markdown Summary Report Generator
# ----------------------------------------------------------------------
def write_consolidated_report(
    hist_metrics: Dict[str, Any],
    fwd_metrics: Dict[str, Any],
    snapshot_date: str,
):
  """Writes a comprehensive operational intelligence brief to markdown."""
  with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
    f.write("# Stone Manor Competitor Intelligence Brief\n\n")
    f.write(f"**Last Data Refresh**: `{snapshot_date}`\n\n")
    f.write(
        "> **Context**: 4-room duopoly tracking (Stone Manor Rooms 5, 6, 7,"
        " 8). Capacity = 4 room-nights/day.\n\n"
    )

    # 1. Historical Realized Performance
    f.write("## 1. Historical Realized Performance\n")
    if hist_metrics["has_history"]:
      past_7 = (
          f"{hist_metrics['past_7d_occ']}%"
          if hist_metrics["past_7d_occ"] is not None
          else "N/A"
      )
      past_30 = (
          f"{hist_metrics['past_30d_occ']}%"
          if hist_metrics["past_30d_occ"] is not None
          else "N/A"
      )
      adr_30 = (
          f"${hist_metrics['past_30d_adr']:.2f}"
          if hist_metrics["past_30d_adr"] is not None
          else "N/A"
      )
      lead_time = (
          f"{hist_metrics['avg_lead_time_days']} days"
          if hist_metrics["avg_lead_time_days"] is not None
          else "N/A"
      )

      f.write(f"- **Past 7-Day Realized Occupancy**: **{past_7}**\n")
      f.write(f"- **Past 30-Day Realized Occupancy**: **{past_30}**\n")
      f.write(f"- **Past 30-Day Average Advertised Rate (ADR)**: **{adr_30}**\n")
      f.write(f"- **Median Booking Lead Time**: **{lead_time}**\n")
      f.write(
          f"- *Total Past Room-Nights Analyzed*: "
          f"{hist_metrics['total_past_stays_tracked']}\n\n"
      )
    else:
      f.write(
          "*Historical data is currently accumulating. Once the tracker has"
          " collected snapshots over multiple calendar dates, realized"
          " occupancy, historical ADR, and booking lead times will populate"
          " automatically here.*\n\n"
      )

    # 2. Forward-Looking Demand & Occupancy Pacing
    f.write("## 2. Forward-Looking Occupancy Pacing\n")
    f.write(
        f"- **Next 7 Days Projected Occupancy**: **{fwd_metrics['occ_7d']}%**\n"
    )
    f.write(
        f"- **Next 30 Days Projected Occupancy**:"
        f" **{fwd_metrics['occ_30d']}%**\n"
    )
    f.write(
        f"- **Next 60 Days Projected Occupancy**:"
        f" **{fwd_metrics['occ_60d']}%**\n\n"
    )

    # 3. Tactical Pricing & Compression Triggers
    f.write("## 3. High Compression Dates (Competitor >= 75% Full)\n")
    f.write(
        "When Stone Manor has 3 or 4 rooms booked, town-wide supply is near"
        " zero. Take immediate pricing action:\n\n"
    )

    compression_dates = fwd_metrics["compression_dates"]
    if compression_dates:
      f.write(
          "| Stay Date | Competitor Booked | Occupancy | Recommended Action"
          " |\n"
      )
      f.write(
          "| :--- | :--- | :--- | :--- |\n"
      )
      for d_str, count in compression_dates[:20]:
        action = (
            "Surge rate +25% to +35%, enforce 2-night minimum"
            if count == 4
            else "Raise rate +15% to +20%"
        )
        f.write(f"| {d_str} | {count}/4 | {int(count/4*100)}% | {action} |\n")
      f.write("\n")
    else:
      f.write(
          "No dates currently exceed the 75% compression threshold in the"
          " next 90 days.\n\n"
      )


# ----------------------------------------------------------------------
# Main Execution Pipeline
# ----------------------------------------------------------------------
def run_pipeline():
  os.makedirs(DATA_DIR, exist_ok=True)
  today = date.today()
  today_str = today.strftime("%Y-%m-%d")
  cutoff_date = today + timedelta(days=FORWARD_DAYS)

  new_rows: List[Dict[str, Any]] = []
  print(
      f"[{today_str}] Initiating competitor sweep for Stone Manor (90-day"
      " window)..."
  )

  # 1. Fetch live forward calendar
  for listing_id, room_name in LISTINGS.items():
    print(f"  -> Fetching {room_name} ({listing_id})...")
    days = fetch_calendar_for_listing(listing_id, today)

    for d in days:
      date_str = d.get("date")
      if not date_str:
        continue
      d_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
      if today <= d_obj <= cutoff_date:
        parsed = parse_day_entry(d, today_str, listing_id, room_name)
        new_rows.append(parsed)

    time.sleep(2)  # Respectful delay between listing requests

  # 2. Append new rows to CSV ledger
  fieldnames = [
      "snapshot_date",
      "stay_date",
      "listing_id",
      "room_name",
      "is_available",
      "price_usd",
      "min_nights",
  ]
  file_exists = os.path.isfile(CSV_FILE)

  with open(CSV_FILE, mode="a", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    if not file_exists:
      writer.writeheader()
    writer.writerows(new_rows)

  print(f"Logged {len(new_rows)} forward records to {CSV_FILE}.")

  # 3. Read accumulated dataset for historical & forward reporting
  df_all = pd.read_csv(CSV_FILE)
  df_all["stay_date"] = pd.to_datetime(df_all["stay_date"])
  df_all["snapshot_date"] = pd.to_datetime(df_all["snapshot_date"])

  hist_metrics = analyze_historical_performance(df_all, today)
  fwd_metrics = analyze_forward_pacing(new_rows, today)

  # 4. Generate consolidated summary document
  write_consolidated_report(hist_metrics, fwd_metrics, today_str)
  print(f"Successfully generated summary report at {SUMMARY_FILE}.")


if __name__ == "__main__":
  run_pipeline()
