"use strict";

const API = "http://127.0.0.1:8002";
const byId = (id) => document.getElementById(id);
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (character) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character])
);

let currentUrl = "";
let serverReady = false;
let currentTailoredResume = null;

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
    const response = await fetch(`${API}/health`);
    if (!response.ok) throw new Error(String(response.status));
    const health = await response.json();
    serverReady = health.status === "ok";
    byId("server-state").className = "server-state ok";
    byId("server-state").lastElementChild.textContent = `Ready / evidence v${health.evidence_version}`;
    setStatus("Connected. Open a job page or paste a description.");
    await loadProviders();
  } catch (_error) {
    serverReady = false;
    byId("server-state").className = "server-state bad";
    byId("server-state").lastElementChild.textContent = "Offline";
    setStatus("Start JME locally on port 8002, then reopen this panel.", true);
  }
  updateCount();
}

async function loadProviders() {
  try {
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
      : "Add OPENAI_API_KEY or ANTHROPIC_API_KEY to .env, then restart JME to enable AI.";
  } catch (_error) {
    byId("provider-note").textContent = "Provider status unavailable; verified mode remains ready.";
  }
}

function renderEntry(entry) {
  const organization = entry.url
    ? `<a href="${escapeHTML(entry.url)}">${escapeHTML(entry.organization)}</a>`
    : escapeHTML(entry.organization);
  return `<div class="entry">
    <div class="entry-head"><span>${organization}</span><span>${escapeHTML(entry.location || entry.dates)}</span></div>
    <div class="entry-sub"><span>${escapeHTML(entry.title)}</span><span>${entry.location ? escapeHTML(entry.dates) : ""}</span></div>
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
  return `<section><h3>${escapeHTML(title)}</h3>${entries.map(renderEntry).join("")}</section>`;
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
  if (customization.applied_mode === "ai") {
    byId("truth-badge").textContent = `${customization.rewritten_bullets} AI rewrites`;
    byId("edit-note").textContent = "Blue-marked bullets were rewritten from verified evidence. Hover to see the original and review before using.";
  } else {
    byId("truth-badge").textContent = "Verified selection";
    byId("edit-note").textContent = "No bullet wording was changed; evidence was selected and reordered only.";
  }
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
  setStatus("Selecting the strongest verified evidence...");
  try {
    const response = await fetch(`${API}/resume/tailor`, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify({
        job_description: byId("job-description").value,
        title: byId("job-title").value,
        company: byId("job-company").value,
        url: currentUrl,
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
    setStatus(customization.warning ? `${message} ${customization.warning}` : message);
  } catch (error) {
    setStatus(`Tailoring failed: ${error.message}`, true);
  } finally {
    button.firstElementChild.textContent = "Build tailored resume";
    updateCount();
  }
});

byId("copy").addEventListener("click", async () => {
  await navigator.clipboard.writeText(byId("resume").innerText);
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

byId("print").addEventListener("click", () => window.print());

checkServer();
