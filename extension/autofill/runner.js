"use strict";

// Orchestration: work out which step of the application is on screen, fill what belongs
// to that step, and report what happened.
//
// The runner never advances the wizard and never submits. Workday applications are five
// or six steps long and a wrong answer buried three pages back is worse than a slow form,
// so the contract is one page per click, always ending with the user reviewing.

(function (global) {
  const ns = (global.JMEAutofill = global.JMEAutofill || {});
  const {
    norm,
    isVisible,
    automationId,
    clickReal,
    settle,
    sleep,
    matchScore,
    fillText,
    setCheckbox,
    chooseRadio,
    selectDropdown,
    selectMultiSelect,
    fillDate,
    attachFile,
    PAGE_SPECS,
    WORK_SPECS,
    EDUCATION_SPECS,
    WEBSITE_SPECS,
    aliasesFor,
    resolveAll,
  } = ns;

  const MONTHS = ["january","february","march","april","may","june","july","august","september","october","november","december"];

  /** Accept "2026-05-01", "May 2026", "05/2026", or `{ month, year }` from the profile. */
  function parseDate(value) {
    if (!value) return null;
    if (typeof value === "object") {
      const { month, day, year } = value;
      return month || year ? { month, day, year } : null;
    }
    const text = String(value).trim();
    if (norm(text) === "today") {
      const now = new Date();
      return { month: now.getMonth() + 1, day: now.getDate(), year: now.getFullYear() };
    }
    let match = text.match(/^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?$/);
    if (match) return { year: match[1], month: match[2], day: match[3] };
    match = text.match(/^(\d{1,2})\/(?:(\d{1,2})\/)?(\d{4})$/);
    if (match) return { month: match[1], day: match[2], year: match[3] };
    match = text.match(/^([a-z]+)\.?\s+(\d{4})$/i);
    if (match) {
      const index = MONTHS.findIndex((name) => name.startsWith(match[1].toLowerCase()));
      if (index >= 0) return { month: index + 1, year: match[2] };
    }
    match = text.match(/^(\d{4})$/);
    if (match) return { year: match[1] };
    return null;
  }

  /** Dispatch one resolved control to the driver for its widget kind. */
  async function applyField(spec, element, value, options) {
    switch (spec.kind) {
      case "text":
        return fillText(element, value, options);
      case "dropdown":
        return selectDropdown(element, value, { aliases: aliasesFor(spec.valueKind, value) });
      case "multiselect":
        return selectMultiSelect(element, value);
      case "date": {
        const parsed = parseDate(value);
        return parsed ? fillDate(element, parsed, options) : ns.fail("unparseable date");
      }
      case "checkbox":
        return setCheckbox(element, Boolean(value));
      case "radiogroup": {
        const answer =
          spec.valueKind === "yesno" ? (value === true || norm(value) === "yes" ? "Yes" : "No") : value;
        return chooseRadio(element, answer);
      }
      case "file":
        return attachFile(element, value);
      default:
        return ns.fail(`unknown kind ${spec.kind}`);
    }
  }

  /**
   * True when the profile has nothing to say about a field.
   *
   * `false` counts as nothing: it is what an unticked box in the profile looks like, and
   * the engine's job is to fill in answers, not to clear boxes the form arrived with.
   */
  const isEmpty = (value) =>
    value === null ||
    value === undefined ||
    value === "" ||
    value === false ||
    (Array.isArray(value) && !value.length);

  /**
   * Run a set of specs against a scope, collecting one report line per answer.
   *
   * Specs sharing an `alt` group are alternate spellings of the same answer, so they are
   * accounted for together: the first that fills wins, later ones are not attempted, and
   * a failure is only reported if every spelling failed.
   */
  async function fillSpecs(specs, scope, source, report, { claimed, options, prefix = "" }) {
    const resolved = resolveAll(specs, scope, { claimed });
    const groups = new Map();

    for (const spec of specs) {
      const name = spec.alt || spec.key;
      let group = groups.get(name);
      if (!group) {
        group = { filled: false, skipped: [], notFound: [] };
        groups.set(name, group);
      }
      if (group.filled) continue;

      let value;
      try {
        value = spec.value(source);
      } catch {
        value = null;
      }
      if (isEmpty(value)) continue;

      const label = prefix ? `${prefix} ${spec.key}` : spec.key;
      const element = resolved.get(spec.key);
      if (!element) {
        group.notFound.push(label);
        continue;
      }
      const result = await applyField(spec, element, value, options);
      if (result.ok) {
        group.filled = true;
        report.filled.push({ field: label, detail: result.detail });
      } else {
        group.skipped.push({ field: label, reason: result.detail });
      }
      await sleep(30);
    }

    for (const group of groups.values()) {
      if (group.filled) continue;
      report.skipped.push(...group.skipped);
      report.notFound.push(...group.notFound);
    }
  }

  // ----------------------------------------------------------------------------------
  // repeating sections
  // ----------------------------------------------------------------------------------

  /**
   * Locate a repeating section by automation id, then by its heading text.
   *
   * The heading fallback climbs to the nearest ancestor that also holds an Add button or
   * form controls, because the heading's own parent is usually just a text wrapper.
   */
  function findSection({ auto = [], titles = [] }) {
    for (const id of auto) {
      const direct = document.querySelector(`[data-automation-id="${id}"]`);
      if (direct && isVisible(direct)) return direct;
    }
    const headings = [...document.querySelectorAll('h2, h3, h4, legend, [role="heading"]')].filter(isVisible);
    for (const heading of headings) {
      const text = (heading.textContent || "").trim();
      if (!titles.some((title) => matchScore(text, title) >= 600)) continue;
      let node = heading.parentElement;
      for (let hops = 0; node && hops < 6; hops += 1) {
        if (node.querySelector("input, textarea, select, button")) return node;
        node = node.parentElement;
      }
    }
    return null;
  }

  function panelsIn(section, prefixes) {
    const found = [...section.querySelectorAll("[data-automation-id]")].filter((element) => {
      const id = automationId(element);
      return prefixes.some((prefix) => new RegExp(`^${prefix}-\\d+$`, "i").test(id)) && isVisible(element);
    });
    if (found.length) return found;
    // Tenants that do not number their panels still wrap each one in a group. Nested
    // groups yield both the outer and inner node, so keep only the innermost — an outer
    // group spans every entry and would collapse them all into one panel.
    const groups = [...section.querySelectorAll('[role="group"], fieldset')].filter(
      (element) => isVisible(element) && element.querySelector("input, textarea, select")
    );
    return groups.filter((group) => !groups.some((other) => other !== group && group.contains(other)));
  }

  function findAddButton(section) {
    const buttons = [...section.querySelectorAll('button, [role="button"], [data-automation-id="Add"]')].filter(isVisible);
    let best = null;
    let bestScore = 0;
    for (const button of buttons) {
      const text = norm(`${automationId(button)} ${button.textContent || ""} ${button.getAttribute("aria-label") || ""}`);
      if (text.includes("delete") || text.includes("remove") || text.includes("cancel")) continue;
      let score = 0;
      if (text === "add") score = 100;
      else if (text.includes("add another")) score = 90;
      else if (/\badd\b/.test(text)) score = 60;
      if (score > bestScore) {
        best = button;
        bestScore = score;
      }
    }
    return best;
  }

  /**
   * Grow the section until it holds `count` panels, then return them.
   *
   * The loop stops as soon as a click fails to add a panel; some tenants cap the number
   * of entries and silently ignore further clicks, and a `while` on the count alone would
   * spin forever there.
   */
  async function ensurePanels(section, prefixes, count, report, name) {
    let panels = panelsIn(section, prefixes);
    let guard = 0;
    while (panels.length < count && guard < count + 3) {
      const add = findAddButton(section);
      if (!add) break;
      clickReal(add);
      await settle();
      const grown = panelsIn(section, prefixes);
      if (grown.length <= panels.length) break;
      panels = grown;
      guard += 1;
    }
    if (panels.length < count) {
      report.warnings.push(
        `${name}: filled ${panels.length} of ${count} entries; this form would not open more.`
      );
    }
    return panels;
  }

  async function fillRepeating({ section, prefixes, entries, specs, name, report, claimed, options }) {
    if (!section || !entries?.length) return;
    const panels = await ensurePanels(section, prefixes, entries.length, report, name);
    for (let index = 0; index < Math.min(panels.length, entries.length); index += 1) {
      await fillSpecs(specs, panels[index], entries[index], report, {
        claimed,
        options,
        prefix: `${name} ${index + 1}`,
      });
    }
  }

  // ----------------------------------------------------------------------------------
  // step detection
  // ----------------------------------------------------------------------------------

  /** Name the current wizard step, for the report header and for the user's benefit. */
  function detectStep() {
    const heading = [...document.querySelectorAll('h1, h2, [data-automation-id="pageHeader"]')]
      .filter(isVisible)
      .map((element) => (element.textContent || "").trim())
      .find(Boolean);
    const haystack = norm(`${heading || ""} ${location.pathname}`);
    if (haystack.includes("voluntary") || haystack.includes("disclosure")) return "Voluntary Disclosures";
    if (haystack.includes("self identify")) return "Self Identify";
    if (haystack.includes("my experience") || haystack.includes("experience")) return "My Experience";
    if (haystack.includes("application question")) return "Application Questions";
    if (haystack.includes("my information") || haystack.includes("information")) return "My Information";
    if (haystack.includes("review")) return "Review";
    return heading || "Application";
  }

  function isWorkdayApplication() {
    if (/myworkdayjobs\.com|myworkdaysite\.com|\.workday\.com/i.test(location.hostname)) return true;
    return Boolean(document.querySelector("[data-automation-id]"));
  }

  // ----------------------------------------------------------------------------------
  // entry point
  // ----------------------------------------------------------------------------------

  /**
   * Fill everything on the current page that the profile has an answer for.
   *
   * Returns a report instead of mutating anything the caller can see, so the side panel
   * and the in-page button can render the same result.
   */
  async function run(profile, settings = {}) {
    const report = { step: detectStep(), filled: [], skipped: [], notFound: [], warnings: [] };
    if (!isWorkdayApplication()) {
      report.warnings.push("This page does not look like a Workday application form.");
      return report;
    }

    const options = { overwrite: Boolean(settings.overwrite) };
    const claimed = new Set();

    let specs = PAGE_SPECS;
    if (!settings.fillVoluntary) {
      specs = specs.filter((spec) => spec.section !== "voluntary" && spec.section !== "selfIdentify");
      if (report.step === "Voluntary Disclosures" || report.step === "Self Identify") {
        report.warnings.push(
          "Voluntary disclosure answers are turned off in your profile, so this page was left blank."
        );
      }
    }

    await fillSpecs(specs, document, profile, report, { claimed, options });

    // Work experience.
    const workSection = findSection({
      auto: ["workExperienceSection", "Work-Experience"],
      titles: ["work experience", "employment history"],
    });
    if (workSection) {
      await fillRepeating({
        section: workSection,
        prefixes: ["workExperience"],
        entries: profile.work.filter((entry) => entry.title || entry.company),
        specs: WORK_SPECS,
        name: "Work experience",
        report,
        claimed,
        options,
      });
    }

    // Education.
    const educationSection = findSection({
      auto: ["educationSection", "Education"],
      titles: ["education"],
    });
    if (educationSection) {
      await fillRepeating({
        section: educationSection,
        prefixes: ["education"],
        entries: profile.education.filter((entry) => entry.school),
        specs: EDUCATION_SPECS,
        name: "Education",
        report,
        claimed,
        options,
      });
    }

    // Websites.
    const websiteSection = findSection({
      auto: ["websitePanelSet", "websiteSection"],
      titles: ["websites", "social network urls"],
    });
    const websites = [profile.links.portfolio, profile.links.github, ...(profile.links.other || [])].filter(Boolean);
    if (websiteSection && websites.length) {
      await fillRepeating({
        section: websiteSection,
        prefixes: ["website"],
        entries: websites,
        specs: WEBSITE_SPECS,
        name: "Website",
        report,
        claimed,
        options,
      });
    }

    // Skills: a single multi-select, but only worth attempting on the experience step.
    const skillsField = [...document.querySelectorAll("[data-automation-id]")].find((element) => {
      const id = norm(automationId(element));
      return (id === "skills" || id.includes("skillsmultiselect")) && isVisible(element);
    });
    if (skillsField && profile.skills?.length) {
      const result = await selectMultiSelect(skillsField, profile.skills);
      if (result.ok) report.filled.push({ field: "Skills", detail: result.detail });
      else report.skipped.push({ field: "Skills", reason: result.detail });
    }

    // Resume upload.
    if (profile.documents?.resume?.base64) {
      const uploads = [...document.querySelectorAll('input[type="file"]')];
      const resumeInput =
        uploads.find((input) => norm(`${automationId(input)} ${ns.groupText(input)}`).includes("resume")) || uploads[0];
      if (resumeInput) {
        const alreadyUploaded = document.querySelector('[data-automation-id="file-upload-item"], [data-automation-id="fileUploadItem"]');
        if (alreadyUploaded && !settings.overwrite) {
          report.skipped.push({ field: "Resume", reason: "a file is already attached" });
        } else {
          const result = await attachFile(resumeInput, profile.documents.resume);
          if (result.ok) report.filled.push({ field: "Resume", detail: result.detail });
          else report.skipped.push({ field: "Resume", reason: result.detail });
        }
      }
    }

    if (!report.filled.length && !report.skipped.length) {
      report.warnings.push("No fields on this page matched your profile. Try the next step of the form.");
    }
    return report;
  }

  Object.assign(ns, {
    parseDate,
    detectStep,
    isWorkdayApplication,
    findSection,
    panelsIn,
    findAddButton,
    run,
  });
})(globalThis);
