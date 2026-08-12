"use strict";

const API = "http://127.0.0.1:8002";
const STEERING_PROMPT_KEY = "jme.resumeSteeringPrompt";
const byId = (id) => document.getElementById(id);
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (character) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character])
);

let currentUrl = "";
let serverReady = false;
let currentTailoredResume = null;
let currentPdfUrl = "";
let chatMessages = [];
let chatProvider = "verified";

function updateCount() {
  const length = byId("job-description").value.length;
  byId("character-count").textContent = `${length.toLocaleString()} characters`;
  byId("tailor").disabled = !serverReady || length < 50;
  updateChatAvailability();
}

function updateChatAvailability() {
  const provider = byId("customization-mode").value;
  const ready = serverReady && currentTailoredResume && provider !== "verified";
  byId("chat-message").disabled = !ready;
  byId("send-chat").disabled = !ready || !byId("chat-message").value.trim();
  byId("chat-provider").textContent = ready
    ? byId("customization-mode").selectedOptions[0].textContent
    : "Choose an available AI provider";
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

function renderChatThread() {
  const thread = byId("chat-thread");
  if (!chatMessages.length) {
    thread.innerHTML = '<p class="chat-empty">Ask for a specific change. The revised Jake-template PDF will replace the preview above.</p>';
    return;
  }
  thread.innerHTML = chatMessages.map((message) => `
    <div class="chat-message ${message.role}">
      <span>${message.role === "user" ? "You" : "AI"}</span>
      <p>${escapeHTML(message.content)}</p>
    </div>`).join("");
  thread.scrollTop = thread.scrollHeight;
}

function resetChat(provider) {
  chatMessages = [];
  chatProvider = provider;
  byId("chat-message").value = "";
  renderChatThread();
  updateChatAvailability();
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
  byId("result").hidden = false;
  updateChatAvailability();
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
        steering_prompt: byId("steering-prompt").value.trim(),
        render_pdf: true,
        customization_mode: byId("customization-mode").value,
      }),
    });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    const data = await response.json();
    resetChat(byId("customization-mode").value);
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

byId("steering-prompt").value = localStorage.getItem(STEERING_PROMPT_KEY) || "";
byId("steering-prompt").addEventListener("input", () => {
  localStorage.setItem(STEERING_PROMPT_KEY, byId("steering-prompt").value);
});

byId("customization-mode").addEventListener("change", () => {
  if (chatProvider !== byId("customization-mode").value) {
    resetChat(byId("customization-mode").value);
  }
  updateChatAvailability();
});

byId("chat-message").addEventListener("input", updateChatAvailability);

byId("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = byId("chat-message");
  const message = input.value.trim();
  const provider = byId("customization-mode").value;
  if (!message || !currentTailoredResume || provider === "verified") return;

  chatMessages.push({ role: "user", content: message });
  chatMessages = chatMessages.slice(-12);
  input.value = "";
  renderChatThread();
  updateChatAvailability();
  input.disabled = true;
  byId("send-chat").disabled = true;
  byId("send-chat").textContent = "Revising...";
  setStatus("Applying your instruction and recompiling the one-page PDF...");
  try {
    const response = await fetch(`${API}/resume/chat`, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify({
        job_description: byId("job-description").value,
        title: byId("job-title").value,
        company: byId("job-company").value,
        url: currentUrl,
        steering_prompt: byId("steering-prompt").value.trim(),
        provider,
        messages: chatMessages,
        render_pdf: true,
      }),
    });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    const data = await response.json();
    renderResume(data);
    chatMessages.push({ role: "assistant", content: data.chat_reply });
    chatMessages = chatMessages.slice(-12);
    renderChatThread();
    const actions = [
      data.customization.rewritten_bullets ? `${data.customization.rewritten_bullets} rewritten` : "",
      data.chat_removed_bullets ? `${data.chat_removed_bullets} removed` : "",
      data.chat_prioritized_bullets ? `${data.chat_prioritized_bullets} reprioritized` : "",
    ].filter(Boolean).join(", ");
    setStatus(actions ? `Revision complete: ${actions}.` : data.chat_reply);
  } catch (error) {
    setStatus(`Revision failed: ${error.message}`, true);
  } finally {
    byId("send-chat").textContent = "Send revision";
    updateChatAvailability();
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

window.addEventListener("focus", checkServer);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) checkServer();
});
checkServer();
