/* AI Usage Board - usage page reader.
 *
 * Evaluated by the widget inside its own hidden browser, on exactly two
 * pages: claude.ai's usage settings and gemini.google.com/usage. It returns
 * one row per usage window - a name, the percent used and the reset line -
 * and touches nothing else on the page: no chat text, no account details,
 * no storage, no network.
 *
 * How each site lays its numbers out (checked against the live pages):
 *
 *   claude.ai   every window is a role="meter" whose aria-valuetext reads
 *               "45% 사용됨". The meter is named after what it covers
 *               ("현재 세션", "모든 모델") and sits in a <section> whose
 *               heading names the window ("주간 한도").
 *   gemini      every window is an <li data-testid="…-usage"> whose lines
 *               read: name, the percent used, the reset time (in either
 *               order), plus the odd icon ligature such as "info".
 */
(() => {
  "use strict";

  // -- wording -------------------------------------------------------------
  // A figure is only trusted with a word that says which way it counts.
  const PERCENT = String.raw`(\d{1,3}(?:[.,]\d+)?)\s*%`;
  const SAYS_USED = new RegExp(`${PERCENT}\\s*(?:used|사용(?:됨|량)?)`, "i");
  const SAYS_LEFT = new RegExp(`${PERCENT}\\s*(?:left|remaining|남음|남은)`, "i");
  const NAMES_WINDOW = /세션|주간|이번\s?주|일간|현재\s?사용량|session|week|daily|5[\s.-]?hour|current usage/i;
  const NAMES_RESET = /초기화|재설정|첫 메시지|reset|refill|starts? when/i;
  const ICON_WORD = /^[a-z_]+$/; // Material icon ligatures render as bare words

  const LIMIT = { rows: 8, label: 48, reset: 60 };

  const tidy = (text, max) =>
    String(text ?? "")
      .replace(/[-]/g, "") // private-use icon glyphs
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, max);

  const linesOf = (node) =>
    String(node?.innerText ?? "")
      .split("\n")
      .map((line) => tidy(line, 120))
      .filter((line) => line && !ICON_WORD.test(line));

  /** On screen and not hidden from assistive tech (the site's own "ignore me"). */
  function shown(node) {
    if (!node || node.closest('[aria-hidden="true"]')) return false;
    return node.getClientRects().length !== 0;
  }

  const BAR = '[role="meter"], [role="progressbar"]';

  /** Percent used, or null when the text does not say which way it counts. */
  function usedPercent(text) {
    const value = (match) => Number(match[1].replace(",", "."));
    const used = SAYS_USED.exec(text);
    const left = used ? null : SAYS_LEFT.exec(text);
    const figure = used ? value(used) : left ? 100 - value(left) : NaN;
    return Number.isFinite(figure) && figure >= 0 && figure <= 100 ? figure : null;
  }

  const resetLine = (lines) =>
    tidy(lines.find((line) => NAMES_RESET.test(line) && usedPercent(line) === null), LIMIT.reset);

  // -- claude.ai -----------------------------------------------------------
  /** What the meter calls itself: its aria-label, else the text it points at. */
  function meterName(meter, doc) {
    const direct = meter.getAttribute("aria-label");
    if (direct) return tidy(direct, LIMIT.label);
    const parts = [];
    for (const id of String(meter.getAttribute("aria-labelledby") || "").trim().split(/\s+/)) {
      const target = id && doc.getElementById(id);
      if (target) parts.push(target.textContent);
    }
    return tidy(parts.join(" "), LIMIT.label);
  }

  /** The block a meter belongs to: the largest ancestor holding no other meter. */
  function meterRow(meter, stop) {
    let row = meter;
    for (let node = meter.parentElement; node && node !== stop; node = node.parentElement) {
      if (node.querySelectorAll(BAR).length > 1) break;
      row = node;
    }
    return row;
  }

  function readClaude(doc) {
    const rows = [];
    for (const meter of doc.querySelectorAll(BAR)) {
      if (!shown(meter)) continue;
      const section = meter.closest("section");
      const heading = linesOf(section?.querySelector('h1, h2, h3, h4, [role="heading"]'))[0] || "";
      const name = meterName(meter, doc);

      // "현재 세션" names its window itself; "모든 모델" needs its heading.
      let label = "";
      if (NAMES_WINDOW.test(name)) label = name;
      else if (NAMES_WINDOW.test(heading)) label = name ? `${tidy(heading, 24)} · ${name}` : heading;
      if (!label) continue;

      const row = meterRow(meter, section);
      const used = usedPercent(meter.getAttribute("aria-valuetext") || "") ?? usedPercent(row.innerText || "");
      if (used === null) continue;
      rows.push({ label: tidy(label, LIMIT.label), used, resetText: resetLine(linesOf(row)) });
    }
    return rows;
  }

  // -- gemini.google.com ---------------------------------------------------
  function readGemini(doc) {
    const rows = [];
    for (const card of doc.querySelectorAll('li[data-testid$="-usage"]')) {
      if (!shown(card)) continue;
      const lines = linesOf(card);
      const used = usedPercent(lines.join("\n"));
      if (used === null) continue;
      const label = lines.find((line) => NAMES_WINDOW.test(line)) || lines[0];
      rows.push({ label: tidy(label, LIMIT.label), used, resetText: resetLine(lines) });
    }
    return rows;
  }

  // -- routing -------------------------------------------------------------
  const PAGES = [
    {
      provider: "claude",
      host: "claude.ai",
      // Opened directly, or as the settings overlay on any page (#settings/usage).
      matches: (loc) =>
        loc.pathname.replace(/\/+$/, "") === "/settings/usage" ||
        String(loc.hash || "").replace(/^#\/?/, "").split("?")[0] === "settings/usage",
      read: readClaude,
    },
    {
      provider: "gemini",
      host: "gemini.google.com",
      matches: (loc) => loc.pathname.replace(/\/+$/, "") === "/usage",
      read: readGemini,
    },
  ];

  const pageFor = (loc) =>
    loc.protocol === "https:" ? PAGES.find((page) => page.host === loc.hostname && page.matches(loc)) : undefined;

  function read(doc = document, loc = window.location) {
    const page = pageFor(loc);
    if (!page) return null;
    const snapshot = { provider: page.provider, windows: page.read(doc).slice(0, LIMIT.rows) };
    // Silent when there are figures; otherwise say the page has not shown them yet.
    snapshot.note = snapshot.windows.length === 0 ? "사용량 화면을 불러오는 중입니다" : "";
    snapshot.pageUpdatedAt = Date.now() / 1000;
    return snapshot;
  }

  globalThis.AIUsageBoardReader = { read, usedPercent, providerFor: (loc) => pageFor(loc)?.provider ?? null };
})();
