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
- **Course selection + priority modes** — pick which course(s) to play, then
  choose whether to lock the **course** (time flexible) or lock the **time**
  (location flexible, searches every listed course).
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
`--players 2` · `--course "Fox Hollow,Eagle Ridge"` · `--priority course|time` ·
`--timezone America/New_York` · `--prefire-seconds 90`

### `run`-only flags
`--now` (skip the wait) · `--headful` (show the browser)

---

## Configuration (`config.yaml`)

```yaml
booking:
  desired_date: "Saturday, June 14"      # natural phrasings accepted
  time_window: { start: "07:00", end: "09:00" }
  players: 4
  courses: ["Fox Hollow", "Eagle Ridge"]  # ordered by preference; [] = single-course
  priority: "course"                       # "course" or "time" (see below)
scheduling:
  unlock_days_before: 8                  # window opens N days before play
  unlock_time: "00:00"                   # local time the window opens
  timezone: "America/New_York"           # IANA tz; blank = system local
  prefire_seconds: 60                    # start polling this early
  poll_interval_seconds: 1.5
  selection_budget_seconds: 6            # cap on finding the date/course control
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

### Courses & priority modes

List the courses you'd play under `booking.courses`, most-preferred first, then
pick what to optimise for with `booking.priority`:

| `priority` | What's locked | What's flexible | Behaviour |
| --- | --- | --- | --- |
| `course` | the course (`courses[0]`) | the **time** | Books the best slot at your favourite course — preferring your window, but taking any available time if the window is full. |
| `time` | your **time window** | the **course** | Scans *every* listed course and books the earliest slot inside your window, whichever course it's at. Ties go to the more-preferred course. |

```bash
# Lock onto Fox Hollow, take whatever time you can get:
python main.py run --priority course --course "Fox Hollow"

# I just want a 7–9am slot — book the earliest one at any of these courses:
python main.py run --priority time --course "Fox Hollow,Eagle Ridge,Pine Valley"
```

For single-course platforms, leave `courses: []`; no course is selected and the
tool just books on the one available sheet.

Course selection in the browser tries a course dropdown first, then a matching
tab/link/button by name — add a `course_select` override (below) if your club's
control isn't matched.

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
    course_select: ["select#course-picker"]
    slot_row:     ["div.tee-slot.is-open"]
    book_button:  ["button.reserve"]
    confirm_button: ["button#confirm-reservation"]
```

Supported keys: `login_link`, `email_input`, `password_input`, `login_submit`,
`logged_in_marker`, `booking_nav`, `course_select`, `slot_row`, `book_button`,
`players_select`, `confirm_button`, `booking_closed_marker`.

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
python -m pytest -q          # 32 tests
```

For an end-to-end browser smoke test against a local mock booking page (no real
network, no credentials), once Chromium is installed:

```bash
python tests/smoke_browser.py
```

This launches a real Chromium, scrapes the mock slots, books the earliest one in
the window, and saves a confirmation screenshot — proving the full automation
path (`collect_slots` → `choose_slot` → `book_slot` → `screenshot`) works.

---

## Running it for real: keeping the machine awake

The booker must be running *at the unlock moment*. If the window opens at
midnight, your computer can't be asleep. Keep it awake for the duration:

```bash
# macOS — prevent sleep while the booker runs (Ctrl-C to stop):
caffeinate -s python main.py run

# Linux — disable sleep/idle for the session, or run on an always-on box:
systemd-inhibit --what=idle:sleep python main.py run
```

For a hands-off setup, run it on an always-on machine (a home server, a small
cloud VM, a Raspberry Pi). Point it at a specific browser and disable the
sandbox where needed with the env vars below.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `playwright install` can't download Chromium (locked-down network / proxy) | Install Chromium another way and set `TENFORE_CHROMIUM_PATH=/path/to/chrome`. The booker will launch that binary instead of a managed download. |
| `Running as root without --no-sandbox` / browser won't launch in a container | Set `TENFORE_NO_SANDBOX=1` to launch with `--no-sandbox`. |
| "Could not locate login form fields" / "Could not explicitly select date/course" | The club's DOM doesn't match the default selectors — run with `--headful` to watch, then add overrides under `site.selectors:` (see *Tuning selectors*). |
| Booking page never opens | The window genuinely hasn't unlocked, or `unlock_time`/`timezone` are wrong. Verify with `python main.py info`; widen `retry.booking_window_minutes` if needed. |
| Nothing books though slots exist | Your window may be full and `fallback_to_next_best` is off, or `priority: course` is locked to a course with no openings. Check `logs/tenfore.log`. |
| No desktop notification on macOS | First run prompts for permission; ensure Terminal/your runner is allowed under System Settings → Notifications. The tool still works without it. |

Every run appends timestamped detail to `logs/tenfore.log` — start there.

### Browser environment variables

| Variable | Effect |
| --- | --- |
| `TENFORE_CHROMIUM_PATH` | Absolute path to a Chromium/Chrome binary to launch instead of Playwright's managed one. |
| `TENFORE_NO_SANDBOX=1` | Launch Chromium with `--no-sandbox` (needed as root / in some containers). |

---

## Notes & limitations

- **Credentials** live only in `.env` (chmod 600, gitignored). Nothing is sent
  anywhere except the Tenfore login form.
- **Booking confirmation** is detected heuristically from page text; always
  check the saved screenshot in `screenshots/` and your Tenfore account.
- The live browser flow depends on the actual Tenfore DOM — if a step can't find
  an element, it logs clearly so you can add a selector override (see above).
- macOS notifications are a no-op on other platforms (the tool still runs).
