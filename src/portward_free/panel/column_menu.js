// A column header's menu (#119, design "Free: panel B"): "Keep from Claude" and "Note". Each entry asks the server
// which table column the result column reads (the app-only column_menu), then acts on that column: keep_from_claude
// adds a block rule, from the next result on (the rows on show stay as they were pulled); column_note writes the
// note Claude reads with the schema. What either shows goes in a section the caller mounts below the table. Nothing
// here goes to the AI. Inlined after ui.js. Same shape as PortwardLevels.menuEntries (#120), so one header menu
// lists both (results.html columnMenu).
"use strict";
const PortwardColumnMenu = (() => {
  const { el } = PortwardUI;

  async function lookUp(pullId, column, callTool) {
    return ((await callTool("column_menu", { pull_id: pullId, column })) || {}).structuredContent || {};
  }

  function named(target) { return el("span", { class: "id" }, `${target.table}.${target.column}`); }

  function section(label, ...children) {
    return el("section", { class: "colmenu-said", "aria-label": label }, ...children);
  }

  // Keep the column from the AI. `changed({kept: column})` tells the panel this result's rules are now out of date.
  async function keep(pullId, column, callTool, who, changed) {
    const heading = el("h2", { tabindex: "-1" }, `Keep ${column} from ${who}`);
    let found;
    try {
      found = await lookUp(pullId, column, callTool);
      if (found.why) return section("Keep from " + who, heading, el("p", {}, PortwardUI.sentence(found.why)));
      if (found.kept) {
        return section("Keep from " + who, heading,
          el("p", {}, named(found.target), ` is already kept from ${who}. Let ${who} read it again from the Portward page.`));
      }
      const done = ((await callTool("keep_from_claude", { pull_id: pullId, column })) || {}).structuredContent || {};
      if (changed) changed({ kept: column });
      return section("Keep from " + who, heading,
        el("p", { role: "status" }, "Kept: ", named(done.kept || found.target),
          ` won't go to ${who} from the next result on. This result stays as it was pulled.`),
        el("p", { class: "caption" }, `To let ${who} read it again, unblock it on the Portward page.`));
    } catch (err) {
      return section("Keep from " + who, heading, PortwardUI.problem("Nothing was kept", err.message, null));
    }
  }

  // Write or edit the column's note: a textarea with the note as it is, Save note and Cancel.
  async function note(pullId, column, callTool, who) {
    const heading = el("h2", { tabindex: "-1" }, `Note on ${column}`);
    let found;
    try {
      found = await lookUp(pullId, column, callTool);
    } catch (err) {
      return section("Note", heading, PortwardUI.problem("The note can't be opened", err.message, null));
    }
    if (found.why) return section("Note", heading, el("p", {}, PortwardUI.sentence(found.why)));
    const id = "colnote-" + Math.random().toString(36).slice(2);
    const area = el("textarea", { id, rows: "3", maxlength: "2000" }, found.note || "");
    const said = el("p", { class: "caption", role: "status" });
    const box = section("Note", heading);
    const save = el("button", { class: "primary", type: "button", onclick: async () => {
      try {
        const done = ((await callTool("column_note", { pull_id: pullId, column, note: area.value })) || {})
          .structuredContent || {};
        said.textContent = done.note ? `Saved. ${who} reads it with the column names from its next question.`
                                     : "Saved: the note is removed.";
      } catch (err) { said.textContent = "Not saved. " + PortwardUI.sentence(err.message); }
    } }, "Save note");
    const cancel = el("button", { class: "quiet", type: "button", onclick: () => box.remove() }, "Cancel");
    PortwardUI.put(box,
      el("label", { for: id }, "What ", named(found.target), " means, in your words"),
      area,
      el("p", { class: "actions" }, save, cancel),
      said);
    return box;
  }

  // The menu entries for one column: {label, run}. run() hands the section it makes to mount(section).
  function menuEntries(pullId, column, callTool, mount, who, changed) {
    const name = who || "the AI";
    return [
      { label: `Keep from ${name}`, run: async () => mount(await keep(pullId, column, callTool, name, changed)) },
      { label: "Say what this means", run: async () => mount(await note(pullId, column, callTool, name)) },
    ];
  }

  return { menuEntries };
})();
