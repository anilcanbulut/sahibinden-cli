"""Moving a signed-in session between machines.

The browser profile itself is not portable. Chrome encrypts its cookie store
with OS-held keys — App-Bound Encryption on Windows, the Keychain on macOS,
the login keyring on Linux — so a copied ``browser-profile/`` decrypts to
nothing on another machine, and across operating systems it does not even
load. That is the whole reason ``browser.py`` drives a live browser instead of
reading its cookie file.

What *is* portable is the cookie set itself, read out of a live context that
has already decrypted it. ``context.cookies()`` hands back plaintext name,
value, domain, path and expiry; ``context.add_cookies()`` takes the same shape
straight back. So the transportable artefact is a small JSON file, and it
costs nothing to produce: reading cookies is a local operation against a
browser that is already open, with no navigation and no request.

The flow this enables:

    # on the machine where you are actually signed in
    sahibinden-cli export-session --attach --out session.json
    scp session.json user@host:~/sahibinden-cli/data/

    # on the headless host
    sahibinden-cli import-session data/session.json
    sahibinden-cli search --brand Toyota --model Corolla ...

The user agent travels with the cookies. A session minted by Chrome on Windows
and replayed by Chromium on Linux otherwise announces a different browser than
the one that logged in, which is a gratuitous inconsistency in a request that
is already unusual for carrying a Turkish session over a foreign address.

What this does **not** fix is the address itself. Cookies are not bound to an
IP on most sites, but "most" is not "this one", and it is cheap to find out:
``import-session`` changes nothing on its own, and the first ``search`` after
it either comes back with listings or comes back walled. One request answers
the question.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import config

#: Only cookies for the site itself travel. A browser profile accumulates
#: cookies for every site it has ever seen, and none of the rest are ours to
#: move between machines.
COOKIE_DOMAIN_MARKER = "sahibinden"

#: Where an imported session lands by default, so the fetcher can find it
#: without being told.
DEFAULT_SESSION_PATH = Path(config.db_path).parent / "session.json"

#: Cookie fields Playwright round-trips between cookies() and add_cookies().
#: Anything else the browser hands back is dropped rather than passed through,
#: because add_cookies() rejects unknown keys outright.
COOKIE_FIELDS = ("name", "value", "domain", "path", "expires",
                 "httpOnly", "secure", "sameSite")


def _clean(cookie: dict) -> dict:
    """Keep only what add_cookies() accepts, in the shape it wants."""
    out = {k: cookie[k] for k in COOKIE_FIELDS if k in cookie}
    # Playwright uses -1 for "session cookie"; it round-trips, but a float
    # expiry from CDP needs to stay a number rather than becoming a string.
    if "expires" in out and out["expires"] is not None:
        out["expires"] = float(out["expires"])
    return out


def summarise(session: dict) -> dict:
    """Describe a session file without opening a browser or spending a request."""
    cookies = session.get("cookies", [])
    now = time.time()
    live = [c for c in cookies
            if not c.get("expires") or c["expires"] < 0 or c["expires"] > now]
    expired = len(cookies) - len(live)

    dated = [c["expires"] for c in cookies
             if c.get("expires") and c["expires"] > now]
    soonest = min(dated) if dated else None

    return {
        "cookies": len(cookies),
        "live": len(live),
        "expired": expired,
        "domains": sorted({c.get("domain", "") for c in cookies}),
        "exported_at": session.get("exported_at"),
        "user_agent": session.get("user_agent"),
        "expires_soonest": (
            datetime.fromtimestamp(soonest, timezone.utc).isoformat(timespec="seconds")
            if soonest else None
        ),
    }


def export_session(out_path: str | Path, *, attach: bool = False,
                   log=print) -> dict:
    """Read the signed-in browser's sahibinden cookies into a portable file.

    Run this on the machine that is actually logged in. It opens no page and
    fetches nothing — the cookies are already in the browser, so this costs
    zero requests and cannot trip the breaker.
    """
    from .browser import connect, launch

    out_path = Path(out_path)
    opener = connect() if attach else launch(offscreen=True)

    with opener as ctx:
        page = ctx.new_page() if attach else (ctx.pages[0] if ctx.pages else ctx.new_page())
        try:
            # about:blank — identity of the browser, not a request to the site.
            user_agent = page.evaluate("() => navigator.userAgent")
            languages = page.evaluate("() => (navigator.languages || []).join(',')")
        except Exception:
            user_agent, languages = None, None
        finally:
            if attach:
                try:
                    page.close()
                except Exception:
                    pass

        cookies = [_clean(c) for c in ctx.cookies()
                   if COOKIE_DOMAIN_MARKER in (c.get("domain") or "").lower()]

    session = {
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "user_agent": user_agent,
        "languages": languages,
        "cookies": cookies,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(session, indent=2), encoding="utf-8")

    info = summarise(session)
    log(f"  {info['cookies']} sahibinden cookies "
        f"({info['live']} live, {info['expired']} already expired)")
    log(f"  domains : {', '.join(info['domains']) or '(none)'}")
    log(f"  browser : {user_agent or '(unknown)'}")
    if info["expires_soonest"]:
        log(f"  first expiry: {info['expires_soonest']}")
    log(f"\n  written to {out_path}")

    if not cookies:
        log("\n  No sahibinden cookies found. Is this browser actually signed in?")
        log("  With --attach, check you started Chrome on the profile you use")
        log("  for sahibinden and not a fresh --user-data-dir.")

    return {"ok": bool(cookies), "path": str(out_path), **info}


def import_session(in_path: str | Path, *, dest: Path | None = None,
                   log=print) -> dict:
    """Install an exported session on this machine. Costs nothing, proves nothing.

    Validation here is structural only. Whether the site still honours these
    cookies — and honours them from *this* address — is not knowable without
    spending a request, so this deliberately does not spend one. The next
    search is the test.
    """
    in_path, dest = Path(in_path), Path(dest or DEFAULT_SESSION_PATH)

    try:
        session = json.loads(in_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"ok": False, "reason": f"no such file: {in_path}"}
    except json.JSONDecodeError as exc:
        return {"ok": False, "reason": f"not valid JSON: {exc}"}

    if not isinstance(session, dict) or not isinstance(session.get("cookies"), list):
        return {"ok": False,
                "reason": "not a session file — expected an object with a 'cookies' list"}

    # Filter on the way in as well as on the way out. Export writes only
    # sahibinden cookies, but an imported file is data from somewhere else, and
    # seeding whatever it happens to contain would push cookies for unrelated
    # sites into this browser.
    incoming = [c for c in session["cookies"]
                if isinstance(c, dict) and "name" in c and "domain" in c]
    session["cookies"] = [_clean(c) for c in incoming
                          if COOKIE_DOMAIN_MARKER in (c.get("domain") or "").lower()]
    foreign = len(incoming) - len(session["cookies"])
    info = summarise(session)

    if not session["cookies"]:
        return {"ok": False, "reason": "no usable cookies in that file", **info}

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(session, indent=2), encoding="utf-8")

    log(f"  {info['cookies']} cookies installed ({info['live']} live, "
        f"{info['expired']} expired)")
    if foreign:
        log(f"  {foreign} cookie(s) for other sites ignored")
    log(f"  exported: {info['exported_at']}")
    log(f"  browser : {info['user_agent'] or '(unknown)'}")
    if info["expires_soonest"]:
        log(f"  first expiry: {info['expires_soonest']}")
    log(f"\n  stored at {dest}")

    if info["expired"] and not info["live"]:
        log("\n  Every cookie in this file has already expired. Re-run")
        log("  'export-session' on the signed-in machine and copy it again.")
        return {"ok": False, "reason": "all cookies expired", **info}

    log("\n  Nothing was fetched. The next search is what proves the session")
    log("  travels — if it comes back walled, the site bound it to the address")
    log("  it was created on and no cookie file will move it.")
    return {"ok": True, "path": str(dest), **info}


def load_session(path: Path | None = None) -> dict | None:
    """The installed session, or None. Used by the fetcher to seed a context."""
    path = Path(path or DEFAULT_SESSION_PATH)
    if not path.exists():
        return None
    try:
        session = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return session if isinstance(session.get("cookies"), list) else None


def seed_context(ctx, session: dict | None = None, *, profile_dir=None,
                 log=None) -> int:
    """Push an imported session's cookies into a fresh browser context.

    Returns how many were applied. Expired cookies are dropped here rather than
    at import, so a file that sat on a laptop overnight degrades gracefully
    instead of failing at the point of transfer.

    Once per imported file and profile. After that the profile holds the
    cookies and whatever the site sets from then on, and pushing the file's
    copies back in on every launch would roll each of those back to the older
    value. A profile that has lost its sahibinden cookies altogether is seeded
    again. (The shortest-lived sign-in cookie, ``st``, expires 24 hours after
    sign-in and was not renewed by searching, so a file older than that is
    missing it.)
    """
    session = session or load_session()
    if not session:
        return 0

    marker = str(profile_dir) if profile_dir else None
    if marker and marker in (session.get("seeded_into") or []):
        try:
            holds_session = any(COOKIE_DOMAIN_MARKER in (c.get("domain") or "")
                                for c in ctx.cookies())
        except Exception:
            holds_session = False
        if holds_session:
            return 0

    now = time.time()
    live = [c for c in session["cookies"]
            if not c.get("expires") or c["expires"] < 0 or c["expires"] > now]
    if not live:
        return 0

    try:
        ctx.add_cookies(live)
    except Exception as exc:  # a malformed cookie must not kill the run
        if log:
            log(f"  could not seed cookies: {exc}")
        return 0

    if marker:
        session["seeded_into"] = sorted({*(session.get("seeded_into") or []), marker})
        try:
            DEFAULT_SESSION_PATH.write_text(json.dumps(session, indent=2),
                                            encoding="utf-8")
        except OSError:
            pass  # worst case the next launch seeds again, as it always used to

    if log:
        log(f"  seeded {len(live)} cookies from {DEFAULT_SESSION_PATH.name}")
    return len(live)
