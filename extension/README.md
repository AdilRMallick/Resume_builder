# JME Apply

An unpacked Chrome/Edge extension with two halves:

- **Workday autofill** — fills a Workday application from a profile stored in this
  browser. Runs entirely on your device; no account, no license, no fill limit.
- **Resume tailor** — the side panel's second tab, which sends a job description to the
  local Job Match Engine on `127.0.0.1:8002` and returns a tailored, locally compiled PDF.

The two halves are independent. Autofill needs no backend at all.

## Install

1. Open `chrome://extensions` in Chrome or `edge://extensions` in Edge.
2. Enable **Developer mode**.
3. Choose **Load unpacked** and select this `extension` directory.
4. Pin **JME Apply**, then click it to open the side panel.

For the tailor tab, start the backend first with `jme serve start --port 8002`.

## First run

Open the side panel and press **Edit profile**. Fill in at least your name and email —
the rest is optional and can be added as you go. If the backend is running,
**Import from JME** seeds your work history, education, and skills from
`jme/resume/profile.json`; it only fills blanks, so anything you typed is kept.

Attach a resume file in the same editor, or compile one on the Tailor tab and press
**Use this PDF for autofill uploads**.

## Filling an application

Open a Workday application and press **Fill this page**, either in the side panel or with
the floating button in the page. The extension fills one step at a time and reports what
it filled, what it skipped, and why.

It never presses Next and never submits. Review each page yourself.

Two behaviours are off by default and live in the profile editor:

- **Voluntary disclosures** (gender, race, veteran status, disability). Optional on every
  application, so those pages stay blank until you enter answers and turn this on.
- **Overwrite existing values.** Off, so anything Workday parsed from your uploaded
  resume is left alone.

## Where your data lives

The profile — including the attached resume file — is stored in `chrome.storage.local`
and read only by this extension. No file under `autofill/` may make a network call, and
the test suite fails the build if one appears. Removing the extension deletes the profile.

The tailor tab is the only part that talks to a server, and that server is the JME
backend on your own machine. Provider API keys stay in the repository's `.env`; they
never enter Chrome.

## How the autofill engine is put together

| File | Responsibility |
| --- | --- |
| `profile/schema.js` | The profile shape, its defaults, and the normaliser every reader runs |
| `profile/editor.html\|css\|js` | The full-page profile editor (the extension's options page) |
| `autofill/dom.js` | Visibility, accessible names, React-safe value writes, waiting, fuzzy matching |
| `autofill/widgets.js` | Drivers for dropdowns, multi-selects, dates, radios, checkboxes, file inputs |
| `autofill/fields.js` | What each field means and how to recognise it, plus the resolver |
| `autofill/runner.js` | Step detection, repeating sections, and the fill report |
| `autofill/content.js` | The floating button and the side panel's message endpoint |

Two things make Workday harder than an ordinary form. Its class names and element ids are
generated per build, so the only stable hooks are `data-automation-id` attributes and a
control's accessible name; and React owns every input's value, so a plain assignment is
reverted on the next render. Each field spec therefore carries automation ids, keyword
phrases, and negative keywords, and every write goes through the prototype's native value
setter.

## Tests

```bash
pytest tests/extension                       # everything below, from the repo root

cd tests/extension
node autofill_checks.js                      # decision logic: no dependencies
npm install && node dom_checks.js            # the engine against synthetic Workday forms
```

`autofill_checks.js` covers what the engine decides to type — profile normalisation,
resume seeding, date parsing, option matching, the field specs. It needs nothing but
node, so it always runs.

`dom_checks.js` covers whether it succeeds in typing it, driving the real engine against
fixtures that reproduce the shapes that make Workday hard: a portal-rendered listbox, a
typeahead that only shows options after input, split date spinbuttons, numbered panels
behind an Add button. It needs jsdom, so it is opt-in and skips until you `npm install`.

The one thing neither can prove is that a given tenant's markup matches the fixtures.
That is what the fill report is for: it names every field it could not place.

## LaTeX PDFs

For exact Jake-template PDF preview and download, install the local LaTeX engine once
from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-tectonic.ps1
```

Restart JME afterward. Compilation happens locally; the extension never sends resume data
to an online LaTeX service.

To enable an AI tailoring option, add `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`GEMINI_API_KEY`, or `MOONSHOT_API_KEY` (Kimi) to the repository's `.env` file before
starting the backend. Without a key, **Verified selection** remains fully usable.
