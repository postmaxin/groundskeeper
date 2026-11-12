/* Help view (#help, #help/<page>): the user docs and the changelog, written in markdown on the server
   (docs/user/*.md, CHANGELOG.md) and rendered here into DOM nodes. Like the rest of the UI it never uses innerHTML:
   the renderer knows a small markdown subset (headings, paragraphs, lists, quotes, code, tables, **bold**, *em*,
   `code`, [links](…)) and anything else is shown as text. */
"use strict";

const helpState = { index: null };

/* ------------------------------------------------------------ inline */
// [text](href), **bold**, *em* / _em_, `code`. Links may only go to the panel itself or to http(s).
const INLINE = /(\[([^\]]+)\]\(([^)\s]+)\))|(\*\*([^*]+)\*\*)|(`([^`]+)`)|(\*([^*\s][^*]*)\*)|(\b_([^_]+)_\b)/g;
function safeHref(href) {
  if (/^(#|\/(?!\/))/.test(href)) return href;
  if (/^https?:\/\//i.test(href)) return href;
  return null;
}
function mdInline(text) {
  const out = [];
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1]) {
      const href = safeHref(m[3]);
      const ext = href && /^https?:/i.test(href);
      out.push(href ? h("a", { href, target: ext ? "_blank" : null, rel: ext ? "noopener" : null }, ...mdInline(m[2]))
                    : m[2]);
    } else if (m[4]) out.push(h("strong", null, ...mdInline(m[5])));
    else if (m[6]) out.push(h("code", null, m[7]));
    else if (m[8]) out.push(h("em", null, ...mdInline(m[9])));
    else if (m[10]) out.push(h("em", null, m[11]));
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

/* ------------------------------------------------------------- blocks */
const isBlank = (l) => !l.trim();
const LIST = /^(\s*)([-*]|\d+\.)\s+(.*)$/;
const listItem = (l) => l.match(LIST);
const startsBlock = (l) => /^(#{1,4}\s|```|>|\|)/.test(l) || LIST.test(l);

function mdList(lines, i, indent) {
  const ordered = /\d/.test(listItem(lines[i])[2]);
  const list = h(ordered ? "ol" : "ul", null);
  while (i < lines.length) {
    const m = listItem(lines[i]);
    if (!m || m[1].length < indent) break;
    if (m[1].length > indent) {                               // a nested list belongs to the last item
      const [sub, next] = mdList(lines, i, m[1].length);
      (list.lastElementChild || list).append(sub);
      i = next;
      continue;
    }
    let text = m[3];
    i++;
    while (i < lines.length && !isBlank(lines[i]) && !LIST.test(lines[i]) && !startsBlock(lines[i].trim())) {
      text += " " + lines[i].trim();                         // a wrapped item
      i++;
    }
    list.append(h("li", null, ...mdInline(text)));
    if (i + 1 < lines.length && isBlank(lines[i]) && LIST.test(lines[i + 1])
        && listItem(lines[i + 1])[1].length >= indent) i++;   // a loose list: one blank line between items
  }
  return [list, i];
}

function mdTable(rows) {
  const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
  const head = cells(rows[0]);
  const body = rows.slice(2).map(cells);
  return h("div", { class: "mdtable" }, h("table", null,
    h("thead", null, h("tr", null, ...head.map((c) => h("th", null, ...mdInline(c))))),
    h("tbody", null, ...body.map((r) => h("tr", null, ...r.map((c) => h("td", null, ...mdInline(c))))))));
}

function renderMarkdown(md) {
  const lines = md.replace(/\r\n?/g, "\n").replace(/<!--[\s\S]*?-->/g, "").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (isBlank(line)) { i++; continue; }
    const hm = line.match(/^(#{1,4})\s+(.*)$/);
    if (hm) {
      const level = Math.min(4, hm[1].length + 1);           // the panel's own title is the h1; # becomes h2
      const text = hm[2].trim();
      out.push(h(`h${level}`, { id: slug(text) }, ...mdInline(text)));
      i++;
      continue;
    }
    if (line.startsWith("```")) {
      const code = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("```")) code.push(lines[i++]);
      i++;
      out.push(h("pre", { class: "mdcode" }, h("code", null, code.join("\n"))));
      continue;
    }
    if (line.startsWith(">")) {
      const q = [];
      while (i < lines.length && lines[i].startsWith(">")) q.push(lines[i++].replace(/^>\s?/, ""));
      out.push(h("blockquote", null, ...renderMarkdown(q.join("\n"))));
      continue;
    }
    if (line.trim().startsWith("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1])) {
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) rows.push(lines[i++]);
      out.push(mdTable(rows));
      continue;
    }
    if (LIST.test(line)) {
      const [list, next] = mdList(lines, i, listItem(line)[1].length);
      out.push(list);
      i = next;
      continue;
    }
    const para = [line.trim()];
    i++;
    while (i < lines.length && !isBlank(lines[i]) && !startsBlock(lines[i])) para.push(lines[i++].trim());
    out.push(h("p", null, ...mdInline(para.join(" "))));
  }
  return out;
}
const slug = (t) => t.toLowerCase().replace(/[`*_[\]()]/g, "").replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

/* --------------------------------------------------------------- view */
async function helpIndex() {
  if (!helpState.index) helpState.index = await getJSON("/api/help");
  return helpState.index;
}

async function renderHelp(sub) {
  const el = $("#p-help");
  const [pageId, anchor] = String(sub || "").split("#");
  let idx;
  try { idx = await helpIndex(); } catch (e) { el.replaceChildren(h("p", { class: "muted pad" }, `Help is not available: ${e.message}`)); return; }
  const nav = h("nav", { class: "helpnav", "aria-label": "Help pages" },
    h("a", { href: "#help", "aria-current": pageId ? null : "page" }, "Contents"),
    ...idx.pages.map((p) => h("a", { href: `#help/${p.id}`, "aria-current": p.id === pageId ? "page" : null }, p.title)));
  const body = h("article", { class: "helpbody" });
  el.replaceChildren(h("div", { class: "helpwrap" }, nav, body));
  if (!pageId) {
    body.append(h("h2", null, "Help"),
      h("p", { class: "muted" }, `Groundskeeper ${versionLabel(idx)}`),
      h("ul", { class: "helplist" }, ...idx.pages.map((p) => h("li", null, h("a", { href: `#help/${p.id}` }, p.title)))));
    return;
  }
  body.append(h("p", { class: "muted" }, "Loading…"));
  try {
    const p = await getJSON(`/api/help/${encodeURIComponent(pageId)}`);
    body.replaceChildren(...renderMarkdown(p.markdown));
    document.title = `${p.title} · Help · Groundskeeper`;
    if (anchor) document.getElementById(anchor)?.scrollIntoView();
    else el.scrollTop = 0;
  } catch (e) {
    body.replaceChildren(h("p", { class: "muted" }, `No help page "${pageId}".`), h("a", { href: "#help" }, "All help pages"));
  }
}

// The ? next to the view title opens that view's page (or the contents when there is none).
const HELP_FOR = { live: "live", runs: "runs", studio: "studio", schedule: "schedule", maintenance: "maintenance",
                   health: "health", events: "events", settings: "settings" };
function syncHelpLink(view) {
  const a = $("#helplink");
  if (!a) return;
  a.hidden = view === "help";
  a.href = HELP_FOR[view] ? `#help/${HELP_FOR[view]}` : "#help";
}

// The version under the robot card, linking to the changelog.
helpIndex().then((idx) => { const a = $("#navver"); if (a) a.textContent = `v${versionLabel(idx)}`; })
  .catch(() => {});
