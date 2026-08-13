"""Fingerprint the automated browser without ever contacting sahibinden.

Run this, then capture the same probe in your normal Chrome and diff the two:

    python tools/fingerprint_test.py            # automated run
    python tools/fingerprint_test.py --compare  # diff against manual baseline

Anything that differs between the two is something sahibinden could also use to
tell them apart.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sahibinden.fetch.browser import ACCEPT_LANGUAGES, connect, launch  # noqa: E402

TOOLS = Path(__file__).resolve().parent
OUT_DIR = TOOLS.parent / "fingerprint"
PROBE = (TOOLS / "probe.js").read_text(encoding="utf-8")

TLS_URL = "https://tls.browserleaks.com/json"
BOT_URL = "https://bot.sannysoft.com/"
NEUTRAL_URL = "https://example.com/"

# Fields where a difference means the automated browser is distinguishable.
# Screen size and pixel ratio are excluded — those vary with window size and are
# not automation tells on their own.
CRITICAL = {
    "webdriver", "pluginCount", "mimeTypeCount", "hasChromeObject",
    "hasChromeRuntime", "cdpStackLeak", "automationGlobals",
    "documentAutomationAttrs", "functionToStringNative", "userAgent",
    "platform", "languages", "timezone", "webglVendor", "webglRenderer",
    "hardwareConcurrency", "deviceMemory",
}


def run_automated(*, attach: bool = False) -> dict:
    """Probe the automated browser. ``attach`` drives the user's running Chrome.

    Worth running in both modes: attaching over CDP is a different code path
    from launching, and could in principle reintroduce a tell that the launched
    profile does not have.
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    result: dict = {"mode": "cdp-attach" if attach else "launch"}

    with (connect() if attach else launch()) as ctx:
        page = ctx.new_page() if attach else (ctx.pages[0] if ctx.pages else ctx.new_page())

        print("  [1/3] TLS fingerprint ...", flush=True)
        try:
            page.goto(TLS_URL, wait_until="domcontentloaded", timeout=45_000)
            body = page.evaluate("() => document.body.innerText")
            result["tls"] = json.loads(body)
        except Exception as exc:  # network/parse — not fatal to the run
            result["tls"] = {"error": f"{type(exc).__name__}: {exc}"}

        print("  [2/3] JS surface probe ...", flush=True)
        page.goto(NEUTRAL_URL, wait_until="domcontentloaded", timeout=45_000)
        result["js"] = page.evaluate(PROBE)
        result["js"]["permissionMismatch"] = page.evaluate(
            """async () => {
                try {
                    const p = await navigator.permissions.query({name: 'notifications'});
                    return `${Notification.permission}/${p.state}`;
                } catch (e) { return 'ERROR: ' + e.name; }
            }"""
        )

        print("  [3/3] bot.sannysoft.com ...", flush=True)
        try:
            page.goto(BOT_URL, wait_until="networkidle", timeout=45_000)
            page.wait_for_timeout(2500)
            result["sannysoft"] = page.evaluate(
                """() => {
                    const out = {};
                    document.querySelectorAll('table tr').forEach(tr => {
                        const td = tr.querySelectorAll('td');
                        if (td.length >= 2) {
                            const k = td[0].innerText.trim();
                            const v = td[1].innerText.trim().split('\\n')[0];
                            const cls = td[1].className || '';
                            if (k) out[k] = {value: v.slice(0, 60), status: cls};
                        }
                    });
                    return out;
                }"""
            )
            page.screenshot(path=str(OUT_DIR / "sannysoft.png"), full_page=True)
        except Exception as exc:
            result["sannysoft"] = {"error": f"{type(exc).__name__}: {exc}"}

    return result


def summarise(result: dict) -> None:
    js = result.get("js", {})
    tls = result.get("tls", {})
    sanny = result.get("sannysoft", {})

    print("\n--- automation tells ---")
    checks = [
        ("navigator.webdriver", js.get("webdriver"), lambda v: v in (False, "undefined")),
        ("CDP stack leak", js.get("cdpStackLeak"), lambda v: v is False),
        ("automation globals", js.get("automationGlobals"), lambda v: v == "none"),
        ("document driver attrs", js.get("documentAutomationAttrs"), lambda v: v == "none"),
        ("window.chrome", js.get("hasChromeObject"), lambda v: v == "object"),
        # chrome.runtime is NOT expected: real Chrome only exposes it to pages
        # an extension has declared externally_connectable. Verified absent in
        # the manual baseline too, so requiring it would be a false alarm.
        ("plugin count", js.get("pluginCount"), lambda v: isinstance(v, int) and v > 0),
        ("native toString", js.get("functionToStringNative"), lambda v: v is True),
        ("timezone", js.get("timezone"), lambda v: v == "Europe/Istanbul"),
        # Must equal what the real browser sends, not "looks Turkish".
        ("languages", js.get("languages"), lambda v: v == ACCEPT_LANGUAGES),
        # A mismatch here (e.g. "denied/prompt") is the headless tell; matching
        # values are healthy.
        ("permission consistency", js.get("permissionMismatch"),
         lambda v: isinstance(v, str) and v.split("/")[0] in ("default", "granted")),
    ]
    for label, value, ok in checks:
        mark = "PASS" if ok(value) else "FAIL"
        print(f"  [{mark}] {label:<24} {value}")

    if "error" not in tls:
        print("\n--- TLS ---")
        for k in ("ja3_hash", "ja3n_hash", "akamai_hash", "user_agent"):
            if k in tls:
                print(f"  {k:<14} {str(tls[k])[:78]}")
    else:
        print(f"\n--- TLS --- {tls['error']}")

    if sanny and "error" not in sanny:
        reds = [k for k, v in sanny.items() if "failed" in str(v.get("status", "")).lower()]
        print(f"\n--- bot.sannysoft.com: {len(sanny)} checks, {len(reds)} failed ---")
        for k in reds:
            print(f"  [FAIL] {k}: {sanny[k]['value']}")
        if not reds:
            print("  all checks passed")


def compare(auto: dict, manual: dict) -> int:
    a, m = auto.get("js", {}), manual.get("js", manual)
    print("\n--- automated vs your real Chrome ---")
    diffs = 0
    for key in sorted(set(a) | set(m)):
        av, mv = a.get(key, "<missing>"), m.get(key, "<missing>")
        if av != mv:
            critical = key in CRITICAL
            diffs += critical
            print(f"  [{'CRITICAL' if critical else 'cosmetic'}] {key}")
            print(f"      automated : {str(av)[:70]}")
            print(f"      real      : {str(mv)[:70]}")
    print(f"\n{diffs} critical difference(s).")
    if diffs == 0:
        print("Automated browser is indistinguishable on every checked signal.")
    return diffs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare", action="store_true",
                    help="diff automated.json against manual.json")
    ap.add_argument("--cdp", action="store_true",
                    help="probe the running Chrome over CDP instead of launching one")
    args = ap.parse_args()

    auto_path = OUT_DIR / ("cdp.json" if args.cdp else "automated.json")
    manual_path = OUT_DIR / "manual.json"

    if args.compare:
        if not auto_path.exists():
            print(f"Run without --compare first (missing {auto_path})")
            return 1
        if not manual_path.exists():
            print(f"Missing {manual_path} — see instructions in tools/probe.js")
            return 1
        return 0 if compare(
            json.loads(auto_path.read_text(encoding="utf-8")),
            json.loads(manual_path.read_text(encoding="utf-8")),
        ) == 0 else 2

    if args.cdp:
        print("Attaching to your running Chrome on localhost:9222 ...")
    else:
        print("Launching real Chrome via Patchright (a window will open) ...")
    result = run_automated(attach=args.cdp)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    auto_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    summarise(result)
    print(f"\nSaved: {auto_path}")
    print(f"Screenshot: {OUT_DIR / 'sannysoft.png'}")
    print("\nNext: capture your real-Chrome baseline (instructions at the top of")
    print("tools/probe.js), save it as fingerprint/manual.json, then run:")
    print("  python tools/fingerprint_test.py --compare")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
