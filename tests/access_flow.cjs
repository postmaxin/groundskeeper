// Settings → Access (web/settings.js secAccess) against stub helpers: prints, as JSON, the row names it draws with
// sign-in on and with PANEL_AUTH=off, and what the Metrics button saves. Run by tests/test_access_js.py.
"use strict";
const fs = require("fs");
const vm = require("vm");

const el = (tag, attrs, kids) => ({ tag, attrs: attrs || {}, kids: kids.flat(9).filter((k) => k != null) });
const text = (n) => (typeof n === "string" ? n : (n.kids || []).map(text).join(""));
const saved = [];
const ctx = vm.createContext({
  h: (tag, attrs, ...kids) => el(tag, attrs, kids), dot: () => null, mono: (t) => t, slink: (a, t) => t,
  when: String, toast() {}, copyToast() {}, confirm: () => true, fetch: async () => ({ ok: true, json: async () => ({}) }),
  putJSON: async (...a) => { saved.push(a); }, $: () => null, location: {}, console, setInterval() {}, setTimeout() {},
  AMBER: "amber", MUTED: "muted", CORAL: "coral", GREEN: "green", document: { addEventListener() {}, getElementById: () => null }, window: { matchMedia: () => ({ addEventListener() {} }), addEventListener() {} },
});
vm.runInContext(fs.readFileSync(process.argv[2], "utf8") + "\n;globalThis.secAccess = secAccess;", ctx);

const names = (cards) => cards.flatMap((c) => c.kids.flat(9)).filter((n) => n && n.attrs && n.attrs.class === "srow")
  .map((r) => text(r.kids[0].kids[0]));
const out = {};
for (const [label, access] of [["off", { disabled: true }], ["on", { tokens: [] }]]) {
  const cards = ctx.secAccess({ access, st: { metrics_enabled: false } });
  out[label] = names(cards);
}
const cards = ctx.secAccess({ access: { disabled: true }, st: { metrics_enabled: true } });
const btn = cards.flatMap((c) => c.kids.flat(9)).flatMap((r) => (r && r.kids ? r.kids.flat(9) : []))
  .flatMap((n) => (n && n.kids ? n.kids.flat(9) : [])).find((n) => n && n.tag === "button");
out.button = btn ? text(btn) : null;
(async () => {
  if (btn) await btn.attrs.onclick().catch(() => {});
  out.saved = saved;
  console.log(JSON.stringify(out));
})();
