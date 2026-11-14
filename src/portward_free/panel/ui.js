// The panel's shared pieces (inlined after the host bridge), lifted from the gateway's widget_ui.js and trimmed:
// building nodes from text only, the empty and failed states, and how values and column names read.
"use strict";
const PortwardUI = (() => {
  // A node from a tag, attributes and children. Children are text or nodes, never markup; null and false are skipped.
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v === true) node.setAttribute(k, "");
      else if (v !== false && v != null) node.setAttribute(k, v);
    }
    return put(node, ...children);
  }

  // Adds children to a node already on the page, as el does: never node.append(null), which adds the text "null".
  function put(node, ...children) {
    for (const child of children.flat()) {
      if (child == null || child === false) continue;
      node.append(child instanceof Node ? child : String(child));
    }
    return node;
  }

  // Words as a sentence: a capital first letter and a full stop.
  function sentence(text) {
    const words = String(text == null ? "" : text).trim();
    if (!words) return "";
    const capped = words[0].toUpperCase() + words.slice(1);
    return /[.!?]$/.test(capped) ? capped : capped + ".";
  }

  // The message of a tool call that failed (isError), or null for an answer. A failure is never shown as "nothing".
  function failure(result) {
    if (!result || result.isError !== true) return null;
    const text = (result.content || []).filter((c) => c && c.type === "text").map((c) => c.text).join(" ");
    return sentence(text) || "Portward didn't say why.";
  }

  // A call that failed: what didn't happen, in plain words, and what to do next.
  function problem(heading, message, next) {
    return el("div", { class: "problem", role: "alert" }, el("h2", {}, heading), el("p", {}, sentence(message)),
      next ? el("p", { class: "next" }, next) : null);
  }

  // Nothing to show: one sentence of what would be here and, when there is one, the next step.
  function empty(text, next) {
    return el("div", { class: "empty" }, el("p", {}, text), next ? el("p", { class: "next" }, next) : null);
  }

  // A column name as words: "open_invoices" reads "Open invoices". The name itself stays in the header's title.
  function label(column) {
    const words = String(column).replace(/_+/g, " ").trim().toLowerCase();
    return words ? words[0].toUpperCase() + words.slice(1) : String(column);
  }

  const PLAIN_NUMBER = /^-?(?:0|[1-9]\d*)(?:\.\d+)?$/;  // no leading zero (a code), no exponent
  function isNumber(value) {
    return (typeof value === "number" && Number.isFinite(value)) || (typeof value === "string" && PLAIN_NUMBER.test(value));
  }

  // A column of numbers is set as data: right-aligned with tabular figures. Every value given must be a plain number.
  function numeric(values) {
    const given = values.filter((v) => v !== null && v !== undefined && v !== "");
    return given.length > 0 && given.every(isNumber);
  }

  // Identifiers, years and codes keep their digits together; amounts and counts group thousands.
  const KEEP_DIGITS = /(?:^|_)(?:id|year|yr|number|num|no|code|zip|postcode|phone)$/i;
  function number(value, column) {
    const text = String(value);
    if (!isNumber(value) || KEEP_DIGITS.test(String(column || ""))) return text;
    const [whole, fraction] = text.split(".");
    const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return fraction === undefined ? grouped : grouped + "." + fraction;
  }

  // How a value wraps in a results cell: a short value without spaces (an id, a date, a code, an email) stays whole,
  // so the table stacks before it would split one; long text gets room rather than one word a line.
  const WHOLE_MAX = 32;
  const PROSE_MIN = 25;
  const BREAKABLE_MIN = 16;  // a compact table may break a single word this long (an email), never a shorter one
  function cellWrap(value) {
    const text = value == null ? "" : String(value);
    if (!text) return null;
    if (!/\s/.test(text)) return text.length <= WHOLE_MAX ? (text.length > BREAKABLE_MIN ? "whole long" : "whole") : null;
    return text.length >= PROSE_MIN ? "prose" : null;
  }

  // Explicit roles on a table whose rows may become blocks: display block or grid drops a table's semantics in
  // WebKit, and with them each cell's column header. A th is a column header unless it heads a row.
  const TABLE_ROLES = { TABLE: "table", THEAD: "rowgroup", TBODY: "rowgroup", TR: "row", TD: "cell" };
  function tableRoles(node) {
    if (node.tagName === "TH") node.setAttribute("role", node.getAttribute("scope") === "row" ? "rowheader" : "columnheader");
    else if (TABLE_ROLES[node.tagName]) node.setAttribute("role", TABLE_ROLES[node.tagName]);
    if (node.tagName !== "TH" && node.tagName !== "TD") {
      for (const child of node.childNodes || []) if (child.tagName) tableRoles(child);
    }
    return node;
  }

  function plural(count, word) { return count + " " + word + (count === 1 ? "" : "s"); }

  // Where a value came from (the value_source tool's answer) as plain lines: what the column is, the source table
  // columns behind it, and each filter the query applied. Text only; the panel puts it in nodes with el.
  const FILTER_WORDS = { join: "Joined where ", where: "Only rows where ", having: "Only groups where " };
  function sourceText(found) {
    const name = label(found.column);
    const tables = [...new Set((found.from || []).map((o) => o.table))].join(", ");
    const from = (found.from || []).map((o) => (o.column ? `${o.table}.${o.column}`
      : found.traced && found.kind === "aggregate" ? `${o.table} (rows counted)` : o.table));
    const group = (found.group || []).map((g) => `${String(g.column).replace(/_+/g, " ").toLowerCase()} ${g.value}`);
    let summary;
    if (!found.traced) summary = `${name} comes from ${tables}; Portward couldn't trace the exact column.`;
    else if (found.kind === "column") summary = `${name} is read as it is from ${from[0]}.`;
    else summary = `${name} is ${found.expression}${group.length ? ", for " + group.join(", ") : ""}.`;
    const filters = (found.filters || []).map((f) => (f.clause === "limit" ? `First ${f.sql} rows only`
      : (FILTER_WORDS[f.clause] || "") + f.sql));
    return { summary, from, filters: filters.length ? filters : ["No filters: every row of the table"] };
  }

  // The usage-counts question, when the server sent it with this result (usage.USAGE_META in _meta): shown in
  // `mount`, and only once it is on the page does the panel tell the server (usage_seen); nothing is counted or
  // sent before that. "Turn off" says no (usage_choice). True when it was shown.
  const USAGE_META = "portward/usage_question";
  function showUsage(result, mount, callTool) {
    const question = result && result._meta && result._meta[USAGE_META];
    if (!question || !question.text) return false;
    const said = el("p", { class: "caption", role: "status" });
    const off = el("button", { class: "quiet", type: "button", onclick: async () => {
      try {
        await callTool("usage_choice", { on: false });
        said.textContent = "Usage counts are off. The counts kept on this computer are deleted.";
        off.remove();
      } catch (err) { said.textContent = sentence(err.message); }
    } }, "Turn usage counts off");
    mount.append(el("aside", { class: "usage", "aria-label": "Usage counts" }, el("p", null, question.text), off, said));
    callTool("usage_seen", {}).catch(() => {});  // not shown as seen if the host refuses: asked again next time
    return true;
  }

  // An offer of Portward Personal (#116), shown when the user reaches for one of its moments in the panel ("profile",
  // or "anonymised_or_totals" from a column menu). The app-only personal_offer tool says whether there is one: the
  // first time only, on Free only, in offers.py's words. The AI never relays it. The button turns that kind off for
  // good (dismiss_offer). True when an offer was shown.
  async function showOffer(moment, mount, callTool) {
    let offer = null;
    try {
      offer = ((await callTool("personal_offer", { moment })) || {}).structuredContent?.offer || null;
    } catch (err) { return false; }  // no offer is better than a broken menu
    if (!offer || !offer.text) return false;
    const said = el("p", { class: "caption", role: "status" });
    const off = el("button", { class: "quiet", type: "button", onclick: async () => {
      try {
        await callTool("dismiss_offer", { kind: offer.kind });
        said.textContent = "Done: this won't be shown again on this computer.";
        off.remove();
      } catch (err) { said.textContent = sentence(err.message); }
    } }, "Don't show this again");
    mount.append(el("aside", { class: "offer", "aria-label": "Portward Personal" }, el("p", null, offer.text), off,
                    said));
    return true;
  }

  return { el, put, sentence, failure, problem, empty, label, numeric, number, cellWrap, tableRoles, plural, sourceText,
           showUsage, showOffer };
})();
