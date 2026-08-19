"use strict";

const API = "http://127.0.0.1:8002";
const AUTOFILL_SCRIPTS = [
  "profile/schema.js",
  "autofill/dom.js",
  "autofill/widgets.js",
  "autofill/fields.js",
  "autofill/runner.js",
  "autofill/content.js",
];

const byId = (id) => document.getElementById(id);
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (character) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character])
);

let currentUrl = "";
let serverReady = false;
let currentTailoredResume = null;
let currentPdfUrl = "";
let currentPdfBase64 = "";
let profile = null;

// ------------------------------------------------------------------------------------
// tabs
// ------------------------------------------------------------------------------------

function showView(name) {
  for (const view of ["autofill", "tailor"]) {
    const active = view === name;
    byId(`view-${view}`).hidden = !active;
    byId(`tab-${view}`).classList.toggle("is-active", active);
    byId(`tab-${view}`).setAttribute("aria-selected", String(active));
  }
}

byId("tab-autofill").addEventListener("click", () => showView("autofill"));
byId("tab-tailor").addEventListener("click", () => showView("tailor"));

// ------------------------------------------------------------------------------------
// profile storage
// ------------------------------------------------------------------------------------

async function loadProfile() {
  const stored = await chrome.storage.local.get(JMEProfile.STORAGE_KEY);
  profile = JMEProfile.normalize(stored[JMEProfile.STORAGE_KEY]);
  renderProfileSummary();
  byId("overwrite-toggle").checked = profile.preferences.overwrite;
  byId("voluntary-toggle").checked = profile.preferences.fillVoluntary;
  return profile;
}

async function saveProfile(next) {
  profile = JMEProfile.normalize(next);
  await chrome.storage.local.set({ [JMEProfile.STORAGE_KEY]: profile });
  renderProfileSummary();
}

function renderProfileSummary() {
  const target = byId("profile-summary");
  if (!JMEProfile.isUsable(profile)) {
    target.innerHTML = "No profile saved yet. <strong>Edit profile</strong> to add your name, contact details, and history.";
    updateFillButton();
    return;
  }
  const name = `${profile.personal.firstName} ${profile.personal.lastName}`.trim();
  const bits = [
    `${profile.work.length} job${profile.work.length === 1 ? "" : "s"}`,
    `${profile.education.length} school${profile.education.length === 1 ? "" : "s"}`,
    `${profile.skills.length} skill${profile.skills.length === 1 ? "" : "s"}`,
    profile.documents.resume ? `resume: ${profile.documents.resume.name}` : "no resume attached",
  ];
  target.innerHTML = `<strong>${escapeHTML(name)}</strong> · ${escapeHTML(profile.personal.email)}<br>${escapeHTML(bits.join(" · "))}`;
  updateFillButton();
}

byId("edit-profile").addEventListener("click", () => chrome.runtime.openOptionsPage());

byId("overwrite-toggle").addEventListener("change", async (event) => {
  await saveProfile({ ...profile, preferences: { ...profile.preferences, overwrite: event.target.checked } });
});

byId("voluntary-toggle").addEventListener("change", async (event) => {
  await saveProfile({ ...profile, preferences: { ...profile.preferences, fillVoluntary: event.target.checked } });
  setFillStatus(
    event.target.checked
      ? "Voluntary disclosure answers will be filled from your profile."
      : "Voluntary disclosure pages will be left blank for you."
  );
});

byId("seed-profile").addEventListener("click", async () => {
  setFillStatus("Reading your verified resume profile from the local backend...");
  try {
    const response = await fetch(`${API}/resume/profile`);
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    const resumeProfile = await response.json();
    await saveProfile(JMEProfile.seedFromResumeProfile(profile, resumeProfile));
    setFillStatus("Imported your work history, education, and skills. Empty fields were left for you to fill in.");
  } catch (error) {
    setFillStatus(`Import failed: ${error.message}. Start JME on port 8002 first.`, true);
  }
});

// ------------------------------------------------------------------------------------
// autofill
// ------------------------------------------------------------------------------------

function setFillStatus(message, error = false) {
  byId("fill-status").textContent = message;
  byId("fill-status").classList.toggle("error", error);
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tab || null;
}

const isWorkdayUrl = (url) => /^https?:\/\/[^/]*(myworkdayjobs\.com|myworkdaysite\.com|workday\.com)/i.test(url || "");

/**
 * Ask the current tab whether the autofill engine is loaded there.
 *
 * The content script is declared for Workday hosts, but a tab that was already open when
 * the extension was installed or reloaded has no script in it until the page reloads, so
 * a failed ping is a normal state rather than an error.
 *
 * `keepStatus` is set when this runs straight after a fill: the report line is the most
 * useful thing on screen at that moment and must not be overwritten by a ready message.
 */
async function refreshPageState({ keepStatus = false } = {}) {
  const tab = await activeTab();
  const dot = byId("page-dot");
  const label = byId("page-label");
  const status = (message) => {
    if (!keepStatus) setFillStatus(message);
  };

  if (!tab || !/^https?:/.test(tab.url || "")) {
    dot.className = "page-dot away";
    label.textContent = "Open a Workday application tab.";
    updateFillButton({ keepStatus });
    return;
  }
  if (!isWorkdayUrl(tab.url)) {
    dot.className = "page-dot away";
    label.textContent = "This tab is not a Workday application.";
    status("Autofill runs on *.myworkdayjobs.com and Workday-hosted application pages.");
    updateFillButton({ keepStatus });
    return;
  }

  try {
    const response = await chrome.tabs.sendMessage(tab.id, { type: "jme-autofill-ping" });
    dot.className = "page-dot ready";
    label.textContent = `Workday · ${response?.step || "application"}`;
    status("Ready. Fill this page, review it, then press Next yourself.");
  } catch {
    dot.className = "page-dot";
    label.textContent = "Workday tab found, engine not loaded yet.";
    status("Press Fill and the engine will be injected into this tab.");
  }
  updateFillButton({ keepStatus });
}

function updateFillButton({ keepStatus = false } = {}) {
  const usable = JMEProfile.isUsable(profile);
  byId("fill").disabled = !usable;
  if (!usable && !keepStatus) {
    setFillStatus("Add your name and email under Edit profile before the first fill.");
  }
}

/** Load the engine into a tab that predates the install, then retry the message. */
async function injectEngine(tabId) {
  await chrome.scripting.executeScript({ target: { tabId }, files: AUTOFILL_SCRIPTS });
}

byId("fill").addEventListener("click", async () => {
  const button = byId("fill");
  const tab = await activeTab();
  if (!tab?.id) {
    setFillStatus("No active tab to fill.", true);
    return;
  }
  button.disabled = true;
  button.firstElementChild.textContent = "Filling...";
  setFillStatus("Reading the form and filling what matches your profile...");
  try {
    let report;
    try {
      report = await chrome.tabs.sendMessage(tab.id, { type: "jme-autofill-run" });
    } catch {
      await injectEngine(tab.id);
      report = await chrome.tabs.sendMessage(tab.id, { type: "jme-autofill-run" });
    }
    renderReport(report);
  } catch (error) {
    setFillStatus(`Fill failed: ${error.message}`, true);
  } finally {
    button.firstElementChild.textContent = "Fill this page";
    refreshPageState({ keepStatus: true });
  }
});

function renderReport(report) {
  const container = byId("fill-report");
  if (!report || report.error) {
    container.hidden = true;
    setFillStatus(report?.error || "The page did not respond.", true);
    return;
  }
  container.hidden = false;
  byId("report-step").textContent = report.step;
  byId("report-count").textContent = `${report.filled.length} filled`;
  byId("report-filled").innerHTML = report.filled.length
    ? report.filled.map((item) =>
        `<li>${escapeHTML(item.field)} <span>${escapeHTML(item.detail)}</span></li>`).join("")
    : "<li><span>Nothing on this page matched your profile.</span></li>";

  const issues = [
    ...report.skipped.map((item) => `${item.field}: ${item.reason}`),
    ...report.warnings,
  ];
  byId("report-issues").hidden = !issues.length;
  byId("report-skipped").innerHTML = issues.map((line) => `<li>${escapeHTML(line)}</li>`).join("");

  setFillStatus(
    report.filled.length
      ? `${report.filled.length} field${report.filled.length === 1 ? "" : "s"} filled on ${report.step}. Review, then press Next yourself.`
      : "No matching fields here. Move to the next step and fill again."
  );
}

// ------------------------------------------------------------------------------------
// resume tailoring
// ------------------------------------------------------------------------------------

function updateCount() {
  const length = byId("job-description").value.length;
  byId("character-count").textContent = `${length.toLocaleString()} characters`;
  byId("tailor").disabled = !serverReady || length < 50;
}

function setStatus(message, error = false) {
  byId("status-copy").textContent = message;
  byId("status-copy").classList.toggle("error", error);
}

async function checkServer() {
  try {
    await loadProviders();
    serverReady = true;
    byId("server-state").className = "server-state ok";
    byId("server-state").lastElementChild.textContent = "Ready / resume tailor";
    setStatus("Connected. Open a job page or paste a description.");
  } catch (_error) {
    serverReady = false;
    byId("server-state").className = "server-state bad";
    byId("server-state").lastElementChild.textContent = "Offline";
    byId("provider-note").textContent = "Provider status unavailable; verified mode remains ready.";
    setStatus("Start JME locally on port 8002, then reopen this panel.", true);
  }
  updateCount();
}

async function loadProviders() {
  const response = await fetch(`${API}/resume/providers`);
  if (!response.ok) throw new Error(String(response.status));
  const data = await response.json();
  for (const provider of data.providers) {
    const option = byId("customization-mode").querySelector(`option[value="${provider.id}"]`);
    if (!option) continue;
    option.disabled = !provider.available;
    option.textContent = provider.available
      ? `${provider.label}${provider.model ? ` · ${provider.model}` : ""}`
      : `${provider.label} (key not configured)`;
  }
  const availableAI = data.providers.filter(
    (provider) => provider.id !== "verified" && provider.available
  );
  byId("provider-note").textContent = availableAI.length
    ? "AI sends this job description and selected verified bullets to the chosen provider."
    : "Add an OpenAI, Anthropic, Gemini, or Moonshot key to .env, then restart JME.";
}

function renderEntry(entry, sectionTitle) {
  const organization = entry.url
    ? `<a href="${escapeHTML(entry.url)}">${escapeHTML(entry.organization)}</a>`
    : escapeHTML(entry.organization);
  const employment = sectionTitle === "Experience" || sectionTitle === "Leadership";
  const primaryLeft = employment ? escapeHTML(entry.title) : organization;
  const primaryRight = employment ? escapeHTML(entry.dates) : escapeHTML(entry.location || entry.dates);
  const secondaryLeft = employment ? organization : escapeHTML(entry.title);
  const secondaryRight = employment ? escapeHTML(entry.location) : (entry.location ? escapeHTML(entry.dates) : "");
  return `<div class="entry">
    <div class="entry-head"><span>${primaryLeft}</span><span>${primaryRight}</span></div>
    <div class="entry-sub"><span>${secondaryLeft}</span><span>${secondaryRight}</span></div>
    <ul>${entry.bullets.map((bullet) => {
      const className = bullet.ai_rewritten ? "ai-rewritten" : "";
      const original = bullet.source_text
        ? ` title="Original: ${escapeHTML(bullet.source_text)}"`
        : "";
      return `<li class="${className}"${original}>${escapeHTML(bullet.text)}</li>`;
    }).join("")}</ul>
  </div>`;
}

function renderSection(title, entries) {
  return `<section><h3>${escapeHTML(title)}</h3>${entries.map((entry) => renderEntry(entry, title)).join("")}</section>`;
}

function renderResume(data) {
  currentTailoredResume = data;
  const contacts = data.contact.map((item) => item.url
    ? `<a href="${escapeHTML(item.url)}">${escapeHTML(item.value)}</a>`
    : escapeHTML(item.value)).join(" &nbsp;|&nbsp; ");
  const skills = Object.entries(data.skills).map(([category, values]) =>
    `<div><strong>${escapeHTML(category)}:</strong> ${values.map(escapeHTML).join(", ")}</div>`
  ).join("");

  byId("match-chips").innerHTML = (data.matched_skills.length ? data.matched_skills.slice(0, 12) : ["title-based selection"])
    .map((skill) => `<span class="match-chip">${escapeHTML(skill)}</span>`).join("");
  byId("resume").innerHTML = `
    <h2>${escapeHTML(data.name)}</h2>
    <div class="contact">${contacts}</div>
    ${renderSection("Education", data.education)}
    ${renderSection("Experience", data.experience)}
    ${renderSection("Projects", data.projects)}
    ${renderSection("Leadership", data.leadership)}
    <section><h3>Technical Skills</h3><div class="skills">${skills}<div><strong>Certifications:</strong> ${data.certifications.map(escapeHTML).join(", ")}</div></div></section>`;
  const customization = data.customization;
  const evidenceNote = customization.applied_mode === "ai"
    ? "AI rewrites remain tied to verified evidence."
    : "No bullet wording was changed; evidence was selected and reordered only.";
  const layoutNote = data.pdf_omitted_bullets
    ? `${data.pdf_omitted_bullets} lower-priority bullet${data.pdf_omitted_bullets === 1 ? " was" : "s were"} omitted to keep the canonical layout to one page.`
    : "The canonical layout fits on one page without further omissions.";
  if (customization.applied_mode === "ai") {
    byId("truth-badge").textContent = `${customization.rewritten_bullets} AI rewrites`;
  } else {
    byId("truth-badge").textContent = "Verified selection";
  }
  if (currentPdfUrl) URL.revokeObjectURL(currentPdfUrl);
  currentPdfUrl = "";
  currentPdfBase64 = data.pdf_base64 || "";
  const pdfFrame = byId("resume-pdf");
  const textPreview = byId("resume");
  const pdfButton = byId("download-pdf");
  if (data.pdf_base64) {
    const binary = atob(data.pdf_base64);
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    currentPdfUrl = URL.createObjectURL(new Blob([bytes], { type: "application/pdf" }));
    pdfFrame.src = currentPdfUrl;
    pdfFrame.hidden = false;
    textPreview.hidden = true;
    pdfButton.disabled = false;
    byId("edit-note").textContent = `This is the actual locally compiled Jake-template PDF. ${layoutNote} ${evidenceNote} Download .tex for source edits.`;
  } else {
    pdfFrame.removeAttribute("src");
    pdfFrame.hidden = true;
    textPreview.hidden = false;
    pdfButton.disabled = true;
    byId("edit-note").textContent = `${evidenceNote} ${data.pdf_error || "The LaTeX PDF preview is unavailable."}`;
  }
  byId("use-for-autofill").disabled = !currentPdfBase64;
  byId("result").hidden = false;
  byId("result").scrollIntoView({ behavior: "smooth", block: "start" });
}

byId("use-page").addEventListener("click", async () => {
  setStatus("Reading the active page...");
  const result = await chrome.runtime.sendMessage({ type: "extract-active-job-page" });
  if (!result?.ok) {
    setStatus(result?.error || "Could not read this page. Paste the description instead.", true);
    return;
  }
  byId("job-description").value = result.text;
  byId("job-title").value = result.title;
  byId("job-company").value = result.company;
  currentUrl = result.url;
  updateCount();
  setStatus(`Captured ${result.text.length.toLocaleString()} visible characters from this page.`);
});

byId("job-file").addEventListener("change", async (event) => {
  const [file] = event.target.files;
  if (!file) return;
  const text = (await file.text()).slice(0, 100000);
  byId("job-description").value = text;
  updateCount();
  setStatus(`Loaded ${file.name}.`);
});

byId("job-description").addEventListener("input", updateCount);

byId("tailor").addEventListener("click", async () => {
  const button = byId("tailor");
  button.disabled = true;
  button.firstElementChild.textContent = "Tailoring...";
  setStatus("Selecting evidence and compiling the Jake-template PDF...");
  try {
    const response = await fetch(`${API}/resume/tailor`, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify({
        job_description: byId("job-description").value,
        title: byId("job-title").value,
        company: byId("job-company").value,
        url: currentUrl,
        render_pdf: true,
        customization_mode: byId("customization-mode").value,
      }),
    });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    const data = await response.json();
    renderResume(data);
    const customization = data.customization;
    const message = customization.applied_mode === "ai"
      ? `${customization.rewritten_bullets} evidence-grounded bullets rewritten by ${customization.provider}. ${customization.rejected_rewrites} rejected by validation.`
      : `${data.matched_skills.length} relevant evidence signals found. No bullet was rewritten.`;
    const warning = [customization.warning, data.pdf_error].filter(Boolean).join(" ");
    setStatus(warning ? `${message} ${warning}` : message);
  } catch (error) {
    setStatus(`Tailoring failed: ${error.message}`, true);
  } finally {
    button.firstElementChild.textContent = "Build tailored resume";
    updateCount();
  }
});

byId("copy").addEventListener("click", async () => {
  const resume = byId("resume");
  await navigator.clipboard.writeText(resume.innerText || resume.textContent);
  setStatus("Resume text copied to the clipboard.");
});

byId("download").addEventListener("click", () => {
  if (!currentTailoredResume) return;
  const href = URL.createObjectURL(new Blob([currentTailoredResume.latex], { type: "application/x-tex" }));
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = "Adil_Mallick_Tailored_Resume.tex";
  anchor.click();
  URL.revokeObjectURL(href);
  setStatus("Downloaded the canonical Jake-template LaTeX resume.");
});

byId("download-pdf").addEventListener("click", () => {
  if (!currentPdfUrl) return;
  const anchor = document.createElement("a");
  anchor.href = currentPdfUrl;
  anchor.download = "Adil_Mallick_Tailored_Resume.pdf";
  anchor.click();
  setStatus("Downloaded the locally compiled Jake-template PDF.");
});

// The one bridge between the two halves of the extension: the resume the tailor just
// compiled becomes the file the autofill engine attaches on the next application.
byId("use-for-autofill").addEventListener("click", async () => {
  if (!currentPdfBase64) return;
  const company = byId("job-company").value.trim().replace(/[^\w-]+/g, "_");
  const name = company ? `Resume_${company}.pdf` : "Resume.pdf";
  await saveProfile({
    ...profile,
    documents: {
      resume: {
        name,
        mimeType: "application/pdf",
        base64: currentPdfBase64,
        savedAt: new Date().toISOString(),
      },
    },
  });
  setStatus(`Saved as ${name}. Autofill will attach it on the next Workday application.`);
});

// ------------------------------------------------------------------------------------
// startup
// ------------------------------------------------------------------------------------

async function refreshAll() {
  await loadProfile();
  await Promise.all([checkServer(), refreshPageState()]);
}

chrome.tabs.onActivated.addListener(refreshPageState);
chrome.tabs.onUpdated.addListener((_tabId, changeInfo) => {
  if (changeInfo.status === "complete") refreshPageState();
});
window.addEventListener("focus", refreshAll);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshAll();
});
refreshAll();
