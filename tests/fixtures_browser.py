"""Browser fixture tests for the riskiest booker paths — no real network.

Serves local HTML fixtures over a loopback HTTP server (so cookies and
storage_state behave like a real site) and drives the real TenforeBooker
through:

  1. one-step login (email + password on one screen)
  2. two-step login (email -> Next -> password), ASP.NET-style capitalized IDs
  3. session persistence round-trip (storage_state: second run skips login)
  4. select_course: <select> dropdown variant and tab variant
  5. select_date: <input type=date> variant and day-cell variant
  6. open_booking_page: rejects an error page (no booking content)

Run (after `python -m playwright install chromium`):
    python tests/fixtures_browser.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import http.server
import json
import sys
import tempfile
import threading
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console  # noqa: E402

from tenfore.booker import TenforeBooker  # noqa: E402
from tenfore.config import BookingConfig, Credentials  # noqa: E402
from tenfore.logsetup import setup_logging  # noqa: E402

ONE_STEP = """<!doctype html><html><body>
<h1>Members</h1>
<input type="email" id="email"><input type="password" id="pw">
<button type="submit" onclick="login()">Login</button>
<script>function login(){
  if(document.getElementById('email').value && document.getElementById('pw').value){
    document.cookie='tenfore_auth=1; path=/';
    document.body.innerHTML='<a href="#" id="lo">Logout</a><p>Welcome!</p>';
  }}</script></body></html>"""

# ASP.NET-style: capitalized control IDs, text (not email) input, Next button.
TWO_STEP = """<!doctype html><html><body>
<div id="step1">
  <input type="text" id="ctl00_Main_UserName" placeholder="Email">
  <button onclick="next1()">Next</button>
</div>
<div id="step2" style="display:none">
  <input type="password" id="ctl00_Main_Password">
  <button onclick="login()">Sign in</button>
</div>
<script>
function next1(){
  if(document.getElementById('ctl00_Main_UserName').value){
    document.getElementById('step1').style.display='none';
    document.getElementById('step2').style.display='block';
  }}
function login(){
  if(document.getElementById('ctl00_Main_Password').value){
    document.cookie='tenfore_auth=1; path=/';
    document.body.innerHTML='<a href="#" id="lo">Logout</a><p>Welcome!</p>';
  }}</script></body></html>"""

# Shows Logout only when the auth cookie is present (for the session round-trip).
COOKIE_GATED = """<!doctype html><html><body><div id="root">
<input type="email" id="email"><input type="password" id="pw">
<button type="submit" onclick="login()">Login</button></div>
<script>
if(document.cookie.includes('tenfore_auth=1')){
  document.getElementById('root').innerHTML='<a href="#" id="lo">Logout</a>';
}
function login(){
  if(document.getElementById('email').value && document.getElementById('pw').value){
    document.cookie='tenfore_auth=1; path=/';
    document.getElementById('root').innerHTML='<a href="#" id="lo">Logout</a>';
  }}</script></body></html>"""

COURSE_DROPDOWN = """<!doctype html><html><body>
<select name="course"><option>Fox Hollow</option><option>Eagle Ridge</option></select>
<div class="teetime"><span>07:20 AM</span><button>Book</button></div>
</body></html>"""

COURSE_TABS = """<!doctype html><html><body>
<a href="#" role="tab">Fox Hollow</a><a href="#" role="tab">Eagle Ridge</a>
<div class="teetime"><span>07:20 AM</span><button>Book</button></div>
</body></html>"""

DATE_INPUT = """<!doctype html><html><body>
<input type="date" id="d">
<div class="teetime"><span>07:20 AM</span><button>Book</button></div>
</body></html>"""

DATE_CELLS = """<!doctype html><html><body>
<table><tr><td data-date="{iso}"><a href="#">{day}</a></td></tr></table>
<div class="teetime"><span>07:20 AM</span><button>Book</button></div>
</body></html>"""

ERROR_PAGE = """<!doctype html><html><body>
<h1>404 — Not Found</h1><p>The page you requested does not exist.</p>
</body></html>"""


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve(directory: Path) -> tuple[http.server.ThreadingHTTPServer, str]:
    handler = partial(_QuietHandler, directory=str(directory))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _config(tmp: Path, base_url: str, **kw) -> BookingConfig:
    defaults = dict(
        desired_date=dt.date(2026, 7, 11),
        window_start=dt.time(7, 0), window_end=dt.time(9, 0), players=2,
        courses=[], priority="time",
        unlock_days_before=8, unlock_time=dt.time(0, 0), timezone=ZoneInfo("UTC"),
        prefire_seconds=60, poll_interval_seconds=0.2, selection_budget_seconds=3,
        login_attempts=2, login_window_seconds=30, login_delay_seconds=0.2,
        booking_window_minutes=0.1, fallback_to_next_best=True,
        headless=True, timeout_ms=8000, macos_desktop=False,
        log_dir=tmp / "logs", screenshot_dir=tmp / "shots",
        base_url=base_url, session_file=tmp / "session.json", raw={},
    )
    defaults.update(kw)
    return BookingConfig(**defaults)


async def scenario(name: str, coro) -> bool:
    console = Console()
    try:
        await coro
        console.print(f"[green]✓ {name}[/green]")
        return True
    except AssertionError as exc:
        console.print(f"[bold red]✗ {name}: {exc}[/bold red]")
        return False


async def run_all() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="tenfore-fixtures-"))
    setup_logging(tmp / "logs", Console(quiet=True))
    web = tmp / "web"
    web.mkdir()
    iso = "2026-07-11"
    (web / "onestep.html").write_text(ONE_STEP)
    (web / "twostep.html").write_text(TWO_STEP)
    (web / "gated.html").write_text(COOKIE_GATED)
    (web / "dropdown.html").write_text(COURSE_DROPDOWN)
    (web / "tabs.html").write_text(COURSE_TABS)
    (web / "dateinput.html").write_text(DATE_INPUT)
    (web / "datecells.html").write_text(DATE_CELLS.format(iso=iso, day="11"))
    (web / "error.html").write_text(ERROR_PAGE)
    srv, base = serve(web)

    creds = Credentials("golfer@example.com", "s3cret")
    results = []

    async def one_step_login():
        b = TenforeBooker(_config(tmp, f"{base}/onestep.html",
                                  session_file=tmp / "s1.json"), creds)
        try:
            await b.start()
            assert await b.login(), "one-step login should succeed"
        finally:
            await b.close()

    async def two_step_login():
        b = TenforeBooker(_config(tmp, f"{base}/twostep.html",
                                  session_file=tmp / "s2.json"), creds)
        try:
            await b.start()
            assert await b.login(), "two-step login should succeed"
        finally:
            await b.close()

    async def session_roundtrip():
        sf = tmp / "s3.json"
        b1 = TenforeBooker(_config(tmp, f"{base}/gated.html", session_file=sf), creds)
        try:
            await b1.start()
            assert await b1.login(), "first login should succeed"
        finally:
            await b1.close()
        assert sf.exists(), "session file should be saved on close"
        state = json.loads(sf.read_text())
        assert any(c.get("name") == "tenfore_auth" for c in state.get("cookies", [])), \
            "auth cookie should be persisted"
        # Second run: must be recognized WITHOUT filling the form.
        b2 = TenforeBooker(_config(tmp, f"{base}/gated.html", session_file=sf), creds)
        try:
            await b2.start()
            await b2._goto(b2.config.base_url)
            marker = await b2._first_visible("logged_in_marker", timeout=3000)
            assert marker, "persisted session should show logged-in marker"
        finally:
            await b2.close()

    async def course_variants():
        for page, label in (("dropdown.html", "dropdown"), ("tabs.html", "tab")):
            b = TenforeBooker(_config(tmp, f"{base}/{page}",
                                      courses=["Fox Hollow", "Eagle Ridge"],
                                      session_file=tmp / f"c-{label}.json"), creds)
            try:
                await b.start()
                await b._goto(b.config.base_url)
                assert await b.select_course("Eagle Ridge"), f"{label} select failed"
            finally:
                await b.close()

    async def date_variants():
        for page, label in (("dateinput.html", "date input"), ("datecells.html", "day cell")):
            b = TenforeBooker(_config(tmp, f"{base}/{page}",
                                      session_file=tmp / f"d-{label[:4]}.json"), creds)
            try:
                await b.start()
                await b._goto(b.config.base_url)
                assert await b.select_date(), f"{label} select failed"
            finally:
                await b.close()

    async def error_page_rejected():
        b = TenforeBooker(_config(tmp, f"{base}/error.html",
                                  session_file=tmp / "e.json"), creds)
        try:
            await b.start()
            await b._goto(b.config.base_url)
            assert not await b._looks_like_booking_page(), \
                "error page must NOT look like a booking page"
            assert not await b.open_booking_page(minutes=0.05), \
                "open_booking_page must not succeed on an error page"
            # sanity: a real slots page IS accepted
            await b._goto(f"{base}/dropdown.html")
            assert await b._looks_like_booking_page(), \
                "slots page should look like a booking page"
        finally:
            await b.close()

    results.append(await scenario("one-step login", one_step_login()))
    results.append(await scenario("two-step login (ASP.NET-style)", two_step_login()))
    results.append(await scenario("session persistence round-trip", session_roundtrip()))
    results.append(await scenario("course select: dropdown + tab", course_variants()))
    results.append(await scenario("date select: input + day cell", date_variants()))
    results.append(await scenario("error page rejected as booking page", error_page_rejected()))

    srv.shutdown()
    console = Console()
    if all(results):
        console.print(f"[bold green]FIXTURE TESTS PASSED[/bold green] ({len(results)}/{len(results)})")
        return 0
    console.print(f"[bold red]FIXTURE TESTS FAILED[/bold red] "
                  f"({sum(results)}/{len(results)} passed)")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_all()))
