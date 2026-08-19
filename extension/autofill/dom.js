"use strict";

// Low-level DOM helpers shared by every Workday widget driver.
//
// Workday renders a React SPA into a deeply nested, generated DOM. Two facts drive
// everything in this file. First, class names and element ids are generated per build,
// so the only stable hooks are `data-automation-id` attributes and the accessible name
// of a control. Second, React owns the value of every input, so assigning `.value`
// directly is silently reverted on the next render; the native setter has to be invoked
// on the prototype so React's own change tracker sees the write.

(function (global) {
  const ns = (global.JMEAutofill = global.JMEAutofill || {});

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  /** Lowercase, punctuation-free form used for every text comparison in the engine. */
  const norm = (value) =>
    String(value ?? "")
      .toLowerCase()
      .replace(/[‘’]/g, "'")
      .replace(/[^a-z0-9]+/g, " ")
      .trim();

  const tokens = (value) => norm(value).split(" ").filter(Boolean);

  function isVisible(element) {
    if (!element || !element.isConnected) return false;
    const style = getComputedStyle(element);
    if (style.display === "none" || style.visibility === "hidden") return false;
    if (element.hasAttribute("aria-hidden") && element.getAttribute("aria-hidden") === "true") {
      return false;
    }
    const rect = element.getBoundingClientRect();
    return rect.width > 1 && rect.height > 1;
  }

  const automationId = (element) => element?.getAttribute?.("data-automation-id") || "";

  /**
   * Accessible name of a control, in the order screen readers resolve it.
   *
   * Workday labels most fields with `aria-labelledby` pointing at a sibling span, so the
   * `<label for>` path alone misses the majority of them.
   */
  function labelText(element) {
    if (!element) return "";
    const parts = [];
    const labelledBy = element.getAttribute("aria-labelledby");
    if (labelledBy) {
      for (const id of labelledBy.split(/\s+/)) {
        const target = document.getElementById(id);
        if (target) parts.push(target.textContent || "");
      }
    }
    if (element.getAttribute("aria-label")) parts.push(element.getAttribute("aria-label"));
    if (element.id) {
      for (const label of document.querySelectorAll(`label[for="${CSS.escape(element.id)}"]`)) {
        parts.push(label.textContent || "");
      }
    }
    const wrapping = element.closest("label");
    if (wrapping) parts.push(wrapping.textContent || "");
    if (element.placeholder) parts.push(element.placeholder);
    if (element.title) parts.push(element.title);
    return parts.join(" ").replace(/\s+/g, " ").trim();
  }

  /**
   * Text of the nearest enclosing group, used to disambiguate identically named fields.
   *
   * "Country" appears in the address block, the phone block, and often a citizenship
   * question on the same page; only the surrounding fieldset tells them apart.
   */
  function groupText(element) {
    const parts = [];
    let node = element?.parentElement;
    let hops = 0;
    while (node && hops < 8) {
      const heading = node.querySelector(":scope > legend, :scope > h2, :scope > h3, :scope > h4, :scope > [role='heading']");
      if (heading) parts.push(heading.textContent || "");
      const groupLabel = node.getAttribute?.("aria-label");
      if (groupLabel) parts.push(groupLabel);
      node = node.parentElement;
      hops += 1;
    }
    return parts.join(" ").replace(/\s+/g, " ").trim();
  }

  /** Every identifying string for a control, concatenated for keyword matching. */
  function signature(element) {
    return norm(
      [
        automationId(element),
        element.getAttribute?.("data-automation-label") || "",
        element.id || "",
        element.name || "",
        labelText(element),
      ].join(" ")
    );
  }

  /**
   * Write a value the way a user's keystrokes would, so React's onChange fires.
   *
   * React caches the last value it wrote on the DOM node. Assigning `element.value`
   * updates that cache too, so the subsequent `input` event looks like a no-op and the
   * component reverts on re-render. Calling the prototype setter bypasses the cache.
   */
  function setNativeValue(element, value) {
    const prototype =
      element instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : element instanceof HTMLSelectElement
          ? HTMLSelectElement.prototype
          : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
    if (setter) setter.call(element, value);
    else element.value = value;
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
  }

  /** A full pointer/mouse sequence. Workday menus ignore a bare `.click()`. */
  function clickReal(element) {
    const options = { bubbles: true, cancelable: true, view: window, composed: true };
    element.dispatchEvent(new PointerEvent("pointerdown", options));
    element.dispatchEvent(new MouseEvent("mousedown", options));
    element.focus?.({ preventScroll: true });
    element.dispatchEvent(new PointerEvent("pointerup", options));
    element.dispatchEvent(new MouseEvent("mouseup", options));
    element.dispatchEvent(new MouseEvent("click", options));
  }

  function fireKey(element, key) {
    for (const type of ["keydown", "keypress", "keyup"]) {
      element.dispatchEvent(new KeyboardEvent(type, { key, bubbles: true, cancelable: true }));
    }
  }

  function scrollIntoView(element) {
    try {
      element.scrollIntoView({ block: "center", inline: "nearest" });
    } catch {
      /* detached nodes are not worth a stack trace */
    }
  }

  /** Poll `predicate` until it returns something truthy, or give up and return null. */
  async function waitFor(predicate, { timeout = 4000, interval = 60 } = {}) {
    const deadline = Date.now() + timeout;
    for (;;) {
      let value = null;
      try {
        value = predicate();
      } catch {
        value = null;
      }
      if (value) return value;
      if (Date.now() >= deadline) return null;
      await sleep(interval);
    }
  }

  /**
   * Resolve once the DOM has been quiet for `quiet` ms, or after `timeout` regardless.
   *
   * Workday re-renders a whole panel after most interactions; reading the DOM mid-render
   * finds half-mounted widgets. Waiting for quiet is more reliable than a fixed sleep and
   * usually much faster.
   */
  function settle({ quiet = 300, timeout = 2500 } = {}) {
    return new Promise((resolve) => {
      let timer = null;
      const observer = new MutationObserver(() => {
        clearTimeout(timer);
        timer = setTimeout(finish, quiet);
      });
      const finish = () => {
        clearTimeout(timer);
        clearTimeout(hardStop);
        observer.disconnect();
        resolve();
      };
      const hardStop = setTimeout(finish, timeout);
      timer = setTimeout(finish, quiet);
      observer.observe(document.body, { childList: true, subtree: true, attributes: true });
    });
  }

  /**
   * How well `candidate` answers to `target`, 0 when it does not.
   *
   * Option lists are the fuzziest surface in Workday: the profile says "Bachelor's
   * Degree" and the tenant offers "Bachelor's Degree (BA/BS)". Exact match wins, then
   * prefix, then containment, then token overlap, and each tier prefers the shortest
   * option so "Master's Degree" never loses to "Master's Degree - Other".
   */
  function matchScore(candidate, target) {
    const left = norm(candidate);
    const right = norm(target);
    if (!left || !right) return 0;
    if (left === right) return 1000;
    if (left.startsWith(right) || right.startsWith(left)) return 800 - Math.abs(left.length - right.length);
    if (left.includes(right) || right.includes(left)) return 600 - Math.abs(left.length - right.length);
    const leftTokens = new Set(tokens(left));
    const rightTokens = tokens(right);
    if (!rightTokens.length) return 0;
    const shared = rightTokens.filter((token) => leftTokens.has(token)).length;
    if (!shared) return 0;
    return Math.round((shared / rightTokens.length) * 400) - Math.abs(left.length - right.length) / 10;
  }

  /** Best entry of `candidates` for `target`, or null when nothing clears `minimum`. */
  function bestMatch(candidates, target, { minimum = 300, text = (item) => item.textContent } = {}) {
    let winner = null;
    let winningScore = minimum;
    for (const candidate of candidates) {
      const score = matchScore(text(candidate), target);
      if (score > winningScore) {
        winner = candidate;
        winningScore = score;
      }
    }
    return winner;
  }

  Object.assign(ns, {
    sleep,
    norm,
    tokens,
    isVisible,
    automationId,
    labelText,
    groupText,
    signature,
    setNativeValue,
    clickReal,
    fireKey,
    scrollIntoView,
    waitFor,
    settle,
    matchScore,
    bestMatch,
  });
})(globalThis);
