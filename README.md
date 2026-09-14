# Harpers Riverside Motel Competitor Intelligence & Pricing Tracker

An automated competitive intelligence pipeline designed for a 4-room boutique motel operating in a 2-property, 8-room local duopoly. 

 This repository tracks competitors rooms daily, capturing forward pricing, booking velocity, historical realized occupancy, and high-demand supply compression dates.

---

## 1. Tracked Listings (Stone Manor Motel)

The system monitors the 4 renovated rooms at Stone Manor via public Airbnb calendar endpoints:

| Room Name | Listing ID | Configuration | Direct Airbnb Link |
| :--- | :--- | :--- | :--- |
| **Room 5** | `1697396664319697096` | Queen Bed | [Stone Manor Room 5 - Queen](https://www.airbnb.com/rooms/1697396664319697096) |
| **Room 6** | `1697399341843035717` | Queen Bed | [Stone Manor Room 6 - Queen](https://www.airbnb.com/rooms/1697399341843035717) |
| **Room 7** | `1695167216674688814` | Queen + Twin Trundle | [Stone Manor Room 7 - Queen w/ Twin trundle](https://www.airbnb.com/rooms/1695167216674688814) |
| **Room 8** | `1679897808235823780` | King (or 2 Twins) | [Stone Manor Room 8 - King (or 2 Twins)](https://www.airbnb.com/rooms/1679897808235823780) |

*Total competitor capacity = 4 room-nights per calendar date.*

---

## 2. Repository Structure

```text
├── .github/
│   └── workflows/
│       └── daily_tracker.yml   # Scheduled cron automation (GitHub Actions)
├── data/
│   ├── daily_snapshots.csv     # Raw time-series database (appended daily)
│   └── latest_summary.md       # Auto-rendered executive brief & alerts
├── tracker.py                  # Core scraping & analytics pipeline
├── requirements.txt            # Python dependencies (requests, pandas)
└── README.md                   # System documentation & operating playbook
