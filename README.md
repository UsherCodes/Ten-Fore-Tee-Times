# 🏌️ Tenfore Tee-Time Auto-Booker

A Python CLI that races to book a golf tee time on the
[Tenfore Golf](https://www.fox.tenfore.golf) platform the instant the booking
window opens (typically 8 days before play). It waits out a precise countdown,
then hammers the booking page with Playwright, logs in, finds the earliest slot
in your preferred time window, books it, screenshots the confirmation, and pings
you on the desktop.

> **Use responsibly.** This automates *your own* account against *your own*
> club's booking window. Don't run it faster than necessary, and respect your
> club's terms of service.

---

## Features

- **Interactive setup wizard** — answers a few questions, writes `config.yaml`
  and a gitignored `.env` (credentials never leave your machine).
- **Precision timer** — sleeps until ~60s before the window opens, then enters a
  1–2s rapid polling loop with a live countdown.
- **Playwright (async) automation** — Chromium login → booking page → date
  select → window filter → book earliest slot → screenshot.
- **Resilient** — login retries (10×), keeps retrying a "not open yet" booking
  page, falls back to the next-best slot if your window fills, and handles
  timeouts/network errors gracefully.
- **Notifications** — Rich terminal panels plus optional macOS desktop alerts
  (`osascript`).
- **Config + CLI overrides** — everything lives in `config.yaml`; any run can be
  overridden with `--date`, `--time-window`, `--players`, etc.
- **Timestamped logging** — every attempt is logged to `logs/tenfore.log`.

---

## Install

Requires **Python 3.11+**.

```bash
pip install -r requirements.txt
python -m playwright install chromium      # one-time browser download
```

## Quick start

```bash
# 1. Configure interactively (writes config.yaml + .env)
python main.py setup

# 2. Sanity-check the resolved settings & unlock moment
python main.py info

# 3. Arm it — it waits for the window, then books. Leave it running.
python main.py run
```

To watch the browser work while debugging, add `--headful`. To skip the
countdown and attempt a booking right now (e.g. the window is already open):

```bash
python main.py run --now --headful
```

---

## Commands

| Command | Purpose |
| --- | --- |
| `setup` | Interactive wizard → `config.yaml` + `.env`. |
| `info`  | Print resolved config and the exact next unlock datetime. |
| `run`   | Wait for the window, then log in and book. |

### Global flags
`--config PATH` · `--env PATH` · `-v/--verbose` · `--version`

### `run` / `info` override flags
`--date "Saturday, June 14"` · `--time-window "between 7:00 AM and 9:00 AM"` ·
`--players 2` · `--timezone America/New_York` · `--prefire-seconds 90`

### `run`-only flags
`--now` (skip the wait) · `--headful` (show the browser)

---

## Configuration (`config.yaml`)

```yaml
booking:
  desired_date: "Saturday, June 14"      # natural phrasings accepted
  time_window: { start: "07:00", end: "09:00" }
  players: 4
scheduling:
  unlock_days_before: 8                  # window opens N days before play
  unlock_time: "00:00"                   # local time the window opens
  timezone: "America/New_York"           # IANA tz; blank = system local
  prefire_seconds: 60                    # start polling this early
  poll_interval_seconds: 1.5
retry:
  login_attempts: 10
  login_delay_seconds: 2
  booking_window_minutes: 5              # keep retrying the page this long
  fallback_to_next_best: true
browser: { headless: true, timeout_ms: 30000 }
notifications: { macos_desktop: true }
paths: { log_dir: "logs", screenshot_dir: "screenshots" }
site:
  base_url: "https://www.fox.tenfore.golf"
```

### The booking-unlock calculation

`unlock = desired_date − unlock_days_before days`, at `unlock_time`, in
`timezone`. With the defaults, a **Sun Jun 14** play date unlocks
**Sat Jun 6 at 00:00 America/New_York**, and polling starts at `23:59:00` the
night before. Verify any time with `python main.py info`.

### Tuning selectors

Tenfore's DOM differs between clubs and changes over time, so every step tries
an ordered list of candidate locators (see `DEFAULT_SELECTORS` in
`tenfore/booker.py`). If your club's page isn't matched, override or extend any
list from `config.yaml` **without editing code** — your entries are tried first,
the defaults remain as fallback:

```yaml
site:
  base_url: "https://www.fox.tenfore.golf"
  selectors:
    login_link:   ["a.member-login"]
    slot_row:     ["div.tee-slot.is-open"]
    book_button:  ["button.reserve"]
    confirm_button: ["button#confirm-reservation"]
```

Supported keys: `login_link`, `email_input`, `password_input`, `login_submit`,
`logged_in_marker`, `booking_nav`, `slot_row`, `book_button`, `players_select`,
`confirm_button`, `booking_closed_marker`.

---

## Project structure

```
.
├── main.py                  # argparse CLI entrypoint (setup / run / info)
├── config.yaml              # all settings (CLI flags override)
├── .env.example             # template for TENFORE_EMAIL / TENFORE_PASSWORD
├── requirements.txt
├── tenfore/
│   ├── config.py            # load config + overrides + unlock-time math
│   ├── timeparse.py         # lenient date/time/window parsing (no deps)
│   ├── scheduler.py         # precise wait + live countdown
│   ├── booker.py            # Playwright automation + slot selection
│   ├── runner.py            # orchestrates wait → poll → login → book → notify
│   ├── notifier.py          # Rich + macOS desktop notifications
│   ├── setup_wizard.py      # interactive setup
│   └── logsetup.py          # timestamped file + Rich console logging
└── tests/
    ├── test_core.py         # unit tests for parsing & slot selection
    └── smoke_browser.py     # end-to-end Playwright test vs a local mock page
```

---

## Testing

The dependency-light core logic (date/time parsing, slot selection, unlock math)
is unit-tested without needing a browser:

```bash
python -m pytest -q          # 24 tests
```

For an end-to-end browser smoke test against a local mock booking page (no real
network, no credentials), once Chromium is installed:

```bash
python tests/smoke_browser.py
```

---

## Notes & limitations

- **Credentials** live only in `.env` (chmod 600, gitignored). Nothing is sent
  anywhere except the Tenfore login form.
- **Booking confirmation** is detected heuristically from page text; always
  check the saved screenshot in `screenshots/` and your Tenfore account.
- The live browser flow depends on the actual Tenfore DOM — if a step can't find
  an element, it logs clearly so you can add a selector override (see above).
- macOS notifications are a no-op on other platforms (the tool still runs).
