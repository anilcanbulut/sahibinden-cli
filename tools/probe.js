/* Fingerprint probe. Runs identically in the automated browser and in your
 * normal Chrome, so the two outputs can be diffed field by field.
 *
 * To capture your real-Chrome baseline: open any ordinary page (not a
 * chrome:// URL — those restrict script), press F12, paste this whole file
 * into the Console, press Enter, then right-click the result → Copy object.
 */
(() => {
  const safe = (fn, fallback = "ERROR") => {
    try {
      const v = fn();
      return v === undefined ? "undefined" : v;
    } catch (e) {
      return `${fallback}: ${e.name}`;
    }
  };

  const gl = safe(() => {
    const c = document.createElement("canvas");
    const ctx = c.getContext("webgl") || c.getContext("experimental-webgl");
    if (!ctx) return null;
    const dbg = ctx.getExtension("WEBGL_debug_renderer_info");
    return {
      vendor: dbg ? ctx.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null,
      renderer: dbg ? ctx.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
    };
  });

  return {
    // The single most-checked automation tell.
    webdriver: safe(() => navigator.webdriver),

    userAgent: safe(() => navigator.userAgent),
    uaData: safe(() =>
      navigator.userAgentData
        ? {
            brands: navigator.userAgentData.brands.map((b) => `${b.brand} ${b.version}`),
            mobile: navigator.userAgentData.mobile,
            platform: navigator.userAgentData.platform,
          }
        : null
    ),
    platform: safe(() => navigator.platform),
    languages: safe(() => navigator.languages.join(",")),
    hardwareConcurrency: safe(() => navigator.hardwareConcurrency),
    deviceMemory: safe(() => navigator.deviceMemory),
    maxTouchPoints: safe(() => navigator.maxTouchPoints),

    // Headless browsers historically report zero plugins / no mime types.
    pluginCount: safe(() => navigator.plugins.length),
    pluginNames: safe(() => Array.from(navigator.plugins).map((p) => p.name).join(" | ")),
    mimeTypeCount: safe(() => navigator.mimeTypes.length),

    // window.chrome is absent in plain Chromium and in many stealth setups.
    hasChromeObject: safe(() => typeof window.chrome),
    chromeKeys: safe(() => (window.chrome ? Object.keys(window.chrome).join(",") : null)),
    hasChromeRuntime: safe(() => Boolean(window.chrome && window.chrome.runtime)),

    // Classic mismatch: notification permission "denied" while Notification
    // says "default" happens under headless.
    permissionMismatch: "async",

    webglVendor: gl ? gl.vendor : null,
    webglRenderer: gl ? gl.renderer : null,

    screen: safe(() => `${screen.width}x${screen.height} avail ${screen.availWidth}x${screen.availHeight} depth ${screen.colorDepth}`),
    devicePixelRatio: safe(() => window.devicePixelRatio),
    timezone: safe(() => Intl.DateTimeFormat().resolvedOptions().timeZone),
    timezoneOffset: safe(() => new Date().getTimezoneOffset()),

    // CDP leak check: Patchright's main fix. Under stock Playwright, a stack
    // trace serialisation side effect is observable while devtools protocol
    // has Runtime enabled.
    cdpStackLeak: safe(() => {
      let leaked = false;
      const probe = new Error();
      Object.defineProperty(probe, "stack", {
        configurable: true,
        get() {
          leaked = true;
          return "";
        },
      });
      console.debug(probe);
      return leaked;
    }),

    // Automation frameworks often leave globals behind.
    automationGlobals: safe(() =>
      Object.keys(window)
        .filter((k) => /webdriver|selenium|puppeteer|playwright|_phantom|callPhantom|domAutomation|cdc_/i.test(k))
        .join(",") || "none"
    ),
    documentAutomationAttrs: safe(() =>
      Object.keys(document)
        .filter((k) => /\$cdc_|\$wdc_|selenium|driver/i.test(k))
        .join(",") || "none"
    ),

    functionToStringNative: safe(
      () => Function.prototype.toString.call(navigator.permissions.query).includes("[native code]")
    ),
  };
})();
