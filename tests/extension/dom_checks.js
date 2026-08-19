"use strict";

// End-to-end checks for the autofill engine against synthetic Workday forms.
//
// These need a DOM, so they are opt-in: `npm install` in this directory pulls in jsdom
// and `pytest tests/extension` picks them up automatically. Without it the suite skips
// and `tests/extension/autofill_checks.js` still covers the engine's decision logic.
//
// The fixtures reproduce the markup shapes that make Workday hard — a portal-rendered
// listbox, a typeahead that only shows options after input, split date spinbuttons,
// numbered repeating panels behind an Add button — using the same `data-automation-id`
// vocabulary the real forms use. They are not a Workday replica; they are the contract
// the drivers are written against.

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const { JSDOM } = require("jsdom");

const EXTENSION = path.join(__dirname, "..", "..", "extension");
const ENGINE = ["profile/schema.js", "autofill/dom.js", "autofill/widgets.js", "autofill/fields.js", "autofill/runner.js"];

/**
 * Install a fresh document, then load the engine into this process against it.
 *
 * jsdom does no layout, so every rect is zero and the engine's visibility test would
 * reject the entire page; a stub rect is the one lie these fixtures tell.
 */
function freshDocument(url = "about:blank") {
  const { window } = new JSDOM("<!doctype html><html><body></body></html>", {
    pretendToBeVisual: true,
    url,
  });
  window.Element.prototype.getBoundingClientRect = () => ({ width: 120, height: 30, top: 10, left: 10 });
  window.Element.prototype.scrollIntoView = function () {};
  if (!window.PointerEvent) window.PointerEvent = window.MouseEvent;
  Object.assign(globalThis, {
    window,
    document: window.document,
    location: window.location,
    getComputedStyle: window.getComputedStyle.bind(window),
    MutationObserver: window.MutationObserver,
    CSS: window.CSS || { escape: (value) => value.replace(/([^\w-])/g, "\\$1") },
    Node: window.Node,
    Event: window.Event,
    MouseEvent: window.MouseEvent,
    PointerEvent: window.PointerEvent,
    KeyboardEvent: window.KeyboardEvent,
    FocusEvent: window.FocusEvent,
    HTMLInputElement: window.HTMLInputElement,
    HTMLTextAreaElement: window.HTMLTextAreaElement,
    HTMLSelectElement: window.HTMLSelectElement,
  });
  return window;
}

freshDocument();
for (const file of ENGINE) {
  vm.runInThisContext(fs.readFileSync(path.join(EXTENSION, file), "utf8"), { filename: file });
}
const { JMEProfile, JMEAutofill } = globalThis;

// ------------------------------------------------------------------------------------
// fixture builders
// ------------------------------------------------------------------------------------

const field = (automationId, label) =>
  `<div><label id="lbl-${automationId}">${label}</label>
   <input type="text" data-automation-id="${automationId}" aria-labelledby="lbl-${automationId}"></div>`;

const at = (automationId, scope = document) => scope.querySelector(`[data-automation-id="${automationId}"]`);

/** Wire a trigger to open a portal-rendered listbox, the way Workday's dropdowns do. */
function wireDropdown(trigger, values) {
  if (trigger.dataset.wired) return;
  trigger.dataset.wired = "1";
  trigger.addEventListener("click", () => {
    if (document.querySelector('[data-automation-widget="wd-popup"]')) return;
    const popup = document.createElement("div");
    popup.setAttribute("data-automation-widget", "wd-popup");
    popup.innerHTML = `<ul role="listbox">${values.map((value) => `<li role="option" data-automation-id="menuItem">${value}</li>`).join("")}</ul>`;
    for (const option of popup.querySelectorAll('[role="option"]')) {
      option.addEventListener("click", () => {
        trigger.textContent = option.textContent;
        popup.remove();
      });
    }
    document.body.appendChild(popup);
  });
}

/** Wire a typeahead that renders prompt options only once something has been typed. */
function wireTypeahead(container, values) {
  const input = container.querySelector("input");
  input.addEventListener("input", () => {
    document.querySelectorAll('[data-automation-widget="prompt"]').forEach((node) => node.remove());
    const query = input.value.trim().toLowerCase();
    if (!query) return;
    const matches = values.filter((value) => value.toLowerCase().includes(query));
    if (!matches.length) return;
    const popup = document.createElement("div");
    popup.setAttribute("data-automation-widget", "prompt");
    popup.innerHTML = matches
      .map((value) => `<div role="option" data-automation-id="promptOption">${value}</div>`)
      .join("");
    for (const option of popup.querySelectorAll('[role="option"]')) {
      option.addEventListener("click", () => {
        const chip = document.createElement("li");
        chip.setAttribute("data-automation-id", "selectedItem");
        chip.textContent = option.textContent;
        container.appendChild(chip);
        popup.remove();
      });
    }
    document.body.appendChild(popup);
  });
}

/** Record every click on the wizard's navigation, so "never submits" can be asserted. */
function wireNavigation(record) {
  for (const id of ["bottom-navigation-next-button", "bottom-navigation-submit-button"]) {
    at(id)?.addEventListener("click", () => record.push(id));
  }
}

// ------------------------------------------------------------------------------------
// tests
// ------------------------------------------------------------------------------------

const tests = [];
const test = (name, body) => tests.push([name, body]);

const TENANT = "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/apply";

test("My Information: names, contact, address, dropdowns, and the source typeahead", async () => {
  freshDocument(TENANT);
  document.body.innerHTML = `
    <h2 data-automation-id="pageHeader">My Information</h2>
    <div data-automation-id="legalNameSection"><h3>Legal Name</h3>
      ${field("legalNameSection_firstName", "First Name")}
      ${field("legalNameSection_middleName", "Middle Name")}
      ${field("legalNameSection_lastName", "Last Name")}</div>
    <div data-automation-id="addressSection"><h3>Address</h3>
      ${field("addressSection_addressLine1", "Address Line 1")}
      ${field("addressSection_city", "City")}
      ${field("addressSection_postalCode", "Postal Code")}
      <div><label id="lbl-state">State</label>
        <button type="button" data-automation-id="addressSection_countryRegion" aria-haspopup="listbox" aria-labelledby="lbl-state">Select One</button></div></div>
    <div data-automation-id="contactInformationSection"><h3>Contact Information</h3>
      ${field("email", "Email Address")}
      ${field("phone-number", "Phone Number")}
      <div><label id="lbl-devtype">Phone Device Type</label>
        <button type="button" data-automation-id="phone-device-type" aria-haspopup="listbox" aria-labelledby="lbl-devtype">Select One</button></div></div>
    <div data-automation-id="sourceSection"><h3>How Did You Hear About Us?</h3>
      <div data-automation-id="source"><input type="text" aria-label="How Did You Hear About Us?"></div></div>
    <fieldset data-automation-id="previousWorker" role="radiogroup"><legend>Have you previously worked here?</legend>
      <label><input type="radio" name="prev" value="yes"> Yes</label>
      <label><input type="radio" name="prev" value="no"> No</label></fieldset>
    <button type="button" data-automation-id="bottom-navigation-next-button">Save and Continue</button>`;

  // Three states share a prefix; only exact-then-prefix scoring picks the right one.
  wireDropdown(at("addressSection_countryRegion"), ["Alabama", "Michigan", "Minnesota", "Mississippi"]);
  wireDropdown(at("phone-device-type"), ["Home", "Mobile", "Work"]);
  wireTypeahead(at("source"), ["Company Website", "LinkedIn", "Job Board", "Referral"]);
  const navigation = [];
  wireNavigation(navigation);

  const profile = JMEProfile.normalize({
    personal: {
      firstName: "Adil", middleName: "R", lastName: "Mallick",
      email: "mallick9@msu.edu", phone: "5551234567", phoneType: "Mobile",
      address1: "123 Grand River Ave", city: "East Lansing", state: "Michigan", postalCode: "48823",
    },
    application: { howHeard: ["LinkedIn"], previouslyEmployed: "No" },
  });
  const report = await JMEAutofill.run(profile, profile.preferences);

  assert.equal(report.step, "My Information");
  assert.equal(report.host, "acme.wd1.myworkdayjobs.com",
    "the report names the tenant, so a diagnostic says which Workday it came from");
  assert.equal(at("legalNameSection_firstName").value, "Adil");
  assert.equal(at("legalNameSection_middleName").value, "R");
  assert.equal(at("legalNameSection_lastName").value, "Mallick");
  assert.equal(at("email").value, "mallick9@msu.edu");
  assert.equal(at("phone-number").value, "5551234567");
  assert.equal(at("addressSection_addressLine1").value, "123 Grand River Ave");
  assert.equal(at("addressSection_city").value, "East Lansing");
  assert.equal(at("addressSection_postalCode").value, "48823");
  assert.equal(at("addressSection_countryRegion").textContent, "Michigan",
    "Michigan must win over Minnesota and Mississippi");
  assert.equal(at("phone-device-type").textContent, "Mobile");
  assert.equal(at("selectedItem").textContent, "LinkedIn");
  assert.equal(document.querySelector('input[name="prev"][value="no"]').checked, true);
  assert.equal(document.querySelector('input[name="prev"][value="yes"]').checked, false);
  assert.deepEqual(navigation, []);
  assert.deepEqual(report.skipped, []);
});

test("My Information: a field the form already filled is left alone unless overwriting", async () => {
  freshDocument(TENANT);
  document.body.innerHTML = `<h2 data-automation-id="pageHeader">My Information</h2>
    ${field("legalNameSection_firstName", "First Name")}
    ${field("email", "Email Address")}`;
  at("legalNameSection_firstName").value = "Parsed From Resume";

  const source = { personal: { firstName: "Adil", lastName: "Mallick", email: "mallick9@msu.edu" } };
  let profile = JMEProfile.normalize(source);
  let report = await JMEAutofill.run(profile, profile.preferences);
  assert.equal(at("legalNameSection_firstName").value, "Parsed From Resume");
  assert.ok(report.skipped.some((item) => item.field === "firstName" && item.reason.includes("kept existing")),
    "the user has to be told the value was kept");
  assert.equal(at("email").value, "mallick9@msu.edu", "empty fields are still filled");

  profile = JMEProfile.normalize({ ...source, preferences: { overwrite: true } });
  await JMEAutofill.run(profile, profile.preferences);
  assert.equal(at("legalNameSection_firstName").value, "Adil");
});

test("My Experience: repeating panels, split dates, degree aliases, and skills", async () => {
  freshDocument(TENANT);
  const workPanel = (index) => `<div data-automation-id="workExperience-${index}" role="group">
    <div><label id="w${index}t">Job Title</label><input type="text" data-automation-id="jobTitle" aria-labelledby="w${index}t"></div>
    <div><label id="w${index}c">Company</label><input type="text" data-automation-id="company" aria-labelledby="w${index}c"></div>
    <div><label id="w${index}l">Location</label><input type="text" data-automation-id="location" aria-labelledby="w${index}l"></div>
    <label><input type="checkbox" data-automation-id="currentlyWorkHere"> I currently work here</label>
    <div data-automation-id="startDate" role="group"><span>From</span>
      <input data-automation-id="dateSectionMonth-input" aria-label="Month">
      <input data-automation-id="dateSectionYear-input" aria-label="Year"></div>
    <div data-automation-id="endDate" role="group"><span>To</span>
      <input data-automation-id="dateSectionMonth-input" aria-label="Month">
      <input data-automation-id="dateSectionYear-input" aria-label="Year"></div>
    <div><label id="w${index}d">Role Description</label><textarea data-automation-id="roleDescription" aria-labelledby="w${index}d"></textarea></div>
  </div>`;
  const educationPanel = (index) => `<div data-automation-id="education-${index}" role="group">
    <div><label id="e${index}s">School or University</label><input type="text" data-automation-id="school" aria-labelledby="e${index}s"></div>
    <div><label id="e${index}g">Degree</label>
      <button type="button" data-automation-id="degree" aria-haspopup="listbox" aria-labelledby="e${index}g">Select One</button></div>
    <div data-automation-id="formField-field-of-study"><label id="e${index}f">Field of Study</label>
      <input type="text" data-automation-id="field-of-study" aria-labelledby="e${index}f"></div>
    <div><label id="e${index}r">Overall Result (GPA)</label><input type="text" data-automation-id="gpa" aria-labelledby="e${index}r"></div>
  </div>`;

  document.body.innerHTML = `
    <h2 data-automation-id="pageHeader">My Experience</h2>
    <div data-automation-id="workExperienceSection"><h3>Work Experience</h3>
      <div id="work-panels"></div><button type="button" data-automation-id="Add">Add</button></div>
    <div data-automation-id="educationSection"><h3>Education</h3>
      <div id="education-panels"></div><button type="button" data-automation-id="Add">Add</button></div>
    <div data-automation-id="skillsSection"><h3>Skills</h3>
      <div data-automation-id="skills"><input type="text" aria-label="Skills"></div></div>
    <button type="button" data-automation-id="bottom-navigation-next-button">Save and Continue</button>`;

  const degrees = ["Associate's Degree", "Bachelor's Degree (BA/BS)", "Master's Degree", "Doctorate (PhD)"];
  const counters = { work: 1, education: 1 };
  for (const [section, list, builder, key] of [
    ["workExperienceSection", "work-panels", workPanel, "work"],
    ["educationSection", "education-panels", educationPanel, "education"],
  ]) {
    at("Add", at(section)).addEventListener("click", () => {
      document.getElementById(list).insertAdjacentHTML("beforeend", builder(counters[key]));
      counters[key] += 1;
      for (const trigger of document.querySelectorAll('[data-automation-id="degree"]')) {
        wireDropdown(trigger, degrees);
      }
    });
  }
  wireTypeahead(at("skills"), ["Python", "PostgreSQL", "FastAPI", "Kubernetes"]);
  const navigation = [];
  wireNavigation(navigation);

  const profile = JMEProfile.normalize({
    personal: { firstName: "Adil", lastName: "Mallick", email: "mallick9@msu.edu" },
    work: [
      { title: "Software Engineer Intern", company: "HERE Technologies", location: "Chicago, IL",
        startMonth: "6", startYear: "2026", endMonth: "8", endYear: "2026", description: "Built things." },
      { title: "Research Assistant", company: "Michigan State University", location: "East Lansing, MI",
        startMonth: "1", startYear: "2025", current: true, description: "Ongoing work." },
    ],
    education: [{ school: "Michigan State University", degree: "Bachelor's Degree", fieldOfStudy: "Computer Science", gpa: "3.8" }],
    skills: ["Python", "PostgreSQL", "Fortran"],
  });
  const report = await JMEAutofill.run(profile, profile.preferences);

  const panels = [...document.querySelectorAll('[data-automation-id^="workExperience-"]')];
  assert.equal(panels.length, 2, "Add must be clicked until there is a panel per job");
  assert.equal(at("jobTitle", panels[0]).value, "Software Engineer Intern");
  assert.equal(at("company", panels[0]).value, "HERE Technologies");
  assert.equal(at("location", panels[0]).value, "Chicago, IL");
  assert.equal(at("roleDescription", panels[0]).value, "Built things.");
  assert.equal(at("startDate", panels[0]).querySelector('[data-automation-id="dateSectionMonth-input"]').value, "06");
  assert.equal(at("startDate", panels[0]).querySelector('[data-automation-id="dateSectionYear-input"]').value, "2026");
  assert.equal(at("endDate", panels[0]).querySelector('[data-automation-id="dateSectionMonth-input"]').value, "08");
  assert.equal(at("company", panels[1]).value, "Michigan State University");
  assert.equal(at("currentlyWorkHere", panels[1]).checked, true);
  assert.equal(at("endDate", panels[1]).querySelector('[data-automation-id="dateSectionYear-input"]').value, "",
    "a current job leaves its end date empty");

  const education = document.querySelector('[data-automation-id^="education-"]');
  assert.equal(at("school", education).value, "Michigan State University");
  assert.equal(at("degree", education).textContent, "Bachelor's Degree (BA/BS)",
    "the alias table has to bridge the tenant's qualified wording");
  assert.equal(at("field-of-study", education).value, "Computer Science");
  assert.equal(at("gpa", education).value, "3.8");
  assert.ok(!report.skipped.some((item) => item.field.includes("fieldOfStudy")),
    "the multi-select spelling failing is not a skip when the text spelling filled");

  const chips = [...document.querySelectorAll('[data-automation-id="selectedItem"]')].map((chip) => chip.textContent);
  assert.deepEqual(chips, ["Python", "PostgreSQL"]);
  const skills = report.filled.find((item) => item.field === "Skills");
  assert.ok(skills.detail.includes("Fortran"), "a skill the tenant does not offer is reported, not forced");
  assert.deepEqual(navigation, []);
});

test("My Experience: the engine reports when the form will not open enough panels", async () => {
  freshDocument(TENANT);
  document.body.innerHTML = `
    <h2 data-automation-id="pageHeader">My Experience</h2>
    <div data-automation-id="workExperienceSection"><h3>Work Experience</h3>
      <div data-automation-id="workExperience-1" role="group">
        <div><label id="w1t">Job Title</label><input type="text" data-automation-id="jobTitle" aria-labelledby="w1t"></div>
      </div>
      <button type="button" data-automation-id="Add">Add</button></div>`;
  // The Add button is wired to nothing, standing in for a tenant that caps its entries.

  const profile = JMEProfile.normalize({
    personal: { firstName: "Adil", lastName: "Mallick", email: "mallick9@msu.edu" },
    work: [{ title: "One", company: "A" }, { title: "Two", company: "B" }, { title: "Three", company: "C" }],
  });
  const report = await JMEAutofill.run(profile, profile.preferences);
  assert.equal(at("jobTitle").value, "One");
  assert.ok(report.warnings.some((warning) => warning.includes("1 of 3")),
    `expected a shortfall warning, got: ${report.warnings.join(" | ")}`);
});

test("Voluntary Disclosures: skipped by default, filled only on opt-in", async () => {
  const answers = {
    personal: { firstName: "Adil", lastName: "Mallick", email: "mallick9@msu.edu" },
    voluntary: { gender: "I do not wish to answer", veteranStatus: "I am not a protected veteran" },
  };
  const veteran = [
    "I identify as one or more of the classifications of a protected veteran",
    "I am not a protected veteran",
    "I do not wish to answer",
  ];
  const build = () => {
    freshDocument(TENANT);
    document.body.innerHTML = `
      <h2 data-automation-id="pageHeader">Voluntary Disclosures</h2>
      <div><label id="lbl-gender">Gender</label>
        <button type="button" data-automation-id="gender" aria-haspopup="listbox" aria-labelledby="lbl-gender">Select One</button></div>
      <div><label id="lbl-vet">Veteran Status</label>
        <button type="button" data-automation-id="veteranStatus" aria-haspopup="listbox" aria-labelledby="lbl-vet">Select One</button></div>
      <label><input type="checkbox" data-automation-id="agreementCheckbox">
        I have read and I agree to the Terms and Conditions of this application, and I certify
        that the information I have provided is true and complete to the best of my knowledge.</label>
      <button type="button" data-automation-id="bottom-navigation-next-button">Save and Continue</button>
      <button type="button" data-automation-id="bottom-navigation-submit-button">Submit</button>`;
    wireDropdown(at("gender"), ["Male", "Female", "I do not wish to answer"]);
    wireDropdown(at("veteranStatus"), veteran);
    const navigation = [];
    wireNavigation(navigation);
    return navigation;
  };

  let navigation = build();
  let profile = JMEProfile.normalize(answers);
  assert.equal(profile.preferences.fillVoluntary, false, "opt-in is the default");
  let report = await JMEAutofill.run(profile, profile.preferences);
  assert.equal(at("gender").textContent, "Select One");
  assert.equal(at("agreementCheckbox").checked, false);
  assert.ok(report.warnings.some((warning) => warning.includes("turned off")),
    "a blank page has to explain itself");
  assert.deepEqual(navigation, []);

  navigation = build();
  profile = JMEProfile.normalize({ ...answers, preferences: { fillVoluntary: true, acceptTerms: true } });
  await JMEAutofill.run(profile, profile.preferences);
  assert.equal(at("gender").textContent, "I do not wish to answer");
  assert.equal(at("veteranStatus").textContent, "I am not a protected veteran",
    "the negative answer must not lose to the positive one it shares words with");
  assert.equal(at("agreementCheckbox").checked, true,
    "a consent label of legal-paragraph length still has to match");
  assert.deepEqual(navigation, [], "the engine must never click Next or Submit");
});

test("a page that is not a Workday form is reported, not guessed at", async () => {
  freshDocument();
  document.body.innerHTML = "<h1>Some blog post</h1><input type='text' aria-label='First Name'>";
  const profile = JMEProfile.normalize({
    personal: { firstName: "Adil", lastName: "Mallick", email: "mallick9@msu.edu" },
  });
  const report = await JMEAutofill.run(profile, profile.preferences);
  assert.deepEqual(report.filled, []);
  assert.ok(report.warnings.some((warning) => warning.includes("does not look like")));
});

// ------------------------------------------------------------------------------------

(async () => {
  let failures = 0;
  for (const [name, body] of tests) {
    try {
      await body();
      console.log(`  ok   ${name}`);
    } catch (error) {
      failures += 1;
      console.error(`  FAIL ${name}\n       ${error.message}`);
    }
  }
  console.log(`\n${tests.length - failures} passed, ${failures} failed`);
  process.exit(failures ? 1 : 0);
})();
