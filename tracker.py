import csv
from datetime import date, datetime, timedelta
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional
import pandas as pd
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

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


def parse_date_str(raw: str) -> Optional[str]:
  """Normalizes various Airbnb calendar date attributes to YYYY-MM-DD."""
  clean = raw.replace("calendar-day-", "").strip()
  for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%m_%d_%Y"):
    try:
      return datetime.strptime(clean, fmt).strftime("%Y-%m-%d")
    except ValueError:
      continue
  return None


def extract_base_price(page) -> Optional[float]:
  """Extracts the advertised base nightly room price from the booking card/header."""
  price_selectors = [
      'span[data-testid*="price"]',
      'div[data-section-id="BOOK_IT_SIDEBAR"] span:has-text("$")',
      'span:has-text("night")',
      'div:has-text("per night")',
  ]
  for sel in price_selectors:
    try:
      elements = page.query_selector_all(sel)
      for el in elements:
        text = el.inner_text()
        m = re.search(r"\$([0-9,]+)", text)
        if m:
          val = float(m.group(1).replace(",", ""))
          if 40 <= val <= 1000:  # Reasonable sanity check for room rate
            return val
    except Exception:
      pass
  return None


def scrape_listing_with_retries(
    listing_id: str,
    room_name: str,
    today_str: str,
    cutoff_date: date,
    max_retries: int = 2,
) -> List[Dict[str, Any]]:
  """Scrapes a listing with retry logic and robust availability checks."""
  url = f"https://www.airbnb.com/rooms/{listing_id}"

  for attempt in range(1, max_retries + 1):
    print(
        f"\n--- Scraping {room_name} ({listing_id}) [Attempt"
        f" {attempt}/{max_retries}] ---"
    )
    captured_data: Dict[str, Dict[str, Any]] = {}

    with sync_playwright() as p:
      browser = p.chromium.launch(
          headless=True,
          args=[
              "--no-sandbox",
              "--disable-blink-features=AutomationControlled",
              "--disable-dev-shm-usage",
          ],
      )
      context = browser.new_context(
          user_agent=(
              "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
              " (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
          ),
          viewport={"width": 1440, "height": 900},
          locale="en-US",
          timezone_id="America/New_York",
      )
      page = context.new_page()

      try:
        page.goto(url, wait_until="domcontentloaded", timeout=50000)
        page.wait_for_timeout(4000)

        # 1. Capture base nightly rate
        room_base_price = extract_base_price(page)
        if room_base_price:
          print(f"    Detected base nightly rate: ${room_base_price:.2f}")
        else:
          print("    Note: Base rate not detected in header; checking DOM cells.")

        # 2. Dismiss cookie/translation overlays
        for modal_btn in [
            'button[aria-label="Close"]',
            'button:has-text("Accept")',
            'button:has-text("OK")',
        ]:
          try:
            if page.is_visible(modal_btn, timeout=1000):
              page.click(modal_btn)
              page.wait_for_timeout(1000)
          except Exception:
            pass

        # 3. Scroll calendar into view
        page.evaluate("window.scrollBy(0, 1100)")
        page.wait_for_timeout(3000)

        # 4. Extract calendar dates across 3 consecutive month clicks
        for month_step in range(3):
          day_buttons = page.query_selector_all(
              '[data-testid*="calendar-day-"]'
          )

          for btn in day_buttons:
            test_id = btn.get_attribute("data-testid") or ""
            stay_dt = parse_date_str(test_id)
            if not stay_dt:
              continue

            aria_label = (btn.get_attribute("aria-label") or "").lower()
            data_blocked = btn.get_attribute("data-is-day-blocked")
            is_html_disabled = btn.is_disabled()

            # Accurate Availability Check
            is_avail = 1
            if (
                is_html_disabled
                or data_blocked in (True, "true", "1")
                or "unavailable" in aria_label
                or "not available" in aria_label
                or "past" in aria_label
            ):
              is_avail = 0

            # Price extraction (cell-level price if available, otherwise base room rate)
            price_val = room_base_price
            cell_text = btn.inner_text()
            m_price = re.search(r"\$([0-9,]+)", aria_label) or re.search(
                r"\$([0-9,]+)", cell_text
            )
            if m_price:
              try:
                price_val = float(m_price.group(1).replace(",", ""))
              except Exception:
                pass

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

          # Click forward to next month
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

      except PlaywrightTimeoutError:
        print(
            f"    Warning: Timeout encountered on attempt {attempt} for"
            f" {room_name}."
        )
      except Exception as err:
        print(f"    Error on attempt {attempt}: {err}")
      finally:
        browser.close()

    # Filter to 90-day forward window
    today_obj = date.today()
    filtered = []
    for s_date, rec in captured_data.items():
      try:
        d_obj = datetime.strptime(s_date, "%Y-%m-%d").date()
        if today_obj <= d_obj <= cutoff_date:
          filtered.append(rec)
      except Exception:
        pass

    if filtered:
      booked_count = sum(1 for r in filtered if r["is_available"] == 0)
      print(
          f"    Success: {len(filtered)} dates captured ({booked_count} booked,"
          f" {len(filtered)-booked_count} available)."
      )
      return filtered

    time.sleep(5)  # Pause before retry

  print(f"    Failed to extract dates for {room_name} after {max_retries} attempts.")
  return []


# ----------------------------------------------------------------------
# Pipeline Runner & Summarizer
# ----------------------------------------------------------------------
def run():
  os.makedirs(DATA_DIR, exist_ok=True)
  today = date.today()
  today_str = today.strftime("%Y-%m-%d")
  cutoff = today + timedelta(days=FORWARD_DAYS)

  print(
      f"Initiating Harpers Riverside Motel Comp sweep for {today_str} (90-day"
      " horizon)..."
  )
  all_rows = []

  for listing_id, room_name in LISTINGS.items():
    rows = scrape_listing_with_retries(listing_id, room_name, today_str, cutoff)
    all_rows.extend(rows)
    time.sleep(4)  # Pacing between rooms to prevent IP rate limits

  if not all_rows:
    print(
        "\n[FATAL] No records extracted from any of the comp listings.",
        file=sys.stderr,
    )
    sys.exit(1)

  # Check listing coverage
  rooms_captured = set(r["room_name"] for r in all_rows)
  print(f"\nRooms successfully captured ({len(rooms_captured)}/4): {rooms_captured}")

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

  print(f"Successfully logged {len(all_rows)} rows to {CSV_FILE}.")

  update_summary(all_rows, today_str)


def update_summary(rows: List[Dict[str, Any]], today_str: str):
  today = datetime.strptime(today_str, "%Y-%m-%d").date()
  dates: Dict[str, Dict[str, Any]] = {}
  for r in rows:
    sd = r["stay_date"]
    if sd not in dates:
      dates[sd] = {"avail": 0, "total": 0, "prices": []}
    dates[sd]["total"] += 1
    if r["is_available"] == 1:
      dates[sd]["avail"] += 1
    if r["price_usd"]:
      dates[sd]["prices"].append(r["price_usd"])

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
    f.write("## Forward Occupancy Pacing\n")
    f.write(f"- **Next 7 Days Occupancy**: **{calc_occ(7)}%**\n")
    f.write(f"- **Next 30 Days Occupancy**: **{calc_occ(30)}%**\n")
    f.write(f"- **Next 60 Days Occupancy**: **{calc_occ(60)}%**\n\n")

    f.write("## High Supply Compression Dates (>= 75% Booked)\n")
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
            "Surge rate +25% to +35%, enforce 2-night min"
            if count == 4
            else "Raise rate +15% to +20%"
        )
        f.write(f"| {d_str} | {count}/4 | {int(count/4*100)}% | {action} |\n")
    else:
      f.write(
          "No dates currently exceed 75% compression in the next 90 days.\n"
      )


if __name__ == "__main__":
  run()
