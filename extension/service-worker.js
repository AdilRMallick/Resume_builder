"use strict";

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type !== "extract-active-job-page") return false;

  (async () => {
    try {
      const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
      if (!tab?.id) throw new Error("No active page is available.");
      if (!/^https?:/.test(tab.url || "")) {
        throw new Error("Open a normal job page first. Browser settings pages cannot be read.");
      }

      const [{ result }] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => {
          const selectors = [
            "[data-testid*='job-description']",
            "[data-testid*='jobDescription']",
            "[class*='job-description']",
            "[class*='jobDescription']",
            "[id*='job-description']",
            "[id*='jobDescription']",
            "main",
            "article",
            "[role='main']",
          ];
          const candidates = [...new Set(selectors.flatMap((selector) => [...document.querySelectorAll(selector)]))]
            .map((element) => (element.innerText || "").trim())
            .filter((value) => value.length >= 250)
            .sort((left, right) => right.length - left.length);
          const pageText = (candidates[0] || document.body?.innerText || "").trim().slice(0, 100000);
          const heading = (document.querySelector("h1")?.innerText || "").trim();
          const siteName = document.querySelector('meta[property="og:site_name"]')?.content || "";
          const titleParts = document.title.split(/\s+[|\-\u2014]\s+/).map((part) => part.trim()).filter(Boolean);
          return {
            text: pageText,
            title: heading || titleParts[0] || document.title,
            company: siteName || titleParts[1] || "",
            url: location.href,
          };
        },
      });
      sendResponse({ ok: true, ...result });
    } catch (error) {
      sendResponse({ ok: false, error: error.message });
    }
  })();

  return true;
});
