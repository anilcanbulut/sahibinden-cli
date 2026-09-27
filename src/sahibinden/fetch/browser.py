"""Browser launcher used by both the fingerprint test and (later) the fetcher.

Deliberate choices:

* **Real Chrome, not bundled Chromium.** ``channel="chrome"`` uses the system
  install, so the UA, build string and TLS stack are the ones millions of other
  Turkish users present.
* **Persistent context.** Cookies and a stable profile survive between runs; a
  browser that arrives with no history every time is itself a signal.
* **Headful.** ``headless=True`` remains detectable, and the whole point of this
  project is a small number of requests, so there is nothing to gain from it.
* **No ``--disable-blink-features=AutomationControlled`` and friends.** Patchright
  handles the CDP leak properly; piling on flags creates new tells.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .portable import load_session, seed_context

CHROME_PATH = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PROFILE_DIR = PROJECT_ROOT / "browser-profile"

# Measured from the real browser via tools/fingerprint_test.py. Chrome's own
# --accept-lang flag drives both navigator.languages and the Accept-Language
# header natively; Playwright's locale= would set them through a CDP override,
# which is itself observable. Same reasoning for timezone and viewport: the host
# machine already reports the right values, so we override nothing.
ACCEPT_LANGUAGES = "en-US,en,tr"


# 127.0.0.1, not "localhost": Chrome binds the debug port to IPv4 only, while
# localhost resolves to ::1 first on Windows, giving ECONNREFUSED.
CDP_URL = "http://127.0.0.1:9222"


@contextmanager
def connect(cdp_url: str = CDP_URL):
    """Attach to the user's already-running Chrome instead of launching one.

    sahibinden serves a sign-in wall to any cookieless session — verified
    against a plain incognito window on the same residential IP, with and
    without a Google referrer. So the fetcher needs a session that has actually
    accumulated trust, and the only one that exists is the user's own browser.

    Extracting its cookies is not an option on Chrome 127+ (App-Bound
    Encryption exists precisely to stop that), so we drive the live browser.

    Start Chrome with the debugging port open first, all other windows closed:

        & "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222

    We never close the user's browser — only the tab we opened.
    """
    from patchright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:
            raise RuntimeError(
                f"Could not attach to Chrome at {cdp_url}.\n"
                "Close every Chrome window, then start it with:\n"
                '  & "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
                "--remote-debugging-port=9222\n"
                "Verify with:  curl.exe http://127.0.0.1:9222/json/version"
            ) from exc

        context = browser.contexts[0] if browser.contexts else browser.new_context()
        try:
            yield context
        finally:
            # Disconnects the CDP session; leaves the browser running.
            browser.close()


#: Far outside any real display. The browser is fully alive — compositing, GPU,
#: WebGL — it simply has no pixels on screen. Preferred over headless: the
#: fingerprint audit passed partly because WebGL reports the machine's actual
#: RTX 3060 through D3D11, and headless Chrome falls back to SwiftShader, which
#: is one of the loudest automation signals available.
OFFSCREEN_POSITION = "-32000,-32000"


def _no_display() -> bool:
    """True on a host with no display server to draw a window on."""
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _free_display() -> str:
    """A display number nothing is using."""
    taken = {p.name for p in Path("/tmp/.X11-unix").glob("X*")} \
        if Path("/tmp/.X11-unix").exists() else set()
    n = 99
    while f"X{n}" in taken:
        n += 1
    return f":{n}"


@contextmanager
def virtual_display(width: int = 1920, height: int = 1080, depth: int = 24):
    """Run an Xvfb server so Chrome can be headful on a machine with no screen.

    This is the difference between working and not working on a server, and the
    reason is in this module's own header: headless Chrome is detectable, and
    its WebGL falls back to SwiftShader, "one of the loudest automation signals
    available". Falling back to headless because there is no monitor hands the
    site exactly the signal the rest of this file is built to avoid — measured,
    once: a headless fetch from this host drew an instant Cloudflare 403 with a
    cf-mitigated header, where nothing else had changed.

    Xvfb is a real X server that draws into memory. Chrome running against it
    is an ordinary headful Chrome — same compositor, same window, same code
    paths — that happens to have no monitor attached. The window still goes to
    OFFSCREEN_POSITION, which costs nothing and keeps one code path for both.

    Yields the display string, or None if Xvfb is not installed, so the caller
    can decide what to do rather than dying inside a context manager.
    """
    if not shutil.which("Xvfb"):
        yield None
        return

    display = _free_display()
    proc = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", f"{width}x{height}x{depth}", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    socket = Path("/tmp/.X11-unix") / f"X{display[1:]}"
    for _ in range(100):  # Xvfb is quick, but not instant
        if socket.exists():
            break
        if proc.poll() is not None:
            yield None
            return
        time.sleep(0.1)
    else:
        proc.terminate()
        yield None
        return

    previous = os.environ.get("DISPLAY")
    os.environ["DISPLAY"] = display
    try:
        yield display
    finally:
        if previous is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = previous
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@contextmanager
def launch(
    profile_dir: Path | None = None,
    *,
    headless: bool | None = None,
    offscreen: bool = True,
    accept_languages: str | None = None,
    seed: bool = True,
):
    """Yield a Patchright persistent browser context using real Chrome.

    ``headless=None`` means "decide": headful where there is a display, because
    that is what the fingerprint audit was run against, and headless where
    there is not, because on a server the alternative is not starting at all.

    ``seed`` pushes an imported session's cookies into the profile before
    anything is fetched. That is what lets a session signed in on one machine
    drive the fetcher on another.
    """
    from patchright.sync_api import sync_playwright

    profile_dir = Path(profile_dir or DEFAULT_PROFILE_DIR)
    profile_dir.mkdir(parents=True, exist_ok=True)

    stack = ExitStack()
    # Headful is the default everywhere, including on servers, because headless
    # is what this site notices. Where there is no screen we make one: Xvfb is a
    # real X server that draws into memory, so Chrome against it is an ordinary
    # headful Chrome that happens to have no monitor.
    #
    # Only when Xvfb is missing do we go headless, and then we say so. Quietly
    # degrading here is how a working fetch turns into a 403 with no clue why —
    # which is exactly what happened once before this branch existed.
    on_virtual_display = False
    if headless is not True and _no_display():
        if stack.enter_context(virtual_display()) is not None:
            headless = False
            on_virtual_display = True
        elif headless is None:
            headless = True
            print("  no display and no Xvfb — falling back to headless Chrome,\n"
                  "  which this site detects. Install it with:\n"
                  "      sudo apt-get install -y xvfb",
                  file=sys.stderr)
        else:
            stack.close()
            raise RuntimeError(
                "headful Chrome was asked for, but this host has no display and "
                "Xvfb is not installed.\n  sudo apt-get install -y xvfb"
            )
    elif headless is None:
        headless = False

    # An earlier version replayed the imported session's user agent here with
    # --user-agent, on the reasoning that a Windows session should not announce
    # itself as Linux. That was wrong, and it was measured wrong: Chrome derives
    # its Client Hints from its own build rather than from the flag, so the
    # override produced navigator.userAgent "Windows NT 10.0" alongside
    # navigator.platform "Linux x86_64" — a browser contradicting itself inside
    # one request. It earned an immediate Cloudflare 403.
    #
    # The browser's identity is whatever the browser actually is. Only the
    # cookies travel. --accept-lang stays, because it is a real Chrome switch
    # that sets the header and navigator.languages together at source.
    session = load_session() if seed else None
    profile_langs = (session or {}).get("languages")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            executable_path=str(CHROME_PATH) if CHROME_PATH.exists() else None,
            headless=headless,
            # no_viewport keeps screen dimensions and devicePixelRatio as the
            # real display reports them; a forced viewport leaves a float
            # artifact in devicePixelRatio and a screen size no monitor has.
            no_viewport=True,
            args=[
                f"--accept-lang="
                f"{accept_languages or profile_langs or ACCEPT_LANGUAGES}",
                # Only on a machine we gave a fake screen to. Chrome will not
                # start software WebGL on its own without this, and a browser
                # reporting *no WebGL context at all* is stranger than one
                # reporting a software renderer — plenty of real users sit on
                # GPU-less VMs and RDP sessions. On a host with a real GPU this
                # flag is absent, so nothing is claimed that is not true.
                *(["--enable-unsafe-swiftshader"] if on_virtual_display else []),
                *(
                    [f"--window-position={OFFSCREEN_POSITION}", "--window-size=1920,1080"]
                    if offscreen
                    else ["--start-maximized"]
                ),
            ],
        )
        if seed:
            seed_context(context, session, profile_dir=profile_dir.resolve())
        try:
            yield context
        finally:
            context.close()
            # Tears down Xvfb, if launch() started one.
            stack.close()
