// The moment a Free user reaches for a Personal level (#120): a column menu's "Anonymise" and "Totals only"
// entries, marked Personal, and what choosing one shows: that column of this result as the AI would get it at that
// level (the app-only level_preview), with the offer beside it (PortwardUI.showOffer: offers.py's words, the first
// time only). A column the user keeps from the AI is never read for it: the server answers "kept" with no values.
// Nothing here goes to the AI. Inlined after ui.js. The header menu (#119) lists menuEntries(...) beside its own.
"use strict";
const PortwardLevels = (() => {
  const { el } = PortwardUI;
  const MOMENT = "anonymised_or_totals";
  const ENTRIES = [{ level: "anonymised", label: "Anonymise (Personal)" },
                   { level: "totals", label: "Totals only (Personal)" }];

  // The menu entries for one column: {label, level, run}. run() makes the preview and hands it to mount(section).
  function menuEntries(pullId, column, callTool, mount, who) {
    return ENTRIES.map((entry) => ({
      label: entry.label, level: entry.level,
      run: async () => mount(await show(pullId, column, entry.level, callTool, who || "the AI")),
    }));
  }

  function heading(column, level) {
    const name = el("span", { class: "id" }, column);
    return el("h2", { tabindex: "-1" }, "Show ", name, level === "totals" ? " to the AI as totals only" : " to the AI as stand-ins");
  }

  function standIns(found, who) {
    const rows = found.rows || [];
    if (!rows.length) return [el("p", {}, `This result has no values of ${found.column} to show.`)];
    return [
      el("p", { class: "lead" }, `${who} would get a code in place of each value. The same value always gets the same ` +
        "code, so it can count repeats and join tables on them, but never reads a value."),
      PortwardUI.tableRoles(el("table", {},
        el("thead", {}, el("tr", {}, el("th", { scope: "col" }, "You see"), el("th", { scope: "col" }, `${who} would get`))),
        el("tbody", {}, rows.map(([mine, theirs]) => el("tr", {},
          el("td", {}, mine == null ? "" : String(mine)), el("td", { class: "ai" }, theirs == null ? "" : theirs)))))),
      el("p", { class: "caption" }, `From your own rows in this result. Only you see this; nothing here went to ${who}.`),
    ];
  }

  function totals(found, who) {
    const t = found.totals || {};
    const rows = PortwardUI.plural(t.rows || 0, "row");
    if (t.small) {
      return [el("p", {}, `This result has ${rows}, fewer than ${t.minimum}: ${who} would get nothing for ${found.column}.`)];
    }
    const sums = t.sum === undefined ? [] : [`sum ${PortwardUI.number(t.sum)}`, `average ${PortwardUI.number(t.average)}`];
    return [
      el("p", { class: "lead" }, `${who} would get totals, never one row.`),
      el("p", { class: "tot" }, [`${rows} counted`, ...sums].join(", ")),
      el("p", { class: "caption" }, `Worked out from the rows of this result shown here. Nothing here went to ${who}.`),
    ];
  }

  // The preview of one column at one level, as a section for the caller to put on the page, with the offer in it.
  async function show(pullId, column, level, callTool, who) {
    const name = who || "the AI";
    const box = el("section", { class: "level-preview", "aria-label": "Portward Personal" }, heading(column, level));
    let found = null;
    try {
      found = ((await callTool("level_preview", { pull_id: pullId, column, level })) || {}).structuredContent || {};
    } catch (err) {
      box.append(PortwardUI.problem("No preview for this column", err.message, null));
    }
    if (found && found.kept) {
      box.append(el("p", {}, `You keep ${column} from ${name}, so Portward doesn't read it, not even for this preview.`));
    } else if (found) {
      PortwardUI.put(box, ...(level === "totals" ? totals(found, name) : standIns(found, name)));
    }
    await PortwardUI.showOffer(MOMENT, box, callTool);
    return box;
  }

  return { menuEntries, show };
})();
