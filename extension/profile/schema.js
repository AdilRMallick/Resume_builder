"use strict";

// The one profile the extension knows about, its defaults, and the normaliser that
// every reader runs first.
//
// The profile lives in `chrome.storage.local` and is never sent anywhere: the autofill
// engine runs in the page, and the resume tailor talks only to the local backend on
// 127.0.0.1. Normalising on read rather than on write means a profile saved by an older
// version of the extension still loads after new fields are added.

(function (global) {
  const STORAGE_KEY = "jmeProfile";

  const blankWork = () => ({
    title: "",
    company: "",
    location: "",
    startMonth: "",
    startYear: "",
    endMonth: "",
    endYear: "",
    current: false,
    description: "",
  });

  const blankEducation = () => ({
    school: "",
    degree: "",
    fieldOfStudy: "",
    gpa: "",
    startYear: "",
    endYear: "",
  });

  const DEFAULT_PROFILE = {
    personal: {
      firstName: "",
      middleName: "",
      lastName: "",
      preferredName: "",
      email: "",
      phone: "",
      phoneType: "Mobile",
      phoneCountryCode: "United States of America (+1)",
      address1: "",
      address2: "",
      city: "",
      state: "",
      postalCode: "",
      country: "United States of America",
    },
    links: { linkedin: "", github: "", portfolio: "", other: [] },
    work: [],
    education: [],
    skills: [],
    application: {
      howHeard: ["Company Website"],
      previouslyEmployed: "No",
      workAuthorized: "Yes",
      requireSponsorship: "No",
      willingToRelocate: "Yes",
      desiredSalary: "",
      availableStartDate: "",
    },
    voluntary: {
      gender: "",
      ethnicity: "",
      hispanicLatino: "",
      veteranStatus: "",
      disability: "",
    },
    documents: {
      // { name, mimeType, base64 } — set from a file picker or from a tailored resume.
      resume: null,
    },
    preferences: {
      // Off by default: demographic answers are the user's to volunteer, not the
      // extension's to assume.
      fillVoluntary: false,
      // Off by default: a value Workday pre-filled from a parsed resume is more likely
      // right than a stale profile entry, and silently replacing it hides the change.
      overwrite: false,
      acceptTerms: false,
    },
  };

  const clone = (value) => JSON.parse(JSON.stringify(value));

  const asString = (value, fallback = "") =>
    value === undefined || value === null ? fallback : String(value);

  const asBool = (value, fallback = false) =>
    value === undefined || value === null ? fallback : Boolean(value);

  const asList = (value) =>
    (Array.isArray(value) ? value : typeof value === "string" ? value.split(",") : [])
      .map((item) => (typeof item === "string" ? item.trim() : item))
      .filter(Boolean);

  function mergeStrings(defaults, raw) {
    const result = {};
    for (const [key, fallback] of Object.entries(defaults)) {
      result[key] = typeof fallback === "boolean" ? asBool(raw?.[key], fallback) : asString(raw?.[key], fallback);
    }
    return result;
  }

  /** Fill in anything missing and coerce every field to the type the fillers expect. */
  function normalize(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const profile = clone(DEFAULT_PROFILE);

    profile.personal = mergeStrings(DEFAULT_PROFILE.personal, source.personal);
    profile.links = {
      ...mergeStrings({ linkedin: "", github: "", portfolio: "" }, source.links),
      other: asList(source.links?.other),
    };

    profile.work = (Array.isArray(source.work) ? source.work : []).map((entry) => ({
      ...blankWork(),
      ...mergeStrings(blankWork(), entry),
      current: asBool(entry?.current),
    }));
    profile.education = (Array.isArray(source.education) ? source.education : []).map((entry) => ({
      ...blankEducation(),
      ...mergeStrings(blankEducation(), entry),
    }));
    profile.skills = asList(source.skills);

    profile.application = {
      ...mergeStrings(
        {
          previouslyEmployed: DEFAULT_PROFILE.application.previouslyEmployed,
          workAuthorized: DEFAULT_PROFILE.application.workAuthorized,
          requireSponsorship: DEFAULT_PROFILE.application.requireSponsorship,
          willingToRelocate: DEFAULT_PROFILE.application.willingToRelocate,
          desiredSalary: "",
          availableStartDate: "",
        },
        source.application
      ),
      howHeard: asList(source.application?.howHeard).length
        ? asList(source.application.howHeard)
        : clone(DEFAULT_PROFILE.application.howHeard),
    };

    profile.voluntary = mergeStrings(DEFAULT_PROFILE.voluntary, source.voluntary);

    const resume = source.documents?.resume;
    profile.documents = {
      resume:
        resume && resume.base64
          ? {
              name: asString(resume.name, "resume.pdf"),
              mimeType: asString(resume.mimeType, "application/pdf"),
              base64: asString(resume.base64),
              savedAt: asString(resume.savedAt),
            }
          : null,
    };

    profile.preferences = mergeStrings(DEFAULT_PROFILE.preferences, source.preferences);
    return profile;
  }

  /** True when there is enough here for a fill to do anything useful. */
  function isUsable(profile) {
    return Boolean(profile?.personal?.firstName && profile?.personal?.lastName && profile?.personal?.email);
  }

  /**
   * Seed name, education, work, and skills from the backend's verified resume profile.
   *
   * The backend profile is the same evidence the resume tailor draws on, so seeding from
   * it keeps one set of facts behind both features. Contact details and anything the
   * user has already typed are left alone — the resume profile has no address or phone,
   * and overwriting a hand-corrected entry would be a surprise.
   */
  function seedFromResumeProfile(profile, resumeProfile) {
    const seeded = normalize(profile);
    if (!resumeProfile) return seeded;

    if (!seeded.personal.firstName && !seeded.personal.lastName && resumeProfile.name) {
      const parts = String(resumeProfile.name).replace(/\./g, "").split(/\s+/).filter(Boolean);
      seeded.personal.firstName = parts[0] || "";
      seeded.personal.lastName = parts.length > 1 ? parts[parts.length - 1] : "";
      if (parts.length > 2) seeded.personal.middleName = parts.slice(1, -1).join(" ");
    }

    for (const contact of resumeProfile.contact || []) {
      const value = String(contact.value || "");
      const url = String(contact.url || "");
      if (!seeded.personal.email && value.includes("@")) seeded.personal.email = value;
      if (!seeded.personal.phone && /\d{3}.*\d{4}/.test(value) && !value.includes("@")) {
        seeded.personal.phone = value;
      }
      if (!seeded.links.linkedin && /linkedin/i.test(url + value)) seeded.links.linkedin = url || value;
      if (!seeded.links.github && /github/i.test(url + value)) seeded.links.github = url || value;
    }

    if (!seeded.work.length) {
      seeded.work = (resumeProfile.experience || []).map((entry) => {
        const [start, end] = splitDateRange(entry.dates);
        return {
          ...blankWork(),
          title: asString(entry.title),
          company: asString(entry.organization),
          location: asString(entry.location),
          startMonth: start.month,
          startYear: start.year,
          endMonth: end.month,
          endYear: end.year,
          current: /present|current/i.test(asString(entry.dates)),
          description: (entry.bullets || []).map((bullet) => `• ${bullet.text}`).join("\n"),
        };
      });
    }

    if (!seeded.education.length) {
      seeded.education = (resumeProfile.education || []).map((entry) => {
        const [start, end] = splitDateRange(entry.dates);
        return {
          ...blankEducation(),
          school: asString(entry.organization),
          degree: degreeFromTitle(entry.title),
          fieldOfStudy: fieldFromTitle(entry.title),
          startYear: start.year,
          endYear: end.year,
        };
      });
    }

    if (!seeded.skills.length) {
      seeded.skills = Object.values(resumeProfile.skills || {}).flat().slice(0, 20);
    }
    return seeded;
  }

  const MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];

  /**
   * Turn "Jun. 2026 - Aug. 2026" or "Expected May 2027" into two `{month, year}` ends.
   *
   * A range with no dash is read as an end date, not a start date: on a resume a lone
   * date is a graduation or completion ("Expected May 2027"), and guessing it as a start
   * would put a future year in the "from" box of every education panel.
   */
  function splitDateRange(raw) {
    const text = asString(raw);
    const halves = text.split(/\s*[-–—]\s*/).filter((half) => half.trim());
    const empty = { month: "", year: "" };
    const parse = (part) => {
      // Scan every word for a month rather than trusting the first one: "Expected May
      // 2027" and "Graduated Dec 2024" both lead with a word that is not a month.
      const words = String(part).match(/[a-z]{3,}/gi) || [];
      let monthIndex = -1;
      for (const word of words) {
        const index = MONTHS.indexOf(word.slice(0, 3).toLowerCase());
        if (index >= 0) {
          monthIndex = index;
          break;
        }
      }
      const yearMatch = String(part).match(/(\d{4})/);
      return {
        month: monthIndex >= 0 ? String(monthIndex + 1) : "",
        year: yearMatch ? yearMatch[1] : "",
      };
    };
    if (halves.length < 2) return [empty, parse(halves[0] || "")];
    return [parse(halves[0]), parse(halves[1])];
  }

  function degreeFromTitle(title) {
    const text = asString(title).toLowerCase();
    if (text.includes("ph.d") || text.includes("doctor")) return "Doctorate";
    if (text.includes("master")) return "Master's Degree";
    if (text.includes("bachelor")) return "Bachelor's Degree";
    if (text.includes("associate")) return "Associate's Degree";
    return "";
  }

  function fieldFromTitle(title) {
    const match = asString(title).match(/\bin\s+([^,]+)/i);
    return match ? match[1].trim() : "";
  }

  global.JMEProfile = {
    STORAGE_KEY,
    DEFAULT_PROFILE,
    blankWork,
    blankEducation,
    normalize,
    isUsable,
    seedFromResumeProfile,
    splitDateRange,
  };
})(globalThis);
