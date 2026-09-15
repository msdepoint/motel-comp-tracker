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

# ----------------------------------------------------------------------
# Configuration: Full Cranberry Lake 8-Room Lodging Market
# ----------------------------------------------------------------------
PROPERTIES = {
    "Harper's Riverside Motel": {
        "1242805963234197434": "Room 1 (3 beds)",
        "1242816231631291331": "Room 2 (2 beds)",
        "1242818612232544628": "Room 3",
        "1242820154511679861": "Room 4 (1 bed)",
    },
    "Harpers Riverside Motel Comp": {
        "1697396664319697096": "Room 5 (Queen)",
        "1697399341843035717": "Room 6 (Queen)",
        "1695167216674688814": "Room 7 (Queen/Trundle)",
        "1679897808235823780": "Room 8 (King/Twins)",
    },
}

# Competitor Seasonal Closure Parameters
COMP_CLOSURE_START = date(2026, 10, 7)
COMP_CLOSURE_END = date(2027, 5, 20)

DATA_DIR = "data"
CSV_FILE = os.path.join(DATA_DIR, "daily_snapshots.csv")
SUMMARY_FILE = os.path.join(DATA_DIR, "latest_summary.md")
FORWARD_DAYS = 90


def is_comp_seasonally_closed(stay_dt: date) -> bool:
  """Returns True if the target date falls within the competitor's seasonal shutdown."""
  return COMP_CLOSURE_START <= stay_dt <= COMP_CLOSURE_END


def parse_date_str(raw: str) -> Optional[str]:
  """Normalizes calendar date attributes to YYYY-MM-DD."""
  clean = raw.replace("calendar-day-", "").strip()
  for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%m_%d_%Y"):
    try:
      return datetime.strptime(clean, fmt).strftime("%Y-%m-%d")
    except ValueError:
      continue
  return None


def get_guaranteed_open_dates(today: date) -> tuple:
  """Calculates open midweek dates (next Tuesday-Thursday) to sample live room rates."""
  days_ahead = (1 - today.weekday()) % 7
  if days_ahead <= 1:
    days_ahead += 7
  check_in = today + timedelta(days=days_ahead)
  check_out = check_in + timedelta(days=2)  # 2-night baseline
  return check_in.strftime("%Y-%m-%d"), check_out.strftime("%Y-%m-%d")


def extract_true_nightly_rate(page) -> Optional[float]:
  """Extracts the true per-night room rate, ignoring multi-night totals and cleaning fees."""
  # 1. Primary price header (e.g., "$125 / night")
  header_selectors = [
      'span[data-testid*="price"]',
      'div[data-section-id="BOOK_IT_SIDEBAR"] span:has-text("$")',
      'span:has-text("/ night")',
      'div:has-text("night")',
  ]

  for sel in header_selectors:
    try:
      for el in page.query_selector_all(sel):
        text = el.inner_text()
        m = re.search(r"\$([0-9,]+)\s*(?:/|\s*per)?\s*night", text, re.I)
        if m:
          val = float(m.group(1).replace(",", ""))
          if 60 <= val <= 600:
            return val
    except Exception:
      pass

  # 2. Price breakdown line item formula (e.g., "$125 x 2 nights")
  try:
    breakdown_elements = page.query_selector_all(
        'div[data-section-id="BOOK_IT_SIDEBAR"] div, div:has-text("nights")'
    )
    for el in breakdown_elements:
      text = el.inner_text()
      m = re.search(r"\$([0-9,]+)\s*x\s*(\d+)\s*nights?", text, re.I)
      if m:
        rate = float(m.group(1).replace(",", ""))
        if 60 <= rate <= 600:
          return rate

      m_sub = re.search(r"(\d+)\s*nights?.*?\$([0-9,]+)", text, re.I | re.S)
      if m_sub:
        nights = float(m_sub.group(1))
        total = float(m_sub.group(2).replace(",", ""))
        if nights > 0 and 60 <= (total / nights) <= 600:
          return round(total / nights, 2)
  except Exception:
    pass

  return None


def scrape_listing(
    browser,
    property_name: str,
    listing_id: str,
    room_name: str,
    today_str: str,
    cutoff_date: date,
    check_in_sample: str,
    check_out_sample: str,
    max_retries: int = 2,
) -> List[Dict[str, Any]]:
  """Scrapes room availability and verified pricing using an isolated page context."""
  url = (
      f"https://www.airbnb.com/rooms/{listing_id}?check_in={check_in_sample}&check_out={check_out_sample}&guests=1&adults=1"
  )

  for attempt in range(1, max_retries + 1):
    print(
        f"  -> [{property_name}] {room_name} ({listing_id}) [Attempt"
        f" {attempt}]..."
    )
    captured_data: Dict[str, Dict[str, Any]] = {}

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

      nightly_rate = extract_true_nightly_rate(page)

      # Dismiss overlays
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

      # Scroll calendar into view
      page.evaluate("window.scrollBy(0, 1100)")
      page.wait_for_timeout(3000)

      # Extract dates across 3 month clicks (90-day window)
      for _ in range(3):
        day_buttons = page.query_selector_all('[data-testid*="calendar-day-"]')

        for btn in day_buttons:
          test_id = btn.get_attribute("data-testid") or ""
          stay_dt = parse_date_str(test_id)
          if not stay_dt:
            continue

          aria_label = (btn.get_attribute("aria-label") or "").lower()
          data_blocked = btn.get_attribute("data-is-day-blocked")
          is_html_disabled = btn.is_disabled()

          # Availability Determination
          is_avail = 1
          if (
              is_html_disabled
              or data_blocked in (True, "true", "1")
              or "unavailable" in aria_label
              or "not available" in aria_label
              or "past" in aria_label
          ):
            is_avail = 0

          # Assign rate if available; leave None if blocked/closed
          price_val = nightly_rate if is_avail == 1 else None

          # Cell-level rate override if specifically rendered
          cell_text = btn.inner_text()
          m_cell = re.search(r"\$([0-9,]+)", aria_label) or re.search(
              r"\$([0-9,]+)", cell_text
          )
          if m_cell and is_avail == 1:
            try:
              price_val = float(m_cell.group(1).replace(",", ""))
            except Exception:
              pass

          if stay_dt not in captured_data:
            captured_data[stay_dt] = {
                "snapshot_date": today_str,
                "stay_date": stay_dt,
                "property": property_name,
                "listing_id": listing_id,
                "room_name": room_name,
                "is_available": is_avail,
                "price_usd": price_val,
                "min_nights": 1,
            }

        # Advance to next month
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
      print(f"     Timeout on attempt {attempt} for {room_name}.")
    except Exception as err:
      print(f"     Error on attempt {attempt} for {room_name}: {err}")
    finally:
      context.close()

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
      booked_cnt = sum(1 for r in filtered if r["is_available"] == 0)
      avail_cnt = sum(1 for r in filtered if r["is_available"] == 1)
      print(
          f"     Success: {len(filtered)} dates ({booked_cnt} booked,"
          f" {avail_cnt} open, Rate: ${nightly_rate or 0.0}/night)."
      )
      return filtered

    time.sleep(4)

  return []


# ----------------------------------------------------------------------
# Pipeline Entry Point & Reporting
# ----------------------------------------------------------------------
def run():
  os.makedirs(DATA_DIR, exist_ok=True)
  today = date.today()
  today_str = today.strftime("%Y-%m-%d")
  cutoff = today + timedelta(days=FORWARD_DAYS)

  sample_in, sample_out = get_guaranteed_open_dates(today)
  print(
      f"Initiating Cranberry Lake 8-Room Duopoly Sweep for {today_str} (rate"
      f" anchor: {sample_in} to {sample_out})...\n"
  )

  all_rows = []

  with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    )

    for prop_name, rooms in PROPERTIES.items():
      print(f"=== Property: {prop_name} ===")
      for listing_id, room_name in rooms.items():
        rows = scrape_listing(
            browser,
            prop_name,
            listing_id,
            room_name,
            today_str,
            cutoff,
            sample_in,
            sample_out,
        )
        all_rows.extend(rows)
        time.sleep(3)
      print("")

    browser.close()

  if not all_rows:
    print("\n[FATAL] No records extracted.", file=sys.stderr)
    sys.exit(1)

  # Check room coverage
  by_prop = {}
  for r in all_rows:
    by_prop.setdefault(r["property"], set()).add(r["room_name"])

  print("=== Sweep Coverage Verification ===")
  for p_name, r_set in by_prop.items():
    print(f"  - {p_name}: {len(r_set)}/4 rooms captured ({r_set})")

  # Write to CSV
  fieldnames = [
      "snapshot_date",
      "stay_date",
      "property",
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

  update_market_summary(all_rows, today_str)


def update_market_summary(rows: List[Dict[str, Any]], today_str: str):
  """Computes Town Occupancy with seasonal adjustment, Price Index, and Compression Dates."""
  today = datetime.strptime(today_str, "%Y-%m-%d").date()

  dates: Dict[str, Dict[str, Any]] = {}
  for r in rows:
    sd = r["stay_date"]
    prop = r["property"]
    if sd not in dates:
      dates[sd] = {
          "harpers_total": 0,
          "harpers_avail": 0,
          "harpers_prices": [],
          "comp_total": 0,
          "comp_avail": 0,
          "comp_prices": [],
      }

    if prop == "Harper's Riverside Motel":
      dates[sd]["harpers_total"] += 1
      if r["is_available"] == 1:
        dates[sd]["harpers_avail"] += 1
      if r["price_usd"]:
        dates[sd]["harpers_prices"].append(r["price_usd"])
    else:
      dates[sd]["comp_total"] += 1
      if r["is_available"] == 1:
        dates[sd]["comp_avail"] += 1
      if r["price_usd"]:
        dates[sd]["comp_prices"].append(r["price_usd"])

  # Sentinel: Detect if competitor unexpectedly unblocked dates during winter closure
  unscheduled_openings = []
  for d_str, v in dates.items():
    d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
    if is_comp_seasonally_closed(d_obj) and v["comp_avail"] > 0:
      unscheduled_openings.append((d_str, v["comp_avail"]))

  def calc_metrics(days_forward):
    target_end = today + timedelta(days=days_forward)
    h_booked = 0
    h_cap = 0
    c_booked = 0
    c_cap = 0
    h_prices = []
    c_prices = []

    for d_str, v in dates.items():
      d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
      if today <= d_obj <= target_end:
        # Harper's always active
        h_cap += 4
        h_booked += max(0, 4 - v["harpers_avail"])
        h_prices.extend(v["harpers_prices"])

        # Competitor: Active capacity drops to 0 during seasonal closure
        if not is_comp_seasonally_closed(d_obj):
          c_cap += 4
          c_booked += max(0, 4 - v["comp_avail"])
          c_prices.extend(v["comp_prices"])

    h_occ = round((h_booked / h_cap * 100), 1) if h_cap > 0 else 0.0
    c_occ = round((c_booked / c_cap * 100), 1) if c_cap > 0 else 0.0
    total_cap = h_cap + c_cap
    total_booked = h_booked + c_booked
    town_occ = (
        round((total_booked / total_cap * 100), 1) if total_cap > 0 else 0.0
    )

    h_adr = round(sum(h_prices) / len(h_prices), 2) if h_prices else 0.0
    c_adr = round(sum(c_prices) / len(c_prices), 2) if c_prices else 0.0
    cpi = round(h_adr / c_adr, 2) if c_adr > 0 else 1.0

    return {
        "town_occ": town_occ,
        "h_occ": h_occ,
        "c_occ": c_occ,
        "h_adr": h_adr,
        "c_adr": c_adr,
        "cpi": cpi,
        "c_cap_active": c_cap > 0,
    }

  m_7d = calc_metrics(7)
  m_30d = calc_metrics(30)
  m_60d = calc_metrics(60)

  # Supply Compression Logic (Dual Mode: Duopoly vs. Monopoly)
  compression_dates = []
  for d_str in sorted(dates.keys()):
    d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
    v = dates[d_str]
    h_b = 4 - v["harpers_avail"]
    h_rem = v["harpers_avail"]

    if is_comp_seasonally_closed(d_obj):
      # Monopoly Mode: Capacity = 4. Compression triggers when Harper's has >= 3 rooms booked
      if h_b >= 3:
        compression_dates.append({
            "date": d_str,
            "mode": "Monopoly",
            "town_status": f"{h_b}/4 booked",
            "harpers_status": f"{h_b}/4 booked",
            "comp_status": "Seasonally Closed",
            "action": (
                "100% Sold out"
                if h_rem == 0
                else f"Harper's has only {h_rem} room left! Surge rate +30%,"
                " 2-night min"
            ),
        })
    else:
      # Duopoly Mode: Capacity = 8. Compression triggers when >= 6 rooms booked town-wide
      c_b = 4 - v["comp_avail"]
      c_rem = v["comp_avail"]
      tot_b = h_b + c_b
      if tot_b >= 6:
        if h_rem == 0:
          action = "Harper's 100% sold out"
        elif c_rem == 0:
          action = (
              f"Comp is 100% full! Surge remaining {h_rem} room(s) at Harper's"
              " +30%"
          )
        else:
          action = (
              f"Market is {tot_b}/8 full ({tot_b/8*100:.0f}%). Surge +20%,"
              " 2-night min"
          )

        compression_dates.append({
            "date": d_str,
            "mode": "Duopoly",
            "town_status": f"{tot_b}/8 booked",
            "harpers_status": f"{h_b}/4 booked",
            "comp_status": f"{c_b}/4 booked",
            "action": action,
        })

  # Generate Markdown Summary
  with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
    f.write("# Cranberry Lake Lodging Market Dashboard\n\n")
    f.write(f"**Last Data Refresh**: `{today_str}`\n\n")

    # Sentinel Alert Banner
    if unscheduled_openings:
      f.write(
          "### 🚨 SENTINEL ALERT: Unscheduled Competitor Openings Detected!\n"
      )
      f.write(
          "> The competitor has unblocked room(s) during their scheduled winter"
          " closure window (Oct 7 - May 20):\n\n"
      )
      for d_str, avail in unscheduled_openings[:10]:
        f.write(f"- **{d_str}**: {avail} room(s) listed as open\n")
      f.write("\n---\n\n")

    f.write("## 1. Market Occupancy Pacing\n\n")
    f.write(
        "| Timeframe | Active Town Occupancy | Harper's Riverside |"
        " Competitor Comp |\n"
    )
    f.write("| :--- | :--- | :--- | :--- |\n")
    f.write(
        f"| **Next 7 Days** | **{m_7d['town_occ']}%** | {m_7d['h_occ']}% |"
        f" {m_7d['c_occ']}% |\n"
    )

    comp_30_str = (
        f"{m_30d['c_occ']}%"
        if m_30d["c_cap_active"]
        else "Seasonally Closed (0% cap)"
    )
    comp_60_str = (
        f"{m_60d['c_occ']}%"
        if m_60d["c_cap_active"]
        else "Seasonally Closed (0% cap)"
    )

    f.write(
        f"| **Next 30 Days** | **{m_30d['town_occ']}%** | {m_30d['h_occ']}% |"
        f" {comp_30_str} |\n"
    )
    f.write(
        f"| **Next 60 Days** | **{m_60d['town_occ']}%** | {m_60d['h_occ']}% |"
        f" {comp_60_str} |\n\n"
    )

    f.write("## 2. Competitive Pricing Benchmark (Active Duopoly Windows)\n\n")
    f.write(
        f"- **Harper's Riverside Average Nightly Rate**:"
        f" **${m_30d['h_adr']}**\n"
    )
    if m_30d["c_cap_active"]:
      f.write(
          f"- **Competitor Comp Average Nightly Rate**:"
          f" **${m_30d['c_adr']}**\n"
      )
      f.write(
          f"- **Competitive Price Index (CPI)**: **{m_30d['cpi']}** "
          f"({'Harper’s is positioned at a premium' if m_30d['cpi'] > 1.0 else 'Harper’s is priced at parity or discount'})\n\n"
      )
    else:
      f.write(
          "- **Competitor Status**: Closed for season (Oct 7, 2026 - May 20,"
          " 2027). Harper's holds 100% town pricing power.\n\n"
      )

    f.write("## 3. High Market Compression Dates (>= 75% Full)\n\n")
    f.write(
        "Dates where remaining inventory is constrained. In winter/fall (during"
        " competitor closure), you hold 100% of town capacity:\n\n"
    )

    if compression_dates:
      f.write(
          "| Stay Date | Market Mode | Town Status | Harper's Booked | Comp"
          " Status | Tactical Recommendation |\n"
      )
      f.write(
          "| :--- | :--- | :--- | :--- | :--- | :--- |\n"
      )
      for item in compression_dates[:25]:
        f.write(
            f"| {item['date']} | {item['mode']} | {item['town_status']} |"
            f" {item['harpers_status']} | {item['comp_status']} |"
            f" {item['action']} |\n"
        )
      f.write("\n")
    else:
      f.write(
          "No dates currently exceed the 75% market compression threshold in"
          " the next 90 days.\n"
      )


if __name__ == "__main__":
  run()
