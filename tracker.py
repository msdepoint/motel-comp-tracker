import csv
from datetime import date, datetime, timedelta
import json
import os
import re
import sys
import time
from typing import Any, Dict, List
import pandas as pd
from playwright.sync_api import sync_playwright

# ----------------------------------------------------------------------
# Configuration: Harpers Riverside Motel Comp (Competitor Listings)
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


def parse_date_str(raw: str) -> str:
  """Normalizes various Airbnb date formats into YYYY-MM-DD."""
  raw = raw.replace("calendar-day-", "").strip()
  for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%m_%d_%Y"):
    try:
      return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
    except ValueError:
      continue
  return None


def scrape_listing(
    listing_id: str, room_name: str, today_str: str, cutoff_date: date
) -> List[Dict[str, Any]]:
  """Scrapes calendar data using rendered DOM elements with network interception fallback."""
  captured_data: Dict[str, Dict[str, Any]] = {}
  url = f"https://www.airbnb.com/rooms/{listing_id}"
  print(f"\n--- Scraping {room_name} (ID: {listing_id}) ---")

  with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
        ],
    )
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1440, "height": 900},
        locale="en-US",
        timezone_id="America/New_York",
    )
    page = context.new_page()

    # Stream 1: Background Network Interception
    def handle_response(response):
      if (
          "api/v3" in response.url
          or "graphql" in response.url
          or "calendar" in response.url
      ):
        try:
          text = response.text()
          if (
              "calendarMonths" in text
              or "pdpAvailabilityCalendar" in text
              or "calendar_months" in text
          ):
            data = json.loads(text)
            months = (
                data.get("data", {})
                .get("merlin", {})
                .get("pdpAvailabilityCalendar", {})
                .get("calendarMonths", [])
            )
            if not months:
              months = (
                  data.get("data", {})
                  .get("node", {})
                  .get("calendarMonths", [])
              )
            if not months:
              months = data.get("calendar_months", [])

            for m in months:
              for day_info in m.get("days", []):
                d_str = day_info.get("date")
                if d_str and d_str not in captured_data:
                  price_val = None
                  p_dict = day_info.get("price", {})
                  if isinstance(p_dict, dict):
                    price_val = p_dict.get("local_price") or p_dict.get(
                        "native_price"
                    )
                  is_avail = 1 if day_info.get("available") is True else 0
                  captured_data[d_str] = {
                      "snapshot_date": today_str,
                      "stay_date": d_str,
                      "listing_id": listing_id,
                      "room_name": room_name,
                      "is_available": is_avail,
                      "price_usd": price_val,
                      "min_nights": day_info.get("min_nights") or 1,
                  }
        except Exception:
          pass

    page.on("response", handle_response)

    try:
      page.goto(url, wait_until="domcontentloaded", timeout=40000)
      page.wait_for_timeout(4000)

      # Dismiss common popups/translation modals if present
      for close_btn in [
          'button[aria-label="Close"]',
          'button:has-text("Accept")',
          'button:has-text("OK")',
      ]:
        try:
          if page.is_visible(close_btn, timeout=1500):
            page.click(close_btn)
            page.wait_for_timeout(1000)
        except Exception:
          pass

      # Scroll down to bring calendar into view
      page.evaluate("window.scrollBy(0, 1100)")
      page.wait_for_timeout(3000)

      # Stream 2: DOM-based Calendar Extraction across multiple months
      for _ in range(3):
        day_buttons = page.query_selector_all('[data-testid*="calendar-day-"]')

        for btn in day_buttons:
          test_id = btn.get_attribute("data-testid") or ""
          stay_dt = parse_date_str(test_id)
          if not stay_dt:
            continue

          aria_label = btn.get_attribute("aria-label") or ""
          aria_disabled = btn.get_attribute("aria-disabled")

          is_avail = 1
          if (
              aria_disabled in (True, "true")
              or "unavailable" in aria_label.lower()
          ):
            is_avail = 0

          price_val = None
          m_price = re.search(r"\$(\d+)", aria_label)
          if not m_price:
            m_price = re.search(r"\$(\d+)", btn.inner_text())
          if m_price:
            price_val = float(m_price.group(1))

          if stay_dt not in captured_data:
            captured_data[stay_dt] = {
                "snapshot_date": today_str,
                "stay_date": stay_dt,
                "listing_id": listing_id,
                "room_name": room_name,
                "is_available": is_avail,
                "price_usd": price_val,
                "min_nights": 1,
            }

        # Click forward to next calendar month
        next_btn = page.query_selector(
            'button[aria-label*="Move forward"], button[aria-label*="Next'
            ' month"]'
        )
        if next_btn and next_btn.is_enabled():
          try:
            next_btn.click()
            page.wait_for_timeout(2000)
          except Exception:
            break
        else:
          break

    except Exception as err:
      print(f"Error navigating {listing_id}: {err}")
    finally:
      browser.close()

  # Filter to forward window
  today_date = date.today()
  filtered = []
  for s_date, record in captured_data.items():
    try:
      d_obj = datetime.strptime(s_date, "%Y-%m-%d").date()
      if today_date <= d_obj <= cutoff_date:
        filtered.append(record)
    except Exception:
      pass

  print(f"Extracted {len(filtered)} valid future dates for {room_name}.")
  return filtered


# ----------------------------------------------------------------------
# Pipeline Execution & Analytics
# ----------------------------------------------------------------------
def run():
  os.makedirs(DATA_DIR, exist_ok=True)
  today = date.today()
  today_str = today.strftime("%Y-%m-%d")
  cutoff = today + timedelta(days=FORWARD_DAYS)

  print(
      f"Starting Harpers Riverside Motel Comp sweep for {today_str} (90-day"
      " window)..."
  )
  all_rows = []

  for listing_id, room_name in LISTINGS.items():
    rows = scrape_listing(listing_id, room_name, today_str, cutoff)
    all_rows.extend(rows)
    time.sleep(2)

  if not all_rows:
    print(
        "\n[FATAL] No records extracted from any of the comp listings.",
        file=sys.stderr,
    )
    sys.exit(1)

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

  print(
      f"\nSuccessfully wrote {len(all_rows)} rows to {CSV_FILE} for"
      f" {today_str}."
  )

  update_summary(all_rows, today_str)


def update_summary(rows: List[Dict[str, Any]], today_str: str):
  today = datetime.strptime(today_str, "%Y-%m-%d").date()
  dates: Dict[str, Dict[str, Any]] = {}
  for r in rows:
    sd = r["stay_date"]
    if sd not in dates:
      dates[sd] = {"avail": 0, "total": 0}
    dates[sd]["total"] += 1
    if r["is_available"] == 1:
      dates[sd]["avail"] += 1

  def calc_occ(days_forward):
    target_end = today + timedelta(days=days_forward)
    cap = 0
    booked = 0
    for d_str, val in dates.items():
      d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
      if today <= d_obj <= target_end:
        cap += 4
        booked += max(0, 4 - val["avail"])
    return round((booked / cap * 100), 1) if cap > 0 else 0.0

  compressions = []
  for d_str in sorted(dates.keys()):
    val = dates[d_str]
    booked_count = 4 - val["avail"]
    if booked_count >= 3:
      compressions.append((d_str, booked_count))

  with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
    f.write(f"# Harpers Riverside Motel Comp Intelligence Brief\n\n")
    f.write(f"**Last Refreshed**: `{today_str}`\n\n")
    f.write(
        "> **Context**: 4-room local competitor set. Total comp capacity = 4"
        " room-nights/day.\n\n"
    )
    f.write("## Forward Occupancy Pacing\n")
    f.write(f"- **Next 7 Days Occupancy**: **{calc_occ(7)}%**\n")
    f.write(f"- **Next 30 Days Occupancy**: **{calc_occ(30)}%**\n")
    f.write(f"- **Next 60 Days Occupancy**: **{calc_occ(60)}%**\n\n")

    f.write("## High Supply Compression Dates (>= 75% Booked)\n")
    f.write(
        "When the competitor has 3 or 4 rooms booked, town-wide supply is"
        " constrained. Take immediate pricing action:\n\n"
    )
    if compressions:
      f.write(
          "| Stay Date | Comp Booked | Comp Occupancy | Recommended Action"
          " |\n"
      )
      f.write(
          "| :--- | :--- | :--- | :--- |\n"
      )
      for d_str, count in compressions[:20]:
        action = (
            "Surge rates +25% to +35%, enforce 2-night min"
            if count == 4
            else "Raise rates +15% to +20%"
        )
        f.write(f"| {d_str} | {count}/4 | {int(count/4*100)}% | {action} |\n")
    else:
      f.write(
          "No dates currently exceed 75% compression in the next 90 days.\n"
      )


if __name__ == "__main__":
  run()
