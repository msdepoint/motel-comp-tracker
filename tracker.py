import csv
from datetime import date, datetime, timedelta
import json
import os
import re
import time
from typing import Any, Dict, List
import pandas as pd
from playwright.sync_api import sync_playwright

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


def scrape_listing_with_playwright(
    listing_id: str, room_name: str, today_str: str, cutoff_date: date
) -> List[Dict[str, Any]]:
  """Launches headless Chromium, navigates to listing, and intercepts calendar data."""
  captured_days = []

  url = f"https://www.airbnb.com/rooms/{listing_id}"
  print(f"  -> Opening {room_name} ({listing_id})...")

  with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
        locale="en-US",
    )
    page = context.new_page()

    # Intercept network responses containing calendar data
    def on_response(response):
      if (
          "PdpAvailabilityCalendar" in response.url
          or "calendar_months" in response.url
      ):
        try:
          data = response.json()
          # Parse GraphQL PdpAvailabilityCalendar response
          months = (
              data.get("data", {})
              .get("merlin", {})
              .get("pdpAvailabilityCalendar", {})
              .get("calendarMonths", [])
          )
          if not months:
            months = data.get("calendar_months", [])

          for m in months:
            for day_info in m.get("days", []):
              d_str = day_info.get("date")
              if not d_str:
                continue

              d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
              if date.today() <= d_obj <= cutoff_date:
                is_avail = 1 if day_info.get("available") is True else 0

                # Extract price
                price_val = None
                price_dict = day_info.get("price", {})
                if isinstance(price_dict, dict):
                  price_val = price_dict.get("local_price") or price_dict.get(
                      "native_price"
                  )
                elif day_info.get("priceString"):
                  m_p = re.search(r"\$(\d+)", day_info["priceString"])
                  if m_p:
                    price_val = float(m_p.group(1))

                min_nights = day_info.get("min_nights") or 1

                captured_days.append({
                    "snapshot_date": today_str,
                    "stay_date": d_str,
                    "listing_id": listing_id,
                    "room_name": room_name,
                    "is_available": is_avail,
                    "price_usd": price_val,
                    "min_nights": min_nights,
                })
        except Exception:
          pass

    page.on("response", on_response)

    try:
      # Navigate to listing page and wait for initial network activity to settle
      page.goto(url, wait_until="domcontentloaded", timeout=45000)
      page.wait_for_timeout(5000)

      # Scroll down to trigger lazy loading of the calendar section
      page.evaluate("window.scrollBy(0, 1000)")
      page.wait_for_timeout(4000)

      # If network interception didn't trigger, click on the check-in date picker to open calendar
      if not captured_days:
        date_picker = page.query_selector(
            'div[data-testid="change-dates-checkIn"]'
        )
        if date_picker:
          date_picker.click()
          page.wait_for_timeout(4000)

    except Exception as err:
      print(f"    Page error on {listing_id}: {err}")
    finally:
      browser.close()

  # Deduplicate captured records by stay_date
  unique_records = {}
  for r in captured_days:
    unique_records[r["stay_date"]] = r

  print(f"    Extracted {len(unique_records)} calendar dates.")
  return list(unique_records.values())


# ----------------------------------------------------------------------
# Analytics & Reporting
# ----------------------------------------------------------------------
def analyze_historical(df: pd.DataFrame, today: date) -> Dict[str, Any]:
  res = {
      "has_history": False,
      "past_7d_occ": None,
      "past_30d_occ": None,
      "past_30d_adr": None,
      "median_lead_time": None,
      "count": 0,
  }
  if df.empty:
    return res

  past = df[df["stay_date"] < pd.to_datetime(today)].copy()
  if past.empty:
    return res

  res["has_history"] = True
  latest = (
      past.sort_values("snapshot_date")
      .groupby(["stay_date", "listing_id"])
      .last()
      .reset_index()
  )
  daily = latest.groupby("stay_date").agg(
      total=("listing_id", "count"),
      avail=("is_available", "sum"),
      price=("price_usd", "mean"),
  )
  daily["occ"] = ((daily["total"] - daily["avail"]) / daily["total"]) * 100

  p7 = daily[daily.index >= pd.to_datetime(today - timedelta(days=7))]
  if not p7.empty:
    res["past_7d_occ"] = round(p7["occ"].mean(), 1)

  p30 = daily[daily.index >= pd.to_datetime(today - timedelta(days=30))]
  if not p30.empty:
    res["past_30d_occ"] = round(p30["occ"].mean(), 1)
    res["past_30d_adr"] = round(p30["price"].mean(), 2)
    res["count"] = len(p30) * 4

  # Booking lead time detection
  sorted_df = df.sort_values(["listing_id", "stay_date", "snapshot_date"])
  sorted_df["prev_avail"] = sorted_df.groupby(["listing_id", "stay_date"])[
      "is_available"
  ].shift(1)
  booked_events = sorted_df[
      (sorted_df["prev_avail"] == 1) & (sorted_df["is_available"] == 0)
  ]
  if not booked_events.empty:
    leads = (booked_events["stay_date"] - booked_events["snapshot_date"]).dt.days
    leads = leads[leads >= 0]
    if not leads.empty:
      res["median_lead_time"] = round(leads.median(), 1)

  return res


def analyze_forward(
    rows: List[Dict[str, Any]], today: date
) -> Dict[str, Any]:
  dates: Dict[str, Dict[str, Any]] = {}
  for r in rows:
    sd = r["stay_date"]
    if sd not in dates:
      dates[sd] = {"avail": 0, "total": 0}
    dates[sd]["total"] += 1
    if r["is_available"] == 1:
      dates[sd]["avail"] += 1

  def window_occ(start_off, end_off):
    s = today + timedelta(days=start_off)
    e = today + timedelta(days=end_off)
    cap = 0
    booked = 0
    for d_str, d_val in dates.items():
      d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
      if s <= d_obj <= e:
        cap += 4
        booked += max(0, 4 - d_val["avail"])
    return round((booked / cap * 100), 1) if cap > 0 else 0.0

  compressions = []
  for d_str in sorted(dates.keys()):
    d_val = dates[d_str]
    booked_count = 4 - d_val["avail"]
    if booked_count >= 3:
      compressions.append((d_str, booked_count))

  return {
      "occ_7d": window_occ(0, 7),
      "occ_30d": window_occ(0, 30),
      "occ_60d": window_occ(0, 60),
      "compression_dates": compressions,
  }


def write_report(hist: Dict[str, Any], fwd: Dict[str, Any], snap_dt: str):
  with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
    f.write("# Harpers Riverside Motel Competitor Intelligence Brief\n\n")
    f.write(f"**Last Data Refresh**: `{snap_dt}`\n\n")

    f.write("## 1. Historical Realized Performance\n")
    if hist["has_history"]:
      f.write(
          f"- **Past 7-Day Realized Occupancy**:"
          f" **{hist['past_7d_occ'] or 'N/A'}%**\n"
      )
      f.write(
          f"- **Past 30-Day Realized Occupancy**:"
          f" **{hist['past_30d_occ'] or 'N/A'}%**\n"
      )
      f.write(
          f"- **Past 30-Day Realized ADR**: **${hist['past_30d_adr'] or 0.0}**\n"
      )
      f.write(
          f"- **Median Booking Lead Time**: **{hist['median_lead_time'] or 'N/A'}"
          " days**\n\n"
      )
    else:
      f.write(
          "*Historical data will populate as snapshots accumulate over past"
          " stay dates.*\n\n"
      )

    f.write("## 2. Forward Occupancy Pacing\n")
    f.write(f"- **Next 7 Days Projected Occupancy**: **{fwd['occ_7d']}%**\n")
    f.write(f"- **Next 30 Days Projected Occupancy**: **{fwd['occ_30d']}%**\n")
    f.write(f"- **Next 60 Days Projected Occupancy**: **{fwd['occ_60d']}%**\n\n")

    f.write("## 3. Supply Compression Dates (>= 75% Booked)\n")
    if fwd["compression_dates"]:
      f.write(
          "| Stay Date | Rooms Booked | Occupancy | Recommended Pricing Action"
          " |\n"
      )
      f.write(
          "| :--- | :--- | :--- | :--- |\n"
      )
      for d_str, count in fwd["compression_dates"][:15]:
        action = (
            "Surge rates +25% to +35%, 2-night min"
            if count == 4
            else "Raise rates +15% to +20%"
        )
        f.write(f"| {d_str} | {count}/4 | {int(count/4*100)}% | {action} |\n")
    else:
      f.write(
          "No dates currently exceed the 75% compression threshold in the"
          " next 90 days.\n"
      )


# ----------------------------------------------------------------------
# Pipeline Entry Point
# ----------------------------------------------------------------------
def run():
  os.makedirs(DATA_DIR, exist_ok=True)
  today = date.today()
  today_str = today.strftime("%Y-%m-%d")
  cutoff = today + timedelta(days=FORWARD_DAYS)

  print(f"Starting browser-based sweep for {today_str}...")
  all_rows = []

  for listing_id, room_name in LISTINGS.items():
    rows = scrape_listing_with_playwright(
        listing_id, room_name, today_str, cutoff
    )
    all_rows.extend(rows)
    time.sleep(3)

  if not all_rows:
    print("Warning: No records were extracted on this run.")
    return

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
    writer.writerows(all_rows)

  print(f"Successfully saved {len(all_rows)} rows to {CSV_FILE}.")

  df = pd.read_csv(CSV_FILE)
  df["stay_date"] = pd.to_datetime(df["stay_date"])
  df["snapshot_date"] = pd.to_datetime(df["snapshot_date"])

  hist = analyze_historical(df, today)
  fwd = analyze_forward(all_rows, today)
  write_report(hist, fwd, today_str)


if __name__ == "__main__":
  run()
