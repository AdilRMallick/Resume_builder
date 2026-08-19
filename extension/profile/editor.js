"use strict";

// The profile editor. A form bound to the stored profile by `data-path`, plus two
// repeaters for the list-shaped sections.
//
// Saving is explicit but a debounced autosave runs behind it, because losing a
// half-typed work history to a closed tab is worse than an extra write to local storage.

const byId = (id) => document.getElementById(id);

let profile = JMEProfile.normalize(null);
let saveTimer = null;

// ------------------------------------------------------------------------------------
// path binding
// ------------------------------------------------------------------------------------

// `readFormInto` writes through nested paths, so it always gets a deep copy: handing it
// the live profile would mutate the object the caller is still reading from.
const copy = (value) => JSON.parse(JSON.stringify(value));

const getPath = (object, path) =>
  path.split(".").reduce((value, key) => (value === undefined || value === null ? value : value[key]), object);

function setPath(object, path, value) {
  const keys = path.split(".");
  const last = keys.pop();
  const target = keys.reduce((value, key) => (value[key] ??= {}), object);
  target[last] = value;
}

const boundInputs = () => [...document.querySelectorAll("[data-path]")];

function readFormInto(target) {
  for (const input of boundInputs()) {
    const path = input.dataset.path;
    if (input.type === "checkbox") {
      setPath(target, path, input.checked);
    } else if (input.hasAttribute("data-list")) {
      setPath(target, path, input.value.split(",").map((item) => item.trim()).filter(Boolean));
    } else {
      setPath(target, path, input.value);
    }
  }
  target.work = readRepeater("work-list");
  target.education = readRepeater("education-list");
  return target;
}

function writeFormFrom(source) {
  for (const input of boundInputs()) {
    const value = getPath(source, input.dataset.path);
    if (input.type === "checkbox") input.checked = Boolean(value);
    else if (input.hasAttribute("data-list")) input.value = (value || []).join(", ");
    else input.value = value ?? "";
  }
  renderRepeater("work-list", "work-template", source.work, workTitle);
  renderRepeater("education-list", "education-template", source.education, educationTitle);
  renderResumeState(source);
}

// ------------------------------------------------------------------------------------
// repeaters
// ------------------------------------------------------------------------------------

const workTitle = (entry) =>
  [entry.title, entry.company].filter(Boolean).join(" · ") || "New job";
const educationTitle = (entry) =>
  [entry.school, entry.degree].filter(Boolean).join(" · ") || "New school";

function renderRepeater(listId, templateId, entries, titleOf) {
  const list = byId(listId);
  list.innerHTML = "";
  for (const entry of entries) list.appendChild(buildCard(templateId, entry, titleOf));
}

function buildCard(templateId, entry, titleOf) {
  const card = byId(templateId).content.firstElementChild.cloneNode(true);
  for (const field of card.querySelectorAll("[data-field]")) {
    const value = entry[field.dataset.field];
    if (field.type === "checkbox") field.checked = Boolean(value);
    else field.value = value ?? "";
    field.addEventListener("input", () => {
      card.querySelector(".entry-title").textContent = titleOf(readCard(card));
      markDirty();
    });
    field.addEventListener("change", markDirty);
  }
  card.querySelector(".entry-title").textContent = titleOf(entry);
  card.querySelector("[data-remove]").addEventListener("click", () => {
    card.remove();
    markDirty();
  });
  return card;
}

function readCard(card) {
  const entry = {};
  for (const field of card.querySelectorAll("[data-field]")) {
    entry[field.dataset.field] = field.type === "checkbox" ? field.checked : field.value;
  }
  return entry;
}

const readRepeater = (listId) => [...byId(listId).children].map(readCard);

byId("add-work").addEventListener("click", () => {
  byId("work-list").appendChild(buildCard("work-template", JMEProfile.blankWork(), workTitle));
  markDirty();
});

byId("add-education").addEventListener("click", () => {
  byId("education-list").appendChild(buildCard("education-template", JMEProfile.blankEducation(), educationTitle));
  markDirty();
});

// ------------------------------------------------------------------------------------
// persistence
// ------------------------------------------------------------------------------------

function setSaveState(message, className = "") {
  const state = byId("save-state");
  state.textContent = message;
  state.className = `save-state ${className}`;
}

function markDirty() {
  setSaveState("Unsaved changes", "dirty");
  clearTimeout(saveTimer);
  saveTimer = setTimeout(save, 1200);
}

async function save() {
  clearTimeout(saveTimer);
  try {
    // The stored profile is the source of truth for the resume file, which has no form
    // control of its own; read the form on top of it rather than replacing it.
    profile = JMEProfile.normalize(readFormInto(copy(profile)));
    await chrome.storage.local.set({ [JMEProfile.STORAGE_KEY]: profile });
    setSaveState(`Saved ${new Date().toLocaleTimeString()}`, "saved");
  } catch (error) {
    setSaveState(`Save failed: ${error.message}`, "error");
  }
}

async function load() {
  const stored = await chrome.storage.local.get(JMEProfile.STORAGE_KEY);
  profile = JMEProfile.normalize(stored[JMEProfile.STORAGE_KEY]);
  writeFormFrom(profile);
  setSaveState(JMEProfile.isUsable(profile) ? "Loaded" : "Start with your name and email");
}

byId("save").addEventListener("click", save);
for (const input of boundInputs()) {
  input.addEventListener("input", markDirty);
  input.addEventListener("change", markDirty);
}

// ------------------------------------------------------------------------------------
// resume file
// ------------------------------------------------------------------------------------

function renderResumeState(source) {
  const state = byId("resume-state");
  const resume = source.documents?.resume;
  if (!resume) {
    state.textContent = "No resume attached.";
    state.className = "resume-state";
    return;
  }
  const kilobytes = Math.round((resume.base64.length * 3) / 4 / 1024);
  const when = resume.savedAt ? ` · saved ${new Date(resume.savedAt).toLocaleDateString()}` : "";
  state.textContent = `${resume.name} · ${kilobytes} KB${when}`;
  state.className = "resume-state set";
}

byId("resume-file").addEventListener("change", async (event) => {
  const [file] = event.target.files;
  if (!file) return;
  // 8 MB keeps the profile comfortably inside the local-storage quota; Workday's own
  // upload limit is smaller than that anyway.
  if (file.size > 8 * 1024 * 1024) {
    setSaveState("That file is over 8 MB", "error");
    return;
  }
  profile.documents.resume = {
    name: file.name,
    mimeType: file.type || "application/pdf",
    base64: await toBase64(file),
    savedAt: new Date().toISOString(),
  };
  renderResumeState(profile);
  await save();
  event.target.value = "";
});

byId("clear-resume").addEventListener("click", async () => {
  profile.documents.resume = null;
  renderResumeState(profile);
  await save();
});

function toBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("could not read the file"));
    reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
    reader.readAsDataURL(file);
  });
}

// ------------------------------------------------------------------------------------
// import and export
// ------------------------------------------------------------------------------------

byId("import-resume").addEventListener("click", async () => {
  setSaveState("Reading the local backend...");
  try {
    const response = await fetch("http://127.0.0.1:8002/resume/profile");
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    // Take what is on screen first so a half-typed entry is not lost to the import.
    profile = JMEProfile.seedFromResumeProfile(readFormInto(copy(profile)), await response.json());
    writeFormFrom(profile);
    await save();
    setSaveState("Imported from JME", "saved");
  } catch (error) {
    setSaveState(`Import failed: ${error.message}`, "error");
  }
});

byId("export").addEventListener("click", () => {
  const current = JMEProfile.normalize(readFormInto(copy(profile)));
  const href = URL.createObjectURL(
    new Blob([JSON.stringify(current, null, 2)], { type: "application/json" })
  );
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = "jme-apply-profile.json";
  anchor.click();
  URL.revokeObjectURL(href);
  setSaveState("Exported", "saved");
});

byId("import-file").addEventListener("change", async (event) => {
  const [file] = event.target.files;
  if (!file) return;
  try {
    profile = JMEProfile.normalize(JSON.parse(await file.text()));
    writeFormFrom(profile);
    await save();
    setSaveState("Imported from file", "saved");
  } catch (error) {
    setSaveState(`Could not read that file: ${error.message}`, "error");
  }
  event.target.value = "";
});

load();
