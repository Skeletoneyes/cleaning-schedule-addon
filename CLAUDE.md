# Cleaning Schedule Tracker — HA Add-on

## Purpose

Home Assistant add-on that tracks Airbnb cleaning schedules. It syncs bookings from an Airbnb iCal feed, lets you assign cleaners to checkout dates, and parses WhatsApp conversations with cleaners using Claude Haiku to detect confirmations and declines.

## Architecture

Single-file Flask app (`cleaning-tracker/app.py`) running as an HA add-on with ingress. No database — data is stored in `/data/data.json` (persists across rebuilds). Configuration is read from `/data/options.json` (populated by HA from `config.yaml` options).

### Key Files

```
repository.yaml              # HA custom repo metadata
cleaning-tracker/
├── config.yaml              # Add-on config: name, version, options schema
├── Dockerfile               # python:3.12-slim, pip install, runs app.py
├── requirements.txt         # flask, requests, icalendar
└── app.py                   # Entire application (Flask routes, templates, logic)
```

### No build.yaml

The Dockerfile hardcodes `python:3.12-slim` directly. The `BUILD_FROM` arg pattern from HA docs did not work — the Supervisor wasn't passing it through, resulting in an empty base image.

## Add-on Options (set in HA UI)

- `ical_url` — Airbnb iCal calendar URL (contains private token, never commit it)
- `anthropic_api_key` — API key for Claude Haiku WhatsApp parsing (stored as password type)
- `cleaners` — List of cleaner names (used for assignment dropdowns)

## How It Works

### iCal Sync
- Fetches Airbnb iCal, extracts `VEVENT` entries with `SUMMARY: Reserved`
- Merges into `data.json`, preserving cleaner assignments across syncs
- Marks bookings as cancelled/complete if they disappear from the feed

### WhatsApp Parsing
- User pastes WhatsApp chat text (any format — the LLM handles parsing)
- App sends chat text + list of upcoming booking checkout dates to Claude Haiku
- Haiku returns structured JSON: which dates are confirmed/declined, cleaner name, time
- Optional "auto-apply" checkbox writes confirmations directly to booking data

### Ingress
All URLs are prefixed with the `X-Ingress-Path` request header so forms and redirects work behind HA's ingress proxy. The `ingress_prefix()` helper is passed to every template as `{{ prefix }}`.

## Deployment

Installed via HA custom repository:
1. Add-ons > Add-on Store > Repositories > paste GitHub URL
2. Install "Cleaning Schedule Tracker"
3. Configure options (iCal URL, API key, cleaners)
4. Start — appears in sidebar as "Cleaning Schedule"

Updates: bump `version` in `config.yaml`, push to GitHub, refresh in HA.

## Important Notes

- `init: false` in config.yaml is required — without it, the HA base image's s6-overlay conflicts with a bare `CMD`
- Do NOT use Samba for iterative add-on development on HAOS from Windows — SMB write caching makes files stale
- The Supervisor caches add-on configs — changing config.yaml for an existing slug requires a version bump
- For local development, the app falls back to reading from the current directory when `/data/options.json` doesn't exist
