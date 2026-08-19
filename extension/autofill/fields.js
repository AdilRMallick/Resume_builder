"use strict";

// What each field on a Workday application means, and how to find it in the DOM.
//
// Tenants customise their Workday instance, so no single selector works everywhere. Each
// spec therefore carries three layers of evidence: exact `data-automation-id` values seen
// across tenants, keyword phrases matched against the control's accessible name, and
// negative keywords that rule a control out. A control has to clear the negatives and
// win on score before anything is typed into it, and every control can only be claimed
// once per pass, so a near-miss cannot steal a field its real owner would have taken.

(function (global) {
  const ns = (global.JMEAutofill = global.JMEAutofill || {});
  const { norm, tokens, isVisible, automationId, signature, groupText, matchScore } = ns;

  const CANDIDATES = {
    text: 'input[type="text"], input[type="email"], input[type="tel"], input[type="url"], input[type="number"], input:not([type]), textarea',
    dropdown:
      'select, button[aria-haspopup], [role="combobox"]:not(input), [data-automation-id$="Dropdown"], [data-automation-id$="-dropdown"]',
    multiselect:
      '[data-automation-id="multiSelectContainer"], [data-automation-id*="multiselect" i], [data-automation-id*="multiSelect"]',
    date: '[data-automation-id="dateInputWrapper"], [data-automation-id*="dateWidget" i], [data-automation-id*="datePicker" i], input[type="date"]',
    checkbox: 'input[type="checkbox"], [role="checkbox"]',
    radiogroup: '[role="radiogroup"], fieldset, [data-automation-id*="radio" i]',
    file: 'input[type="file"]',
  };

  // Widgets with no label of their own (a radio group, a date wrapper, a bare checkbox)
  // carry their question in the surrounding markup, so those kinds match on context too.
  const CONTEXTUAL = new Set(["radiogroup", "checkbox", "date", "multiselect", "file"]);

  /**
   * Score how well `element` answers to `spec`, or 0 when it must not be used.
   *
   * The tiers are deliberately far apart. An exact automation id is near-certain and
   * should never lose to a keyword coincidence, so it scores an order of magnitude
   * higher than the best phrase match can reach.
   */
  function score(element, spec) {
    const id = norm(automationId(element));
    const group = norm(groupText(element));
    const sig = CONTEXTUAL.has(spec.kind)
      ? norm([signature(element), group, (element.textContent || "").slice(0, 300)].join(" "))
      : signature(element);

    if (spec.auto?.some((wanted) => id === norm(wanted))) return 10000;

    for (const excluded of spec.not || []) {
      if (sig.includes(norm(excluded))) return 0;
    }

    let best = 0;
    for (const wanted of spec.auto || []) {
      const target = norm(wanted);
      if (target && id.includes(target)) best = Math.max(best, 5000 - (id.length - target.length));
    }
    for (const phrase of spec.any || []) {
      const needed = tokens(phrase);
      if (!needed.length) continue;
      if (needed.every((token) => sig.includes(token))) {
        // The length penalty only breaks ties between similar labels, so it is capped:
        // a consent checkbox carries a paragraph of legal text and would otherwise score
        // itself out of contention entirely.
        best = Math.max(best, 1000 + needed.length * 50 - Math.min(sig.length, 400) / 4);
      }
    }
    if (!best) return 0;

    // A field that names its own group ("Country" under "Address") beats the same label
    // sitting under an unrelated heading.
    if (spec.group?.some((phrase) => group.includes(norm(phrase)))) best += 400;
    if (spec.notGroup?.some((phrase) => group.includes(norm(phrase)))) best -= 900;
    return Math.max(best, 0);
  }

  function query(scope, selector) {
    try {
      return [...scope.querySelectorAll(selector)].filter(isVisible);
    } catch {
      return [];
    }
  }

  function candidates(scope, kind) {
    const declared = query(scope, CANDIDATES[kind] || CANDIDATES.text);
    if (kind === "multiselect") return unique([...declared, ...derivedMultiSelects(scope)]);
    if (kind === "date") return unique([...declared, ...derivedDateWrappers(scope)]);
    return declared;
  }

  const unique = (nodes) => [...new Set(nodes)];

  /** Visible form controls inside `element`, used to judge how tight a wrapper is. */
  const controlsIn = (element) => query(element, "input, textarea, select");

  /**
   * Multi-selects whose container is named after the question rather than the widget.
   *
   * Not every tenant marks the container `multiSelectContainer`; "How Did You Hear About
   * Us?" is often just `data-automation-id="source"`. The derived container has to be the
   * tightest labelled wrapper around exactly one input, so a whole section — or a work
   * experience panel, which also contains a matching label — can never stand in for one.
   */
  function derivedMultiSelects(scope) {
    const found = [];
    for (const input of query(scope, 'input[type="text"], input:not([type]), input[role="combobox"]')) {
      const container = input.parentElement?.closest("[data-automation-id]");
      if (container && controlsIn(container).length === 1) found.push(container);
    }
    return found;
  }

  /** Date fields found through their month/day/year spinbuttons rather than a wrapper. */
  function derivedDateWrappers(scope) {
    const found = [];
    for (const section of query(scope, '[data-automation-id^="dateSection"]')) {
      const wrapper = section.parentElement?.closest('[data-automation-id], [role="group"], fieldset');
      if (wrapper && !found.includes(wrapper)) found.push(wrapper);
    }
    return found;
  }

  /**
   * Assign at most one control to each spec, best pairing first.
   *
   * Greedy over the whole score matrix rather than per spec in order: the strongest
   * evidence anywhere on the page gets to claim its control before a weaker spec can.
   */
  function resolveAll(specs, scope, { claimed = new Set() } = {}) {
    const pairs = [];
    for (const spec of specs) {
      for (const element of candidates(scope, spec.kind)) {
        if (claimed.has(element)) continue;
        const value = score(element, spec);
        if (value > 0) pairs.push({ spec, element, value });
      }
    }
    pairs.sort((left, right) => right.value - left.value);

    const resolved = new Map();
    for (const pair of pairs) {
      if (resolved.has(pair.spec.key) || claimed.has(pair.element)) continue;
      resolved.set(pair.spec.key, pair.element);
      claimed.add(pair.element);
    }
    return resolved;
  }

  // ----------------------------------------------------------------------------------
  // value aliases
  // ----------------------------------------------------------------------------------

  // Tenants word the same option differently ("Bachelor's Degree" vs "Bachelors (BA/BS)"),
  // so a stored answer ships with the spellings it is also known by.
  const DEGREE_ALIASES = {
    "high school diploma": ["High School", "High School or equivalent", "Secondary Education"],
    "associate's degree": ["Associate", "Associates Degree", "Associate (AA/AS)"],
    "bachelor's degree": ["Bachelors", "Bachelor", "Bachelor's (BA/BS)", "Bachelors Degree", "Undergraduate"],
    "master's degree": ["Masters", "Master", "Master's (MA/MS)", "Masters Degree", "Graduate"],
    "doctorate": ["PhD", "Doctoral Degree", "Doctorate (PhD)"],
  };

  const YES_NO = (value) =>
    value === true || norm(value) === "yes" ? ["Yes", "Y", "True"] : ["No", "N", "False"];

  // Built once so a lookup uses the same normalisation as every other comparison in the
  // engine; the literal keys above are written for a human to read.
  const NORMALISED_DEGREES = new Map(
    Object.entries(DEGREE_ALIASES).map(([key, aliases]) => [norm(key), aliases])
  );

  const aliasesFor = (kind, value) => {
    if (kind === "degree") return NORMALISED_DEGREES.get(norm(value)) || [];
    if (kind === "yesno") return YES_NO(value);
    return [];
  };

  // ----------------------------------------------------------------------------------
  // specs
  // ----------------------------------------------------------------------------------

  /** Single-instance fields, tagged by the application step they usually appear on. */
  const PAGE_SPECS = [
    // --- name -----------------------------------------------------------------------
    { key: "firstName", kind: "text", section: "personal",
      auto: ["legalNameSection_firstName", "name--legalName--firstName", "firstName"],
      any: ["first name", "given name"], not: ["preferred", "middle", "last", "family"],
      value: (p) => p.personal.firstName },
    { key: "middleName", kind: "text", section: "personal",
      auto: ["legalNameSection_middleName", "middleName"],
      any: ["middle name", "middle initial"], not: ["preferred"],
      value: (p) => p.personal.middleName },
    { key: "lastName", kind: "text", section: "personal",
      auto: ["legalNameSection_lastName", "name--legalName--lastName", "lastName"],
      any: ["last name", "family name", "surname"], not: ["preferred", "first", "middle"],
      value: (p) => p.personal.lastName },
    { key: "preferredName", kind: "text", section: "personal",
      auto: ["preferredNameSection_firstName"], any: ["preferred name", "preferred first"],
      value: (p) => p.personal.preferredName },

    // --- contact --------------------------------------------------------------------
    { key: "email", kind: "text", section: "personal",
      auto: ["email", "emailAddress", "contactInformation_email"],
      any: ["email"], not: ["confirm", "verify", "re enter"],
      value: (p) => p.personal.email },
    { key: "phone", kind: "text", section: "personal",
      auto: ["phone-number", "phoneNumber", "phone--phoneNumber"],
      any: ["phone number", "telephone"], not: ["extension", "country", "device", "type"],
      value: (p) => p.personal.phone },
    { key: "phoneType", kind: "dropdown", section: "personal",
      auto: ["phone-device-type", "phoneType", "phone--phoneType"],
      any: ["phone device type", "phone type"],
      value: (p) => p.personal.phoneType },
    { key: "phoneCountryCode", kind: "dropdown", section: "personal",
      auto: ["countryPhoneCode", "phone--countryPhoneCode"],
      any: ["country phone code"],
      value: (p) => p.personal.phoneCountryCode },

    // --- address --------------------------------------------------------------------
    { key: "address1", kind: "text", section: "personal",
      auto: ["addressSection_addressLine1", "addressLine1"],
      any: ["address line 1", "street address"], not: ["line 2"],
      value: (p) => p.personal.address1 },
    { key: "address2", kind: "text", section: "personal",
      auto: ["addressSection_addressLine2", "addressLine2"],
      any: ["address line 2", "apartment", "suite"],
      value: (p) => p.personal.address2 },
    { key: "city", kind: "text", section: "personal",
      auto: ["addressSection_city", "city"], any: ["city", "town"], not: ["citizen"],
      value: (p) => p.personal.city },
    { key: "state", kind: "dropdown", section: "personal",
      auto: ["addressSection_countryRegion", "countryRegion", "addressSection_state"],
      any: ["state", "province", "region"], not: ["country of", "citizenship"],
      value: (p) => p.personal.state },
    { key: "postalCode", kind: "text", section: "personal",
      auto: ["addressSection_postalCode", "postalCode"],
      any: ["postal code", "zip code", "zip"],
      value: (p) => p.personal.postalCode },
    { key: "country", kind: "dropdown", section: "personal",
      auto: ["addressSection_countryDropdown", "country"],
      any: ["country"], not: ["phone", "citizenship", "region", "code"],
      group: ["address"],
      value: (p) => p.personal.country },

    // --- source ---------------------------------------------------------------------
    { key: "howHeard", kind: "multiselect", section: "personal",
      auto: ["source", "sourceSection_source", "sourceProspectQuestion"],
      any: ["how did you hear about us", "source"],
      value: (p) => p.application.howHeard },
    { key: "previouslyEmployed", kind: "radiogroup", section: "personal",
      auto: ["previousWorker", "candidateIsPreviousWorker"],
      any: ["previously worked", "former employee", "worked here before"],
      valueKind: "yesno",
      value: (p) => p.application.previouslyEmployed },

    // --- links ----------------------------------------------------------------------
    { key: "linkedin", kind: "text", section: "experience",
      auto: ["linkedinQuestion", "linkedIn"], any: ["linkedin"],
      value: (p) => p.links.linkedin },

    // --- application questions ------------------------------------------------------
    { key: "workAuthorized", kind: "radiogroup", section: "questions",
      any: ["legally authorized to work", "authorized to work", "work authorization"],
      not: ["sponsor"], valueKind: "yesno",
      value: (p) => p.application.workAuthorized },
    { key: "requireSponsorship", kind: "radiogroup", section: "questions",
      any: ["require sponsorship", "need sponsorship", "visa sponsorship", "immigration sponsorship"],
      valueKind: "yesno",
      value: (p) => p.application.requireSponsorship },
    { key: "willingToRelocate", kind: "radiogroup", section: "questions",
      any: ["willing to relocate", "open to relocation"], valueKind: "yesno",
      value: (p) => p.application.willingToRelocate },
    { key: "desiredSalary", kind: "text", section: "questions",
      any: ["desired salary", "salary expectation", "expected compensation", "desired compensation"],
      value: (p) => p.application.desiredSalary },
    { key: "availableStartDate", kind: "date", section: "questions",
      any: ["available start date", "earliest start date", "start date"], not: ["employment", "experience"],
      value: (p) => p.application.availableStartDate },

    // --- voluntary disclosures ------------------------------------------------------
    { key: "gender", kind: "dropdown", section: "voluntary",
      auto: ["gender", "personalInfoUS--gender"], any: ["gender"],
      value: (p) => p.voluntary.gender },
    { key: "ethnicity", kind: "dropdown", section: "voluntary",
      auto: ["ethnicity", "personalInfoUS--ethnicity"], any: ["ethnicity", "race"],
      not: ["hispanic"],
      value: (p) => p.voluntary.ethnicity },
    { key: "hispanicLatino", kind: "dropdown", section: "voluntary",
      auto: ["hispanicOrLatino"], any: ["hispanic or latino", "hispanic"], valueKind: "yesno",
      value: (p) => p.voluntary.hispanicLatino },
    { key: "veteranStatus", kind: "dropdown", section: "voluntary",
      auto: ["veteranStatus", "personalInfoUS--veteranStatus"],
      any: ["veteran status", "protected veteran", "veteran"],
      value: (p) => p.voluntary.veteranStatus },
    { key: "disability", kind: "dropdown", section: "voluntary",
      auto: ["disability", "selfIdentifiedDisabilityData--disability"],
      any: ["disability status", "disability"],
      value: (p) => p.voluntary.disability },
    { key: "voluntaryTerms", kind: "checkbox", section: "voluntary",
      auto: ["agreementCheckbox", "termsAndConditions", "acceptTermsAndAgreements"],
      any: ["i agree", "i have read", "terms and conditions", "acknowledge"],
      value: (p) => (p.preferences.acceptTerms ? true : null) },

    // --- self identify --------------------------------------------------------------
    { key: "selfIdentifyName", kind: "text", section: "selfIdentify",
      auto: ["name--legalName--firstName", "selfIdentifiedDisabilityData--name"],
      any: ["your name", "employee name"],
      value: (p) => `${p.personal.firstName} ${p.personal.lastName}`.trim() },
    { key: "selfIdentifyDate", kind: "date", section: "selfIdentify",
      auto: ["dateSignedOn", "selfIdentifiedDisabilityData--dateSignedOn"],
      any: ["today s date", "date signed"],
      value: () => "today" },
  ];

  /** Fields inside one Work Experience panel. */
  const WORK_SPECS = [
    { key: "title", kind: "text", auto: ["jobTitle"], any: ["job title", "title"], not: ["company"],
      value: (entry) => entry.title },
    { key: "company", kind: "text", auto: ["company"], any: ["company", "employer", "organization"],
      value: (entry) => entry.company },
    { key: "location", kind: "text", auto: ["location"], any: ["location"],
      value: (entry) => entry.location },
    { key: "current", kind: "checkbox", auto: ["currentlyWorkHere"],
      any: ["i currently work here", "currently work here", "current position"],
      value: (entry) => entry.current },
    { key: "startDate", kind: "date", auto: ["startDate", "dateSectionMonth-display"],
      any: ["from", "start date"], not: ["to ", "end"],
      value: (entry) => ({ month: entry.startMonth, year: entry.startYear }) },
    { key: "endDate", kind: "date", auto: ["endDate"], any: ["to", "end date"], not: ["start"],
      value: (entry) => (entry.current ? null : { month: entry.endMonth, year: entry.endYear }) },
    { key: "description", kind: "text", auto: ["roleDescription", "description"],
      any: ["role description", "description", "responsibilities"],
      value: (entry) => entry.description },
  ];

  // Some fields are a text box in one tenant and a picker in another. Specs that share an
  // `alt` group are alternate spellings of one answer: the first that fills wins, and the
  // others are neither retried nor reported as missing.
  /** Fields inside one Education panel. */
  const EDUCATION_SPECS = [
    { key: "school", kind: "text", auto: ["school", "schoolName", "institution"],
      any: ["school", "university", "college", "institution"], not: ["degree", "study"],
      value: (entry) => entry.school },
    { key: "schoolDropdown", kind: "dropdown", alt: "school", auto: ["school", "schoolItem"],
      any: ["school", "university"], not: ["degree", "study"],
      value: (entry) => entry.school },
    { key: "degree", kind: "dropdown", auto: ["degree"], any: ["degree"], valueKind: "degree",
      value: (entry) => entry.degree },
    { key: "fieldOfStudy", kind: "multiselect", auto: ["field-of-study", "fieldOfStudy"],
      any: ["field of study", "major"],
      value: (entry) => (entry.fieldOfStudy ? [entry.fieldOfStudy] : null) },
    { key: "fieldOfStudyText", kind: "text", alt: "fieldOfStudy", auto: ["field-of-study", "fieldOfStudy"],
      any: ["field of study", "major"],
      value: (entry) => entry.fieldOfStudy },
    { key: "gpa", kind: "text", auto: ["gpa", "overallResult"], any: ["gpa", "grade average"],
      value: (entry) => entry.gpa },
    { key: "startYear", kind: "date", auto: ["firstYearAttended", "startDate"],
      any: ["from", "first year attended", "start"], not: ["last", "end"],
      value: (entry) => (entry.startYear ? { year: entry.startYear } : null) },
    { key: "endYear", kind: "date", auto: ["lastYearAttended", "endDate"],
      any: ["to", "last year attended", "end"], not: ["first", "start"],
      value: (entry) => (entry.endYear ? { year: entry.endYear } : null) },
  ];

  /** Fields inside one Website panel. */
  const WEBSITE_SPECS = [
    { key: "url", kind: "text", auto: ["website"], any: ["url", "website", "web address"],
      value: (entry) => entry },
  ];

  Object.assign(ns, {
    CANDIDATES,
    PAGE_SPECS,
    WORK_SPECS,
    EDUCATION_SPECS,
    WEBSITE_SPECS,
    DEGREE_ALIASES,
    aliasesFor,
    score,
    candidates,
    resolveAll,
  });
})(globalThis);
