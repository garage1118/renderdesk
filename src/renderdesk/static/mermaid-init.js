// data-theme is set by theme.js, which must load first (see view.py's asset
// ordering). Only html-format artifacts' injected mermaid (no theme.js
// there — sandboxed raw content, never renderdesk chrome) hit the "dark"
// default, preserving prior behavior for that path.
mermaid.initialize({
    startOnLoad: true,
    securityLevel: "strict",
    theme: document.documentElement.getAttribute("data-theme") === "light" ? "default" : "dark",
});
