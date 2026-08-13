# JME Resume Tailor browser extension

This unpacked Chrome/Edge extension opens as a side panel on a job page. It can extract
the visible description from the active tab or accept pasted/uploaded text, then sends
that text to the local Job Match Engine at `127.0.0.1:8002`.

The engine always starts by selecting and reordering bullets from
`jme/resume/profile.json`. Verified mode stops there. Claude Code, OpenAI, Claude,
Gemini, and Kimi modes ask the local backend for evidence-linked rewrites, validate
them, and fall back to verified mode on provider or validation failure. The extension
never stores or receives API keys, and it cannot tell which provider holds a
credential — it only reads the availability flags the backend reports.

The **Always follow these instructions** field is saved locally in the extension and
steers every build. After generating a resume with an available AI provider, use the
embedded chat to request another rewrite, removal, or reprioritization. Each turn rebuilds
from `jme/resume/profile.json`; the browser cannot add claims to the evidence bank.

## Install locally

1. Start the application with `jme serve start --port 8002`.
2. Open `chrome://extensions` in Chrome or `edge://extensions` in Edge.
3. Enable **Developer mode**.
4. Choose **Load unpacked** and select this `extension` directory.
5. Pin **JME Resume Tailor**, open a job page, and click the extension icon.

### AI options

**AI rewrite · Claude Code** needs no API key. Install the Claude Code CLI, run
`claude auth login` once, and restart JME; the backend then runs `claude -p` locally for
each rewrite. That work draws on the Claude subscription the CLI is signed into rather
than Anthropic API credits, and it stops when that subscription's usage limit is
reached. A free Claude.ai account does not include this programmatic route, so the
option stays greyed out there.

The other options bill an API key instead: add `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`GEMINI_API_KEY`, or `MOONSHOT_API_KEY` (Kimi) to the repository's `.env` file before
starting the application. Those keys are never used by the Claude Code path — the
backend strips them from its environment before launching the CLI, so a configured key
can never silently redirect subscription work onto metered API billing.

Without any key or CLI, **Verified selection** remains fully usable.

For exact Jake-template PDF preview and download, install the local LaTeX engine once
from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-tectonic.ps1
```

Restart JME afterward. Compilation happens locally; the extension never sends resume
data to an online LaTeX service.

Use **Use current job page** for a normal listing, or paste/upload a `.txt`, `.md`, or
`.html` job description. The result can be copied, downloaded as canonical Jake-template
LaTeX, or downloaded as the actual locally compiled PDF.
