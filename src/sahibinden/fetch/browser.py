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
import sys
from contextlib import contextmanager
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
    """True on a host with no display server to draw a window on.

    A headful Chrome on such a host does not quietly fall back to headless — it
    fails to start at all, with an error about the display that says nothing
    about the real problem. Detecting it here turns that into a working run.
    """
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


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
    if headless is None:
        headless = _no_display()

    # An imported session carries the identity of the browser that made it, and
    # replaying it under a different one is a contradiction we put there
    # ourselves. Headless Chrome is the sharp case: it announces
    # "HeadlessChrome/153" in its own user agent, so a Windows session replayed
    # from here would arrive with Windows cookies and a Linux headless badge.
    #
    # --user-agent and --accept-lang are Chrome's own switches, so they set the
    # header and navigator together at source. Playwright's user_agent= would
    # do it through a CDP override instead, which is the observable way to lie
    # about exactly this.
    session = load_session() if seed else None
    profile_ua = (session or {}).get("user_agent")
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
                *([f"--user-agent={profile_ua}"] if profile_ua else []),
                *(
                    [f"--window-position={OFFSCREEN_POSITION}", "--window-size=1920,1080"]
                    if offscreen
                    else ["--start-maximized"]
                ),
            ],
        )
        if seed:
            seed_context(context, session)
        try:
            yield context
        finally:
            context.close()
