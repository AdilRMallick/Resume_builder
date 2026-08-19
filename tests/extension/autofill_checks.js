"use strict";

// Behaviour checks for the parts of the autofill engine that are pure logic: profile
// normalisation, resume-profile seeding, date parsing, and option matching.
//
// The engine's DOM drivers need a real browser and are exercised by hand against live
// Workday forms; everything here runs headless so a regression in the rules that decide
// *what* to type is caught by `make test`. Run with `node tests/extension/autofill_checks.js`.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const EXTENSION = path.join(__dirname, "..", "..", "extension");

// Each engine file is an IIFE that hangs its exports off globalThis, so evaluating them
// in order in this process is exactly what the browser does with the manifest's js list.
for (const file of [
  "profile/schema.js",
  "report.js",
  "autofill/dom.js",
  "autofill/widgets.js",
  "autofill/fields.js",
  "autofill/runner.js",
]) {
  vm.runInThisContext(fs.readFileSync(path.join(EXTENSION, file), "utf8"), { filename: file });
}

const { JMEProfile, JMEAutofill, JMEReport } = globalThis;
const tests = [];
const test = (name, body) => tests.push([name, body]);

// ------------------------------------------------------------------------------------
// profile normalisation
// ------------------------------------------------------------------------------------

test("an empty profile normalises to safe defaults", () => {
  const profile = JMEProfile.normalize(null);
  assert.equal(profile.preferences.fillVoluntary, false, "voluntary answers must be opt-in");
  assert.equal(profile.preferences.overwrite, false, "existing values must be kept by default");
  assert.equal(profile.preferences.acceptTerms, false, "acknowledgements must be opt-in");
  assert.deepEqual(profile.work, []);
  assert.equal(profile.documents.resume, null);
  assert.equal(JMEProfile.isUsable(profile), false);
});

test("normalisation coerces loose values and drops unusable resume blobs", () => {
  const profile = JMEProfile.normalize({
    personal: { firstName: "Ada", lastName: "Lovelace", email: "ada@example.com", phone: 5551234567 },
    skills: "Python, , SQL ,",
    links: { other: "https://a.example, https://b.example" },
    work: [{ title: "Engineer", current: "yes" }],
    documents: { resume: { name: "resume.pdf" } },
  });
  assert.equal(profile.personal.phone, "5551234567");
  assert.deepEqual(profile.skills, ["Python", "SQL"]);
  assert.deepEqual(profile.links.other, ["https://a.example", "https://b.example"]);
  assert.equal(profile.work[0].current, true);
  assert.equal(profile.work[0].company, "", "missing keys fill in from the blank entry");
  assert.equal(profile.documents.resume, null, "a resume with no bytes is not a resume");
  assert.equal(JMEProfile.isUsable(profile), true);
});

test("normalisation is idempotent", () => {
  const once = JMEProfile.normalize({ personal: { firstName: "Ada" }, skills: ["Go"] });
  assert.deepEqual(JMEProfile.normalize(once), once);
});

// ------------------------------------------------------------------------------------
// seeding from the backend resume profile
// ------------------------------------------------------------------------------------

const RESUME_PROFILE = JSON.parse(
  fs.readFileSync(path.join(__dirname, "..", "..", "jme", "resume", "profile.json"), "utf8")
);

test("seeding fills work, education, and skills from the verified resume profile", () => {
  const seeded = JMEProfile.seedFromResumeProfile(null, RESUME_PROFILE);
  assert.equal(seeded.personal.firstName, "Adil");
  assert.equal(seeded.personal.lastName, "Mallick");
  assert.equal(seeded.work.length, RESUME_PROFILE.experience.length);
  assert.equal(seeded.work[0].company, RESUME_PROFILE.experience[0].organization);
  assert.equal(seeded.work[0].title, RESUME_PROFILE.experience[0].title);
  assert.ok(seeded.work[0].startYear, "a start year is derived from the resume date range");
  assert.equal(seeded.education[0].degree, "Bachelor's Degree");
  assert.equal(seeded.education[0].fieldOfStudy, "Computer Science");
  assert.ok(seeded.skills.length > 0);
});

test("seeding never overwrites details the user already entered", () => {
  const existing = JMEProfile.normalize({
    personal: { firstName: "Ada", lastName: "Lovelace", email: "ada@example.com" },
    work: [{ title: "Kept", company: "Kept Co" }],
    skills: ["Kept skill"],
  });
  const seeded = JMEProfile.seedFromResumeProfile(existing, RESUME_PROFILE);
  assert.equal(seeded.personal.firstName, "Ada");
  assert.equal(seeded.personal.email, "ada@example.com");
  assert.deepEqual(seeded.skills, ["Kept skill"]);
  assert.equal(seeded.work.length, 1);
  assert.equal(seeded.work[0].company, "Kept Co");
});

test("a lone resume date is read as an end date, not a start date", () => {
  const [start, end] = JMEProfile.splitDateRange("Expected May 2027");
  assert.deepEqual(start, { month: "", year: "" });
  assert.deepEqual(end, { month: "5", year: "2027" });

  const [from, to] = JMEProfile.splitDateRange("Jun. 2026 - Aug. 2026");
  assert.deepEqual(from, { month: "6", year: "2026" });
  assert.deepEqual(to, { month: "8", year: "2026" });
});

// ------------------------------------------------------------------------------------
// date parsing
// ------------------------------------------------------------------------------------

test("every date shape a profile can hold is parsed", () => {
  assert.deepEqual(JMEAutofill.parseDate("2027-05-01"), { year: "2027", month: "05", day: "01" });
  assert.deepEqual(JMEAutofill.parseDate("05/01/2027"), { month: "05", day: "01", year: "2027" });
  assert.deepEqual(JMEAutofill.parseDate("May 2027"), { month: 5, year: "2027" });
  assert.deepEqual(JMEAutofill.parseDate("2027"), { year: "2027" });
  assert.deepEqual(JMEAutofill.parseDate({ month: "6", year: "2026" }), {
    month: "6",
    day: undefined,
    year: "2026",
  });
  assert.equal(JMEAutofill.parseDate(""), null);
  assert.equal(JMEAutofill.parseDate("whenever"), null);

  const today = JMEAutofill.parseDate("today");
  assert.equal(today.year, new Date().getFullYear());
});

// ------------------------------------------------------------------------------------
// option matching
// ------------------------------------------------------------------------------------

test("option matching prefers exact, then prefix, then containment", () => {
  const { matchScore } = JMEAutofill;
  const exact = matchScore("Bachelor's Degree", "Bachelor's Degree");
  const prefix = matchScore("Bachelor's Degree (BA/BS)", "Bachelor's Degree");
  const contains = matchScore("Completed Bachelor's Degree program", "Bachelor's Degree");
  assert.ok(exact > prefix, "exact must beat prefix");
  assert.ok(prefix > contains, "prefix must beat containment");
  assert.ok(contains > 0);
  assert.equal(matchScore("Master's Degree", "Doctorate"), 0, "unrelated options score nothing");
});

test("the shortest option wins a tie, so a plain answer beats a qualified one", () => {
  const { matchScore } = JMEAutofill;
  const plain = matchScore("Master's Degree", "Master's Degree");
  const qualified = matchScore("Master's Degree - Other", "Master's Degree");
  assert.ok(plain > qualified);
});

test("degree and yes/no answers carry the spellings tenants also use", () => {
  const degree = JMEAutofill.aliasesFor("degree", "Bachelor's Degree");
  assert.ok(degree.includes("Bachelors"));
  assert.ok(degree.includes("Undergraduate"));
  assert.deepEqual(JMEAutofill.aliasesFor("yesno", "Yes"), ["Yes", "Y", "True"]);
  assert.deepEqual(JMEAutofill.aliasesFor("yesno", false), ["No", "N", "False"]);
  assert.deepEqual(JMEAutofill.aliasesFor("text", "anything"), []);
});

// ------------------------------------------------------------------------------------
// field specs
// ------------------------------------------------------------------------------------

const ALL_SPECS = [
  ["PAGE_SPECS", JMEAutofill.PAGE_SPECS],
  ["WORK_SPECS", JMEAutofill.WORK_SPECS],
  ["EDUCATION_SPECS", JMEAutofill.EDUCATION_SPECS],
  ["WEBSITE_SPECS", JMEAutofill.WEBSITE_SPECS],
];

test("every field spec is well formed", () => {
  for (const [name, specs] of ALL_SPECS) {
    const keys = new Set();
    for (const spec of specs) {
      assert.ok(spec.key, `${name}: a spec is missing its key`);
      assert.ok(!keys.has(spec.key), `${name}: duplicate key ${spec.key}`);
      keys.add(spec.key);
      assert.ok(
        Object.prototype.hasOwnProperty.call(JMEAutofill.CANDIDATES, spec.kind),
        `${name}.${spec.key}: unknown widget kind ${spec.kind}`
      );
      assert.equal(typeof spec.value, "function", `${name}.${spec.key}: value must be a function`);
      assert.ok(
        (spec.auto && spec.auto.length) || (spec.any && spec.any.length),
        `${name}.${spec.key}: needs at least one automation id or keyword phrase`
      );
    }
  }
});

test("every page spec reads cleanly from a default profile", () => {
  const profile = JMEProfile.normalize(null);
  for (const spec of JMEAutofill.PAGE_SPECS) {
    assert.doesNotThrow(() => spec.value(profile), `PAGE_SPECS.${spec.key} threw on an empty profile`);
  }
  const work = JMEProfile.blankWork();
  for (const spec of JMEAutofill.WORK_SPECS) {
    assert.doesNotThrow(() => spec.value(work), `WORK_SPECS.${spec.key} threw on an empty entry`);
  }
  const education = JMEProfile.blankEducation();
  for (const spec of JMEAutofill.EDUCATION_SPECS) {
    assert.doesNotThrow(() => spec.value(education), `EDUCATION_SPECS.${spec.key} threw on an empty entry`);
  }
});

test("voluntary answers are only offered when the profile opts in", () => {
  const voluntary = JMEAutofill.PAGE_SPECS.filter((spec) => spec.section === "voluntary");
  assert.ok(voluntary.length >= 5, "gender, ethnicity, hispanic, veteran, and disability are all covered");
  const profile = JMEProfile.normalize(null);
  const terms = voluntary.find((spec) => spec.key === "voluntaryTerms");
  assert.equal(terms.value(profile), null, "acknowledgements stay untouched unless opted in");
});

test("an end date is skipped for a job marked current", () => {
  const endDate = JMEAutofill.WORK_SPECS.find((spec) => spec.key === "endDate");
  assert.equal(endDate.value({ current: true, endMonth: "8", endYear: "2026" }), null);
  assert.deepEqual(endDate.value({ current: false, endMonth: "8", endYear: "2026" }), {
    month: "8",
    year: "2026",
  });
});

// ------------------------------------------------------------------------------------
// diagnostic report
// ------------------------------------------------------------------------------------

const SAMPLE_REPORT = {
  step: "My Information",
  host: "acme.wd1.myworkdayjobs.com",
  filled: [
    { field: "firstName", detail: "Adil" },
    { field: "email", detail: "mallick9@msu.edu" },
    { field: "address1", detail: "123 Grand River Ave" },
  ],
  skipped: [{ field: "state", reason: 'no option matching "Michigan" among 47' }],
  notFound: ["phoneCountryCode", "howHeard"],
  warnings: ["Work experience: filled 1 of 3 entries."],
};

test("the diagnostic names every field, in the section that explains it", () => {
  const text = JMEReport.buildDiagnostic(SAMPLE_REPORT);
  assert.ok(text.includes("acme.wd1.myworkdayjobs.com"), "the tenant has to be identifiable");
  assert.ok(text.includes("My Information"));
  assert.ok(text.includes("3 filled · 1 skipped · 2 not found"));
  for (const field of ["firstName", "email", "address1", "state", "phoneCountryCode", "howHeard"]) {
    assert.ok(text.includes(field), `${field} missing from the report`);
  }
  assert.ok(text.includes('no option matching "Michigan" among 47'), "skip reasons are the diagnostic");
  assert.ok(text.includes("filled 1 of 3 entries"), "warnings carry through");
});

test("the diagnostic never carries the values that were typed in", () => {
  const text = JMEReport.buildDiagnostic(SAMPLE_REPORT);
  for (const value of ["Adil", "mallick9@msu.edu", "123 Grand River Ave"]) {
    assert.ok(!text.includes(value), `the report leaked a filled value: ${value}`);
  }
});

test("an empty report still renders without throwing", () => {
  const text = JMEReport.buildDiagnostic({
    step: "Review",
    host: "acme.wd1.myworkdayjobs.com",
    filled: [],
    skipped: [],
    notFound: [],
    warnings: [],
  });
  assert.ok(text.includes("0 filled · 0 skipped · 0 not found"));
  assert.ok(text.includes("(nothing)"));
  assert.equal(JMEReport.buildDiagnostic(null), "");
});

// ------------------------------------------------------------------------------------
// runner
// ------------------------------------------------------------------------------------

test("the runner exposes no way to submit or advance the form", () => {
  const source = fs.readFileSync(path.join(EXTENSION, "autofill", "runner.js"), "utf8");
  for (const forbidden of ["submit", "Next", "Continue"]) {
    assert.ok(
      !new RegExp(`click\\w*\\([^)]*${forbidden}`, "i").test(source),
      `runner.js must never click ${forbidden}`
    );
  }
});

// ------------------------------------------------------------------------------------

let failures = 0;
for (const [name, body] of tests) {
  try {
    body();
    console.log(`  ok   ${name}`);
  } catch (error) {
    failures += 1;
    console.error(`  FAIL ${name}\n       ${error.message}`);
  }
}
console.log(`\n${tests.length - failures} passed, ${failures} failed`);
process.exit(failures ? 1 : 0);
