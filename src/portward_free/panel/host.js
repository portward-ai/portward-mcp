// The results panel's bridge to the host (inlined into the page by results_panel.page). Lifted from the gateway's
// widget_host.js and trimmed: JSON-RPC over postMessage (MCP Apps) to Claude, window.openai for ChatGPT.
// The panel reaches the local server only through the host (tools/call to app-only tools): there is no HTTP origin.
// Text reaches the AI only as a visible chat message (ui/message); the silent model-context channel is never used.
"use strict";
const PortwardHost = (() => {
  let nextId = 1;
  let caps = {};
  let name = "the AI";
  let onResult = null;
  let delivered = false;
  const pending = new Map();

  function post(message) { window.parent.postMessage(Object.assign({ jsonrpc: "2.0" }, message), "*"); }

  // The host's look: its style variables, light or dark, and the safe area (MCP Apps hostContext; ChatGPT's
  // window.openai). Only the host's own variable families are taken, and only plain values: nothing that could load
  // from elsewhere or end the declaration.
  const HOST_VARIABLE = /^--(?:color|font|border-radius|border-width|shadow)-[a-z0-9-]+$/;
  const UNSAFE_VALUE = /url\(|image\(|@|\\|[;{}<>]/i;
  const SAFE_SIDES = ["top", "right", "bottom", "left"];
  const MAX_INSET_PX = 200;

  function applyTheme(theme) {
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  }

  function applyVariables(variables) {
    if (!variables || typeof variables !== "object") return;
    for (const [property, value] of Object.entries(variables)) {
      if (typeof value !== "string" || !HOST_VARIABLE.test(property) || UNSAFE_VALUE.test(value)) continue;
      document.documentElement.style.setProperty(property, value);
    }
  }

  function applyInsets(insets) {
    if (!insets || typeof insets !== "object") return;
    for (const side of SAFE_SIDES) {
      const px = insets[side];
      if (typeof px === "number" && px >= 0 && px <= MAX_INSET_PX) document.documentElement.style.setProperty("--safe-" + side, px + "px");
    }
  }

  // Updates carry only the fields that changed.
  function applyHostContext(context) {
    if (!context || typeof context !== "object") return;
    applyTheme(context.theme);
    applyVariables(context.styles && context.styles.variables);
    applyInsets(context.safeAreaInsets);
  }

  function applyOpenAI() {
    const openai = window.openai;
    if (!openai) return;
    applyTheme(openai.theme);
    applyInsets(openai.safeArea && openai.safeArea.insets);
  }

  const REQUEST_TIMEOUT_MS = 30000;  // a host that never answers must not leave a button stuck

  function request(method, params) {
    const id = nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { pending.delete(id); reject(new Error("no answer from " + name)); }, REQUEST_TIMEOUT_MS);
      pending.set(id, {
        resolve: (v) => { clearTimeout(timer); resolve(v); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });
      post({ id, method, params });
    });
  }

  function deliver(result) {
    if (delivered || !result || !onResult) return;
    delivered = true;
    onResult(result);
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window.parent) return;
    const message = event.data || {};
    if (message.method === "ui/notifications/tool-result") { deliver(message.params); return; }
    if (message.method === "ui/notifications/host-context-changed") { applyHostContext(message.params); return; }
    if (pending.has(message.id)) {
      const waiter = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) waiter.reject(new Error(message.error.message || "denied"));
      else waiter.resolve(message.result);
    }
  });

  // Tell the host how tall the content is, so it sizes the frame to fit (MCP Apps size-changed).
  let lastHeight = 0;
  function reportSize() {
    const height = Math.ceil(document.body.getBoundingClientRect().height);
    if (Math.abs(height - lastHeight) < 2) return;
    lastHeight = height;
    post({ method: "ui/notifications/size-changed", params: { width: document.documentElement.scrollWidth, height } });
  }
  if (typeof ResizeObserver === "function") new ResizeObserver(() => reportSize()).observe(document.body);

  function fromOpenAI() {
    const openai = window.openai;
    if (openai && openai.toolOutput) deliver({ structuredContent: openai.toolOutput, _meta: openai.toolResponseMetadata || {} });
  }

  // A tool result's words, for a call that failed (isError).
  function failureText(result) {
    const text = ((result && result.content) || []).filter((c) => c && c.type === "text").map((c) => c.text).join(" ");
    return text || "the tool didn't say why";
  }

  return {
    start(handler) {
      onResult = handler;
      request("ui/initialize", {
        protocolVersion: "2025-06-18", appInfo: { name: "portward", version: "0.1.0" }, appCapabilities: {},
      }).then((result) => {
        const info = (result && result.hostInfo) || {};
        name = /claude/i.test(info.name || "") ? "Claude" : (info.name || "the AI");
        caps = (result && result.hostCapabilities) || {};
        applyHostContext(result && result.hostContext);
        post({ method: "ui/notifications/initialized", params: {} });
        reportSize();
      }).catch(() => {});
      window.addEventListener("openai:set_globals", () => { applyOpenAI(); fromOpenAI(); });
      applyOpenAI();
      fromOpenAI();
    },
    hostName() { return window.openai ? "ChatGPT" : name; },
    // Call one of the server's tools through the host. A failed call (isError) throws with the tool's words.
    async callTool(tool, args) {
      const result = window.openai && window.openai.callTool
        ? await window.openai.callTool(tool, args)
        : await request("tools/call", { name: tool, arguments: args });
      if (result && result.isError === true) throw new Error(failureText(result));
      return result;
    },
    canPutInChat() { return Boolean(caps.message || (window.openai && window.openai.sendFollowUpMessage)); },
    // Put text in the chat as a visible message from the user. Returns "sent" when the host sends it (ChatGPT),
    // "composed" when it lands in the message box and the user presses send (Claude, per the #27 live check).
    async putInChat(text) {
      if (window.openai && window.openai.sendFollowUpMessage) {
        await window.openai.sendFollowUpMessage({ prompt: text });
        return "sent";
      }
      if (!caps.message) throw new Error(name + " can't put text in the chat from here");
      await request("ui/message", { role: "user", content: [{ type: "text", text: text }] });
      return "composed";
    },
  };
})();
