"use strict";

// Drivers for the five Workday widgets that defeat a browser's built-in autofill:
// the button-and-popup dropdown, the typeahead multi-select, the three-part date field,
// the radio group rendered as styled divs, and the file input hidden behind a button.
//
// Every driver returns `{ ok, detail }` rather than throwing. A single unfillable field
// should cost that field, not the rest of the form.

(function (global) {
  const ns = (global.JMEAutofill = global.JMEAutofill || {});
  const {
    sleep,
    norm,
    isVisible,
    automationId,
    setNativeValue,
    clickReal,
    fireKey,
    scrollIntoView,
    waitFor,
    matchScore,
  } = ns;

  const ok = (detail) => ({ ok: true, detail });
  const fail = (detail) => ({ ok: false, detail });

  /** Workday renders menus into a portal at the end of <body>, not inside the field. */
  function openPopup() {
    const popups = [
      ...document.querySelectorAll(
        '[data-automation-widget="wd-popup"], [role="listbox"], [data-automation-id="activeListContainer"], [data-automation-id="selectListPopup"]'
      ),
    ].filter(isVisible);
    if (popups.length) return popups[popups.length - 1];

    // Tenants that wrap the menu in an unrecognised container still render the options
    // themselves the same way, so fall back to the smallest element holding all of them.
    const options = [
      ...document.querySelectorAll('[data-automation-id="promptOption"], [data-automation-id="menuItem"], [role="option"]'),
    ].filter(isVisible);
    if (!options.length) return null;
    let container = options[0].parentElement;
    while (container && !options.every((option) => container.contains(option))) {
      container = container.parentElement;
    }
    return container || options[0].parentElement;
  }

  function popupOptions(popup) {
    const options = [
      ...popup.querySelectorAll(
        '[role="option"], [data-automation-id="menuItem"], [data-automation-id="promptOption"], li[data-value], li'
      ),
    ].filter(isVisible);
    // Nested <li><div role="option"> markup yields both nodes; keep the innermost.
    return options.filter((option) => !options.some((other) => other !== option && option.contains(other)));
  }

  function optionLabel(option) {
    const explicit = option.getAttribute("data-automation-label") || option.getAttribute("aria-label");
    return (explicit || option.textContent || "").replace(/\s+/g, " ").trim();
  }

  async function closePopup() {
    if (!openPopup()) return;
    document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await sleep(80);
  }

  /** Commit a field the way leaving it would. React's onBlur listens for `focusout`. */
  function blur(element) {
    element.dispatchEvent(new FocusEvent("blur", { bubbles: false }));
    element.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
  }

  // ----------------------------------------------------------------------------------
  // text and textarea
  // ----------------------------------------------------------------------------------

  async function fillText(element, value, { overwrite = false } = {}) {
    if (!element || value === undefined || value === null || value === "") return fail("no value");
    if (element.disabled || element.readOnly) return fail("read-only");
    const existing = String(element.value || "").trim();
    if (existing && !overwrite) return fail(`kept existing "${existing}"`);
    if (norm(existing) === norm(value)) return ok("already correct");
    scrollIntoView(element);
    element.focus({ preventScroll: true });
    setNativeValue(element, "");
    setNativeValue(element, String(value));
    blur(element);
    await sleep(40);
    return String(element.value) === String(value) ? ok(String(value)) : fail("value did not stick");
  }

  // ----------------------------------------------------------------------------------
  // checkbox and radio
  // ----------------------------------------------------------------------------------

  async function setCheckbox(element, checked) {
    if (!element) return fail("missing");
    if (Boolean(element.checked) === Boolean(checked)) return ok(checked ? "checked" : "unchecked");
    scrollIntoView(element);
    clickReal(element);
    await sleep(60);
    if (Boolean(element.checked) !== Boolean(checked)) {
      // Some tenants style the box with an overlaying <div>; the label is the hit target.
      const label = element.id ? document.querySelector(`label[for="${CSS.escape(element.id)}"]`) : null;
      if (label) {
        clickReal(label);
        await sleep(60);
      }
    }
    return Boolean(element.checked) === Boolean(checked) ? ok(checked ? "checked" : "unchecked") : fail("did not toggle");
  }

  /**
   * Pick a radio inside `container` whose accessible name matches `value`.
   *
   * Handles both real `input[type=radio]` groups and the `role="radio"` divs Workday
   * uses for yes/no questions.
   */
  async function chooseRadio(container, value) {
    if (!container || !value) return fail("missing");
    const radios = [...container.querySelectorAll('input[type="radio"], [role="radio"]')].filter(isVisible);
    if (!radios.length) return fail("no radios");
    let winner = null;
    let winningScore = 300;
    for (const radio of radios) {
      const label =
        ns.labelText(radio) ||
        radio.closest("label")?.textContent ||
        radio.parentElement?.textContent ||
        "";
      const score = matchScore(label, value);
      if (score > winningScore) {
        winner = radio;
        winningScore = score;
      }
    }
    if (!winner) return fail(`no option matching "${value}"`);
    scrollIntoView(winner);
    clickReal(winner);
    await sleep(80);
    const selected = winner.checked || winner.getAttribute("aria-checked") === "true";
    return selected ? ok(String(value)) : fail("did not select");
  }

  // ----------------------------------------------------------------------------------
  // dropdown
  // ----------------------------------------------------------------------------------

  /**
   * Choose `value` in a Workday dropdown, trying each alias in turn.
   *
   * `element` may be a native <select>, the trigger button, or any wrapper containing
   * one — callers match on the field, not on the exact node Workday chose to render.
   */
  async function selectDropdown(element, value, { aliases = [] } = {}) {
    if (!element || !value) return fail("no value");
    const wanted = [String(value), ...aliases].filter(Boolean);

    const native =
      element instanceof HTMLSelectElement ? element : element.querySelector?.("select");
    if (native) return selectNative(native, wanted);

    const trigger = findTrigger(element);
    if (!trigger) return fail("no dropdown trigger");

    const current = (trigger.textContent || "").trim();
    if (current && wanted.some((option) => matchScore(current, option) >= 800)) {
      return ok(`already "${current}"`);
    }

    scrollIntoView(trigger);
    clickReal(trigger);
    const popup = await waitFor(openPopup, { timeout: 3000 });
    if (!popup) return fail("dropdown did not open");

    const options = popupOptions(popup);
    if (!options.length) {
      await closePopup();
      return fail("dropdown had no options");
    }

    let winner = null;
    let winningScore = 300;
    for (const option of options) {
      for (const candidate of wanted) {
        const score = matchScore(optionLabel(option), candidate);
        if (score > winningScore) {
          winner = option;
          winningScore = score;
        }
      }
    }
    if (!winner) {
      await closePopup();
      return fail(`no option matching "${value}" among ${options.length}`);
    }

    const chosen = optionLabel(winner);
    clickReal(winner);
    await sleep(150);
    await closePopup();
    return ok(chosen);
  }

  function findTrigger(element) {
    if (element.matches?.('button, [role="button"], [role="combobox"], [aria-haspopup]')) return element;
    return (
      element.querySelector?.('button[aria-haspopup], [role="combobox"], [aria-haspopup="listbox"], button') || null
    );
  }

  async function selectNative(select, wanted) {
    const options = [...select.options];
    let winner = null;
    let winningScore = 300;
    for (const option of options) {
      for (const candidate of wanted) {
        const score = Math.max(matchScore(option.textContent, candidate), matchScore(option.value, candidate));
        if (score > winningScore) {
          winner = option;
          winningScore = score;
        }
      }
    }
    if (!winner) return fail(`no option matching "${wanted[0]}"`);
    setNativeValue(select, winner.value);
    return ok(winner.textContent.trim());
  }

  // ----------------------------------------------------------------------------------
  // multi-select typeahead
  // ----------------------------------------------------------------------------------

  /**
   * Add each of `values` to a Workday multi-select.
   *
   * The widget only renders options once a search string has been typed, and it drops
   * the popup between selections, so every value is a fresh type-wait-click cycle.
   * Values the tenant does not offer are reported rather than forced.
   */
  async function selectMultiSelect(element, values, { limit = 12 } = {}) {
    const wanted = (Array.isArray(values) ? values : [values]).filter(Boolean).slice(0, limit);
    if (!element || !wanted.length) return fail("no values");

    const input = element.matches?.("input")
      ? element
      : element.querySelector('input[type="text"], input:not([type]), input[role="combobox"]');
    if (!input) return fail("no multi-select input");

    const added = [];
    const missed = [];
    for (const value of wanted) {
      if (selectedChips(element).some((chip) => matchScore(chip, value) >= 800)) {
        added.push(value);
        continue;
      }
      scrollIntoView(input);
      input.focus({ preventScroll: true });
      setNativeValue(input, "");
      setNativeValue(input, String(value));
      fireKey(input, "a");
      const popup = await waitFor(
        () => {
          const candidate = openPopup();
          return candidate && popupOptions(candidate).length ? candidate : null;
        },
        { timeout: 2500 }
      );
      if (!popup) {
        missed.push(value);
        setNativeValue(input, "");
        continue;
      }
      const options = popupOptions(popup);
      let winner = null;
      let winningScore = 400;
      for (const option of options) {
        const score = matchScore(optionLabel(option), value);
        if (score > winningScore) {
          winner = option;
          winningScore = score;
        }
      }
      if (!winner) {
        missed.push(value);
        setNativeValue(input, "");
        await closePopup();
        continue;
      }
      added.push(optionLabel(winner));
      clickReal(winner);
      await sleep(180);
      setNativeValue(input, "");
    }
    await closePopup();
    if (!added.length) return fail(`none of ${wanted.length} values were offered`);
    const detail = missed.length ? `${added.join(", ")} (not offered: ${missed.join(", ")})` : added.join(", ");
    return ok(detail);
  }

  function selectedChips(container) {
    return [
      ...container.querySelectorAll(
        '[data-automation-id="selectedItem"], [data-automation-id="selectedItemContainer"] li, [role="listitem"]'
      ),
    ].map((chip) => (chip.textContent || "").trim());
  }

  // ----------------------------------------------------------------------------------
  // date
  // ----------------------------------------------------------------------------------

  /**
   * Fill a date field from `{ month, day, year }` (strings or numbers; day optional).
   *
   * Workday's date widget is three spinbutton inputs that each advance focus on entry.
   * Tenants that omit the day section get a two-part field, and a few older ones render
   * a single MM/DD/YYYY text input instead.
   */
  async function fillDate(element, { month, day, year }, { overwrite = false } = {}) {
    if (!element || (!month && !year)) return fail("no date");
    const pad = (value) => (value === undefined || value === "" ? "" : String(value).padStart(2, "0"));

    const sections = {
      month: element.querySelector('[data-automation-id="dateSectionMonth-input"], input[aria-label*="Month" i]'),
      day: element.querySelector('[data-automation-id="dateSectionDay-input"], input[aria-label*="Day" i]'),
      year: element.querySelector('[data-automation-id="dateSectionYear-input"], input[aria-label*="Year" i]'),
    };

    if (sections.month || sections.year) {
      const written = [];
      for (const [name, value] of [
        ["month", pad(month)],
        ["day", pad(day)],
        ["year", year ? String(year) : ""],
      ]) {
        const input = sections[name];
        if (!input || !value) continue;
        if (String(input.value || "").trim() && !overwrite) continue;
        scrollIntoView(input);
        input.focus({ preventScroll: true });
        setNativeValue(input, value);
        fireKey(input, value.slice(-1));
        blur(input);
        written.push(value);
        await sleep(50);
      }
      return written.length ? ok(written.join("/")) : fail("date sections were already set");
    }

    const single = element.matches?.("input") ? element : element.querySelector('input[type="text"], input');
    if (!single) return fail("no date input");
    const formatted = day ? `${pad(month)}/${pad(day)}/${year}` : `${pad(month)}/${year}`;
    return fillText(single, formatted, { overwrite });
  }

  // ----------------------------------------------------------------------------------
  // file upload
  // ----------------------------------------------------------------------------------

  /**
   * Attach a stored document to a file input via a synthetic DataTransfer.
   *
   * This is the one place the extension writes a real file into the page. `input.files`
   * is only assignable from a DataTransfer list, which is exactly what a drag-and-drop
   * would have produced, so Workday's upload handler sees nothing unusual.
   */
  async function attachFile(input, document_) {
    if (!input || !document_?.base64) return fail("no document stored");
    try {
      const binary = atob(document_.base64);
      const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
      const file = new File([bytes], document_.name || "resume.pdf", {
        type: document_.mimeType || "application/pdf",
      });
      const transfer = new DataTransfer();
      transfer.items.add(file);
      input.files = transfer.files;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      const dropTarget = input.closest('[data-automation-id*="fileUpload" i], [data-automation-id*="dropZone" i]') || input.parentElement;
      dropTarget?.dispatchEvent(new DragEvent("drop", { bubbles: true, dataTransfer: transfer }));
      await sleep(200);
      return ok(file.name);
    } catch (error) {
      return fail(`upload failed: ${error.message}`);
    }
  }

  Object.assign(ns, {
    ok,
    fail,
    openPopup,
    popupOptions,
    optionLabel,
    fillText,
    setCheckbox,
    chooseRadio,
    selectDropdown,
    selectMultiSelect,
    fillDate,
    attachFile,
  });
})(globalThis);
