# JME Resume Tailor browser extension

This unpacked Chrome/Edge extension opens as a side panel on a job page. It can extract
the visible description from the active tab or accept pasted/uploaded text, then sends
that text to the local Job Match Engine at `127.0.0.1:8002`.

The engine always starts by selecting and reordering bullets from
`jme/resume/profile.json`. Verified mode stops there. OpenAI, Claude, Gemini, and Kimi modes ask the
local backend for evidence-linked rewrites, validate them, and fall back to verified
mode on provider or validation failure. The extension never stores or receives API keys.

## Install locally

1. Start the application with `jme serve start --port 8002`.
2. Open `chrome://extensions` in Chrome or `edge://extensions` in Edge.
3. Enable **Developer mode**.
4. Choose **Load unpacked** and select this `extension` directory.
5. Pin **JME Resume Tailor**, open a job page, and click the extension icon.

To enable an AI option, add `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, or
`MOONSHOT_API_KEY` (Kimi) to the repository's `.env` file before starting the
application. Without a key, **Verified selection** remains fully usable.

Use **Use current job page** for a normal listing, or paste/upload a `.txt`, `.md`, or
`.html` job description. The result is editable in the panel and can be copied,
downloaded as canonical Jake-template LaTeX, or printed to PDF.
