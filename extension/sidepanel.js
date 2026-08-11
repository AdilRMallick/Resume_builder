"use strict";

const API = "http://127.0.0.1:8002";
const byId = (id) => document.getElementById(id);
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (character) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character])
);

let currentUrl = "";
let serverReady = false;

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
  } catch (_error) {
    serverReady = false;
    byId("server-state").className = "server-state bad";
    byId("server-state").lastElementChild.textContent = "Offline";
    setStatus("Start JME locally on port 8002, then reopen this panel.", true);
  }
  updateCount();
}

function renderEntry(entry) {
  const organization = entry.url
    ? `<a href="${escapeHTML(entry.url)}">${escapeHTML(entry.organization)}</a>`
    : escapeHTML(entry.organization);
  return `<div class="entry">
    <div class="entry-head"><span>${organization}</span><span>${escapeHTML(entry.location || entry.dates)}</span></div>
    <div class="entry-sub"><span>${escapeHTML(entry.title)}</span><span>${entry.location ? escapeHTML(entry.dates) : ""}</span></div>
    <ul>${entry.bullets.map((bullet) => `<li>${escapeHTML(bullet.text)}</li>`).join("")}</ul>
  </div>`;
}

function renderSection(title, entries) {
  return `<section><h3>${escapeHTML(title)}</h3>${entries.map(renderEntry).join("")}</section>`;
}

function renderResume(data) {
  const contacts = data.contact.map((item) => item.url
    ? `<a href="${escapeHTML(item.url)}">${escapeHTML(item.value)}</a>`
    : escapeHTML(item.value)).join(" &nbsp;|&nbsp; ");
  const target = [data.target.company, data.target.title].filter(Boolean).join(" - ") || "Pasted job description";
  const skills = Object.entries(data.skills).map(([category, values]) =>
    `<div><strong>${escapeHTML(category)}:</strong> ${values.map(escapeHTML).join(", ")}</div>`
  ).join("");

  byId("match-chips").innerHTML = (data.matched_skills.length ? data.matched_skills.slice(0, 12) : ["title-based selection"])
    .map((skill) => `<span class="match-chip">${escapeHTML(skill)}</span>`).join("");
  byId("resume").innerHTML = `
    <h2>${escapeHTML(data.name)}</h2>
    <p class="headline">${escapeHTML(data.headline)}</p>
    <div class="contact">${contacts}</div>
    <p class="target"><strong>Tailored for:</strong> ${escapeHTML(target)}</p>
    ${renderSection("Education", data.education)}
    ${renderSection("Experience", data.experience)}
    ${renderSection("Selected Projects", data.projects)}
    ${renderSection("Leadership", data.leadership)}
    <section><h3>Technical Skills</h3><div class="skills">${skills}<div><strong>Certifications:</strong> ${data.certifications.map(escapeHTML).join(", ")}</div></div></section>`;
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
      }),
    });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    const data = await response.json();
    renderResume(data);
    setStatus(`${data.matched_skills.length} relevant evidence signals found. No bullet was rewritten.`);
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
  const styles = `body{margin:0;background:#fff}.resume{width:7.4in;margin:auto;padding:.45in .55in;color:#171b24;font:10px/1.35 Arial,sans-serif}.resume h2{text-align:center;text-transform:uppercase}.resume .headline,.resume .contact{text-align:center}.resume .target{padding:7px;border-left:3px solid #5877e8;background:#f2f4fa}.resume section{margin-top:10px}.resume h3{border-bottom:1px solid #222;text-transform:uppercase;font-size:11px}.entry-head,.entry-sub{display:flex;justify-content:space-between}.entry-head{font-weight:bold}.entry-sub{font-style:italic;color:#555}.resume ul{margin:3px 0;padding-left:17px}`;
  const documentHTML = `<!doctype html><html><head><meta charset="utf-8"><title>Tailored Resume</title><style>${styles}</style></head><body>${byId("resume").outerHTML}</body></html>`;
  const href = URL.createObjectURL(new Blob([documentHTML], { type: "text/html" }));
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = "Adil_Mallick_Tailored_Resume.html";
  anchor.click();
  URL.revokeObjectURL(href);
  setStatus("Downloaded an editable, printable HTML resume.");
});

byId("print").addEventListener("click", () => window.print());

checkServer();
