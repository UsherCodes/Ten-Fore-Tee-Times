# 🏌️ Mac Runbook — from zero to a booked tee time

The exact sequence for your machine. Everything here has been verified in a
fresh-clone simulation and an end-to-end CLI dry run; the only step that can't
be pre-verified is the live TenFore login, which is Step 4 — and it tells you
exactly what to report if something misses.

Your project folder in these examples:
`~/Desktop/Git Test/Ten-Fore-Scheduler/Ten-Fore-Tee-Times`

---

## Step 1 — Get the latest code

```bash
cd "$HOME/Desktop/Git Test/Ten-Fore-Scheduler/Ten-Fore-Tee-Times"
git pull origin main
```

## Step 2 — Install (one time)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m playwright install chromium      # ~150 MB, one time
```

If `python3` is missing: install from https://python.org or `brew install python`.
Re-run `source .venv/bin/activate` in every new Terminal window before using
the tool.

## Step 3 — Configure

```bash
python3 main.py setup
```

Answers to have ready: play date, time window, number of players, course
name(s), course-vs-time priority, when the booking window unlocks (days before
+ time of day), timezone, and your TenFore email/password (stored only in a
local `.env`, never committed).

Then confirm the plan — especially the **Unlocks** line:

```bash
python3 main.py info
```

## Step 4 — Verify against the real site (the important one)

```bash
python3 main.py check --headful
```

A Chromium window opens so you can watch. This logs in and inspects the tee
sheet but **books nothing**. You'll get a pass/fail table:

| If you see | It means | Do |
|---|---|---|
| All rows ✓ | Everything works — you're deployment-verified | Go to Step 5 |
| Login ✗ | Credentials or login-form selectors | Double-check `.env`; if creds are right, screenshot the login page and send it back |
| Booking page ✗ | Site navigation not matched | Screenshot what the browser shows after login |
| Slots ✗ "window may not be open yet" | Normal before the unlock day | Not a failure — retry targeting a date whose window is open |
| Slots ✗ "check the 'slot_row' selector" | Tee-sheet rows not recognized | Screenshot the tee sheet page |

Selector fixes go in `config.yaml` under `site.selectors:` (see README
"Tuning selectors") — no code changes needed.

Optional full rehearsal (does everything except the final confirm click):

```bash
python3 main.py run --now --dry-run --headful
```

## Step 5 — Arm it for the real booking

The booker must be running at the unlock moment, so keep the Mac awake:

```bash
caffeinate -s python3 main.py run
```

Leave the Terminal open. It counts down, wakes ~60s early, logs in (or resumes
the saved session from Step 4 — faster), and races to book the earliest slot
in your window the instant the sheet opens. On success: terminal confirmation,
a screenshot in `screenshots/`, and a macOS notification.

**Always verify the reservation in your TenFore account afterward.**

## Cheat sheet

```bash
python3 main.py info                        # what will it do, and when
python3 main.py check --headful             # no-booking validation, watchable
python3 main.py run --now --dry-run         # rehearsal, never confirms
caffeinate -s python3 main.py run           # the real thing
python3 -m pytest -q && python3 tests/smoke_browser.py && python3 tests/fixtures_browser.py
                                            # full local test battery
```

## If something goes wrong

1. `logs/tenfore.log` has a timestamped record of every attempt — start there.
2. The README's **Troubleshooting** table covers the common failures.
3. Send back the relevant log lines and/or a screenshot; selector overrides
   in `config.yaml` fix most site-matching issues without touching code.
