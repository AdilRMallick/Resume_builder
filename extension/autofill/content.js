"use strict";

// The in-page surface: a floating Fill button and the message endpoint the side panel
// calls. Everything here is presentation; the filling itself lives in runner.js.

(function () {
  const ns = globalThis.JMEAutofill;
  const STORAGE_KEY = "jmeProfile";

  // The side panel re-injects the whole engine into tabs that predate the install. That
  // path can run against a tab the declared content script already covers, so registering
  // the listener twice would answer every message twice.
  if (globalThis.__jmeAutofillMounted) return;
  globalThis.__jmeAutofillMounted = true;

  let busy = false;

  async function loadProfile() {
    const stored = await chrome.storage.local.get(STORAGE_KEY);
    const profile = stored[STORAGE_KEY];
    if (!profile) return null;
    return globalThis.JMEProfile.normalize(profile);
  }

  async function fillNow() {
    if (busy) return { error: "A fill is already running." };
    busy = true;
    try {
      const profile = await loadProfile();
      if (!profile) return { error: "No profile saved yet. Open the side panel and fill in your details first." };
      return await ns.run(profile, profile.preferences);
    } catch (error) {
      return { error: error.message };
    } finally {
      busy = false;
    }
  }

  // ----------------------------------------------------------------------------------
  // side panel bridge
  // ----------------------------------------------------------------------------------

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "jme-autofill-run") {
      fillNow().then(sendResponse);
      return true;
    }
    if (message?.type === "jme-autofill-ping") {
      sendResponse({ ready: true, workday: ns.isWorkdayApplication(), step: ns.detectStep() });
      return false;
    }
    return false;
  });

  // ----------------------------------------------------------------------------------
  // floating button
  // ----------------------------------------------------------------------------------

  // Rendered in a shadow root so the host page's stylesheet cannot reach it and its own
  // rules cannot leak into the form underneath.
  const HOST_ID = "jme-autofill-host";

  function mount() {
    if (document.getElementById(HOST_ID) || !ns.isWorkdayApplication()) return;
    const host = document.createElement("div");
    host.id = HOST_ID;
    host.style.cssText = "position:fixed;right:18px;bottom:18px;z-index:2147483647;";
    const shadow = host.attachShadow({ mode: "open" });
    shadow.innerHTML = `
      <style>
        :host { all: initial; }
        .pill {
          display: flex; align-items: center; gap: 8px;
          font: 600 13px/1.2 system-ui, -apple-system, "Segoe UI", sans-serif;
          background: #14532d; color: #f0fdf4; border: 0; border-radius: 999px;
          padding: 11px 18px; cursor: pointer; box-shadow: 0 6px 20px rgba(0,0,0,.25);
        }
        .pill:hover { background: #166534; }
        .pill:disabled { opacity: .65; cursor: progress; }
        .dot { width: 7px; height: 7px; border-radius: 50%; background: #4ade80; }
        .toast {
          margin-top: 10px; max-width: 300px; max-height: 240px; overflow-y: auto;
          font: 400 12px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
          background: #0b1220; color: #e2e8f0; border-radius: 12px; padding: 12px 14px;
          box-shadow: 0 6px 20px rgba(0,0,0,.3);
        }
        .toast strong { color: #fff; }
        .toast ul { margin: 6px 0 0; padding-left: 16px; }
        .toast li { margin: 2px 0; }
        .muted { color: #94a3b8; }
        [hidden] { display: none; }
      </style>
      <button class="pill" id="fill" type="button"><span class="dot"></span>Fill this page</button>
      <div class="toast" id="toast" hidden></div>`;
    document.documentElement.appendChild(host);

    const button = shadow.getElementById("fill");
    const toast = shadow.getElementById("toast");

    button.addEventListener("click", async () => {
      button.disabled = true;
      button.lastChild.textContent = "Filling...";
      toast.hidden = true;
      const report = await fillNow();
      button.disabled = false;
      button.lastChild.textContent = "Fill this page";
      renderToast(toast, report);
    });
  }

  function renderToast(toast, report) {
    toast.hidden = false;
    if (report.error) {
      toast.innerHTML = `<strong>Could not fill.</strong><div class="muted">${escapeHTML(report.error)}</div>`;
      return;
    }
    const lines = [];
    lines.push(`<strong>${escapeHTML(report.step)}: ${report.filled.length} field${report.filled.length === 1 ? "" : "s"} filled.</strong>`);
    lines.push(`<div class="muted">Nothing was submitted. Review before you continue.</div>`);
    if (report.skipped.length) {
      lines.push(`<ul>${report.skipped.slice(0, 5).map((item) =>
        `<li class="muted">${escapeHTML(item.field)}: ${escapeHTML(item.reason)}</li>`).join("")}</ul>`);
    }
    for (const warning of report.warnings) {
      lines.push(`<div class="muted">${escapeHTML(warning)}</div>`);
    }
    toast.innerHTML = lines.join("");
    setTimeout(() => { toast.hidden = true; }, 12000);
  }

  const escapeHTML = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (character) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]
    );

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount, { once: true });
  } else {
    mount();
  }
  // Workday swaps the whole page body between steps without a navigation, so the button
  // has to be re-mounted rather than mounted once.
  new MutationObserver(() => mount()).observe(document.documentElement, { childList: true, subtree: false });
})();
