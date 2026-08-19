"use strict";

// Turns a fill report into plain text a person can paste somewhere to get help.
//
// Kept free of the DOM and of `chrome.*` so the test suite can load it directly and hold
// it to its one real promise: the text names fields and failure reasons, never the values
// typed into them. A report that leaked a home address would undo the point of an
// extension whose whole claim is that the profile never leaves the device.

(function (global) {
  /** Field names and outcomes only — the `detail` of a filled field is its value. */
  function filledLines(report) {
    if (!report.filled.length) return ["  (nothing)"];
    return report.filled.map((item) => `  ${item.field}`);
  }

  function section(title, lines) {
    return lines.length ? [`${title}`, ...lines, ""] : [];
  }

  /**
   * Render `report` as a diagnostic block.
   *
   * Skip reasons and warnings are kept verbatim: they are the whole diagnostic value, and
   * what they quote is an option label or a value that failed to match rather than the
   * contact details in the profile. They are shown on screen before this is copied, so
   * they can be read first.
   */
  function buildDiagnostic(report) {
    if (!report) return "";
    const counts = [
      `${report.filled.length} filled`,
      `${report.skipped.length} skipped`,
      `${report.notFound.length} not found`,
    ].join(" · ");

    const lines = [
      "JME Apply — fill report",
      `host: ${report.host || "unknown"}`,
      `step: ${report.step}`,
      counts,
      "",
      ...section("FILLED (field names only; values are not included)", filledLines(report)),
      ...section(
        "SKIPPED (a control was found, nothing was typed)",
        report.skipped.map((item) => `  ${item.field}: ${item.reason}`)
      ),
      ...section(
        "NOT FOUND (no control on this page matched)",
        report.notFound.map((field) => `  ${field}`)
      ),
      ...section("WARNINGS", report.warnings.map((warning) => `  ${warning}`)),
    ];
    return lines.join("\n").trimEnd();
  }

  global.JMEReport = { buildDiagnostic };
})(globalThis);
