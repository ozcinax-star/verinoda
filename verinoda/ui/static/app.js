/* Verinoda notes and graph: a note per symbol, file and data file; local and global graphs.
   No libraries and no network beyond this server (Content-Security-Policy: 'self'). */
"use strict";
(() => {
  // -- small helpers -------------------------------------------------------------------------
  const $ = (sel, root = document) => root.querySelector(sel);
  function el(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") e.className = v;
      else if (k === "text") e.textContent = v;
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v === true ? "" : String(v));
    }
    for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false)
      e.append(k instanceof Node ? k : document.createTextNode(String(k)));
    return e;
  }
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (_) { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (_) { /* private mode: not remembered */ } },
  };
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  async function api(path) {
    const r = await fetch(path, { headers: { Accept: "application/json" } });
    let j = {};
    try { j = await r.json(); } catch (_) { /* not JSON */ }
    if (!r.ok) { const e = new Error(j.message || r.statusText); e.code = j.error; e.status = r.status; throw e; }
    return j;
  }
  const noteHref = (id) => "#/n/" + encodeURIComponent(id);

  // -- language ------------------------------------------------------------------------------
  const I18N = {
    en: {
      search: "Search notes, or ask a question  (Ctrl+K)", files: "Files", localGraph: "Local graph", depth: "Depth",
      tests: "Tests", dataFiles: "Data files", external: "External", outline: "Outline", graphView: "Graph view",
      highlight: "Highlight notes…", labels: "Labels", fit: "Fit", close: "Close", back: "Back", forward: "Forward",
      colorBy: "Colour by", byFolder: "folder", byCommunity: "community",
      theme: "Light / dark", toggleFiles: "Show or hide files", toggleGraph: "Show or hide the local graph",
      notes: "notes", filesN: "files", links: "links", dataNotes: "data notes", hubs: "Most connected",
      welcome: "Every symbol, file and data file of the project is a note. Search above, browse the files, or open the graph view.",
      lines: "lines", line: "line", more: "more", moreLines: "more lines in the file", noResults: "No notes match.",
      inferred: "inferred: the link is not stated in the code, the tool derived it", loading: "Loading…",
      noIndex: "This project has no index yet: run `verinoda scan .` in it, then reload.",
      shown: "shown", hidden: "hidden (least connected)", test: "test", defined: "defined at",
      sec: {
        defined_in: "Defined in", members: "Members", calls: "Calls", called_by: "Called by",
        extends: "Extends / implements", extended_by: "Subclasses / implementations", imports: "Imports",
        imported_by: "Imported by", references: "References", referenced_by: "Referenced by",
        names_data: "Names (resource ids)", named_by: "Named by", other_out: "Other links", other_in: "Other backlinks",
        claims: "Claims", outline: "Outline", hubs: "Most connected",
      },
      kind: {
        class: "class", method: "method", function: "function", file: "file", doc: "document", section: "section",
        data: "data file", symbol: "symbol", external: "external", claim: "claim",
      },
      why: { name: "name", "name starts with": "name starts with", "name contains": "in the name", path: "in the path", question: "for the question" },
    },
    tr: {
      search: "Not ara ya da soru sor  (Ctrl+K)", files: "Dosyalar", localGraph: "Yerel graf", depth: "Derinlik",
      tests: "Testler", dataFiles: "Veri dosyaları", external: "Dış", outline: "Ana hat", graphView: "Graf görünümü",
      highlight: "Notları vurgula…", labels: "Etiketler", fit: "Sığdır", close: "Kapat", back: "Geri", forward: "İleri",
      colorBy: "Renk", byFolder: "klasör", byCommunity: "topluluk",
      theme: "Açık / koyu", toggleFiles: "Dosyaları göster ya da gizle", toggleGraph: "Yerel grafı göster ya da gizle",
      notes: "not", filesN: "dosya", links: "bağlantı", dataNotes: "veri notu", hubs: "En çok bağlantılı",
      welcome: "Projenin her sembolü, dosyası ve veri dosyası bir not. Yukarıdan ara, dosyalara göz at ya da graf görünümünü aç.",
      lines: "satır", line: "satır", more: "daha", moreLines: "satır daha var", noResults: "Eşleşen not yok.",
      inferred: "çıkarım: bağlantı kodda açıkça yazmıyor, araç türetti", loading: "Yükleniyor…",
      noIndex: "Bu projenin henüz indeksi yok: içinde `verinoda scan .` çalıştırıp sayfayı yenileyin.",
      shown: "gösterilen", hidden: "gizli (en az bağlantılı)", test: "test", defined: "tanımı",
      sec: {
        defined_in: "Tanımlandığı yer", members: "Üyeler", calls: "Çağırdıkları", called_by: "Çağıranlar",
        extends: "Genişlettiği / uyguladığı", extended_by: "Alt sınıflar / uygulamalar", imports: "İçe aktardıkları",
        imported_by: "İçe aktaranlar", references: "Başvurdukları", referenced_by: "Başvuranlar",
        names_data: "Adlandırdığı kaynaklar", named_by: "Adlandıranlar", other_out: "Diğer bağlantılar",
        other_in: "Diğer geri bağlantılar", claims: "İddialar", outline: "Ana hat", hubs: "En çok bağlantılı",
      },
      kind: {
        class: "sınıf", method: "metot", function: "fonksiyon", file: "dosya", doc: "belge", section: "bölüm",
        data: "veri dosyası", symbol: "sembol", external: "dış", claim: "iddia",
      },
      why: { name: "ad", "name starts with": "ad böyle başlıyor", "name contains": "adında geçiyor", path: "yolunda geçiyor", question: "soruya göre" },
    },
  };
  let lang = store.get("vn.lang", (navigator.language || "").toLowerCase().startsWith("tr") ? "tr" : "en");
  const t = (key) => key.split(".").reduce((o, k) => (o && o[k] !== undefined ? o[k] : null), I18N[lang]) ?? key;
  function applyI18n() {
    document.documentElement.lang = lang;
    for (const e of document.querySelectorAll("[data-i18n]")) e.textContent = t(e.dataset.i18n);
    for (const e of document.querySelectorAll("[data-i18n-placeholder]")) e.placeholder = t(e.dataset.i18nPlaceholder);
    for (const e of document.querySelectorAll("[data-i18n-title]")) e.title = t(e.dataset.i18nTitle);
    $("#btn-lang").textContent = lang === "tr" ? "EN" : "TR";
  }

  // -- theme ---------------------------------------------------------------------------------
  let theme = store.get("vn.theme", window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  const colors = {};
  function applyTheme() {
    document.documentElement.dataset.theme = theme;
    const cs = getComputedStyle(document.documentElement);
    for (const k of ["bg", "text", "muted", "faint", "accent", "data", "border"]) colors[k] = cs.getPropertyValue("--" + k).trim();
    for (const g of graphs) g.dirty = true, g.kick();
  }

  // -- code highlighting (a light tokenizer: comments, strings, numbers, keywords, calls) --------
  const KEYWORDS = {
    python: "and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield self",
    java: "abstract assert boolean break byte case catch char class const continue default do double else enum extends final finally float for if implements import instanceof int interface long native new null package private protected public return short static super switch synchronized this throw throws transient try void volatile while var record true false",
    kotlin: "as break class continue do else false for fun if in interface is null object package return super this throw true try typealias val var when while by companion data enum import init internal lateinit open override private protected public sealed suspend",
    js: "async await break case catch class const continue default delete do else export extends false finally for from function if import in instanceof let new null return static super switch this throw true try typeof undefined var void while yield of",
    go: "break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var nil true false",
    rust: "as async await break const continue crate else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while",
  };
  KEYWORDS.ts = KEYWORDS.js + " interface type enum implements private public protected readonly declare namespace as";
  KEYWORDS.csharp = KEYWORDS.java + " using namespace string bool object var async await get set";
  KEYWORDS.cpp = "auto bool break case catch char class const continue default delete do double else enum explicit extern false float for friend if inline int long namespace new nullptr operator private protected public return short signed sizeof static struct switch template this throw true try typedef typename union unsigned using virtual void volatile while";
  KEYWORDS.c = KEYWORDS.cpp;
  KEYWORDS.groovy = KEYWORDS.java + " def";
  const KW_SETS = Object.fromEntries(Object.entries(KEYWORDS).map(([k, v]) => [k, new Set(v.split(" "))]));
  const HASH_COMMENT = new Set(["python", "yaml", "toml", "shell", "mcfunction", "ruby"]);
  function highlightLines(lines, langName) {
    const kw = KW_SETS[langName] || null;
    const hash = HASH_COMMENT.has(langName);
    const slash = !hash && langName !== "json" && langName !== "markdown" && langName !== "text";
    let inBlock = false;
    return lines.map((line) => {
      let out = "", i = 0;
      const n = line.length;
      while (i < n) {
        if (inBlock) {
          const end = line.indexOf("*/", i);
          const stop = end < 0 ? n : end + 2;
          out += `<span class="tok-com">${esc(line.slice(i, stop))}</span>`;
          i = stop; inBlock = end < 0; continue;
        }
        const c = line[i], rest = line.slice(i);
        if ((hash && c === "#") || (slash && rest.startsWith("//")) || (langName === "sql" && rest.startsWith("--"))) {
          out += `<span class="tok-com">${esc(rest)}</span>`; break;
        }
        if (slash && rest.startsWith("/*")) { inBlock = true; continue; }
        if (c === '"' || c === "'" || c === "`") {
          let j = i + 1;
          while (j < n && line[j] !== c) j += line[j] === "\\" ? 2 : 1;
          out += `<span class="tok-str">${esc(line.slice(i, j + 1))}</span>`; i = j + 1; continue;
        }
        if (c === "@" && /[A-Za-z]/.test(line[i + 1] || "")) {
          const m = /^@[\w.]+/.exec(rest); out += `<span class="tok-ann">${esc(m[0])}</span>`; i += m[0].length; continue;
        }
        if (/[0-9]/.test(c) && !/[\w]/.test(line[i - 1] || "")) {
          const m = /^[0-9][0-9a-fA-FxX_.]*[lLfFdD]?/.exec(rest); out += `<span class="tok-num">${esc(m[0])}</span>`; i += m[0].length; continue;
        }
        if (/[A-Za-z_$]/.test(c)) {
          const m = /^[A-Za-z_$][\w$]*/.exec(rest), w = m[0];
          if (kw && kw.has(w)) out += `<span class="tok-kw">${esc(w)}</span>`;
          else if (line[i + w.length] === "(") out += `<span class="tok-fn">${esc(w)}</span>`;
          else out += esc(w);
          i += w.length; continue;
        }
        out += esc(c); i++;
      }
      return out;
    });
  }

  // -- note rendering --------------------------------------------------------------------------
  const KIND_LETTER = { class: "C", method: "M", function: "F", file: "F", doc: "D", section: "§", data: "{}", symbol: "S", external: "E", claim: "!" };
  const kindBadge = (kind) => el("span", { class: "kbadge k-" + (kind || "symbol"), title: t("kind." + kind), text: KIND_LETTER[kind] || "·" });
  function noteLink(item, cls) {
    return el("a", { href: noteHref(item.id), class: cls || "", title: [item.file, item.line ? `:${item.line}` : ""].join("") }, item.title);
  }
  let current = null;
  const main = $("#note");

  function setMain(...kids) { main.replaceChildren(...kids); $("#main").scrollTop = 0; }

  async function openNote(id) {
    current = id;
    setMain(el("div", { class: "empty", text: t("loading") }));
    let n;
    try { n = await api("/api/note?id=" + encodeURIComponent(id)); }
    catch (e) {
      if (current === id) {
        showError(e);
        $("#outline").replaceChildren(); localSeq++; local.setData([], []); markTree(null);
      }
      return;
    }
    if (current !== id) return;
    renderNote(n);
    loadLocal(id);
    markTree(n.file);
    document.title = `${n.title} · Verinoda`;
  }

  function showError(e) {
    setMain(el("div", { class: "empty error", text: e.code === "no_index" ? t("noIndex") : `${e.status || ""} ${e.message}` }));
  }

  function renderNote(n) {
    const parts = [];
    const crumbs = el("div", { class: "crumbs" });
    (n.breadcrumb || []).forEach((c, i) => { if (i) crumbs.append(" / "); crumbs.append(noteLink(c)); });
    if (!(n.breadcrumb || []).length && n.file) crumbs.append(n.file);
    parts.push(crumbs);
    parts.push(el("h1", {}, kindBadge(n.kind), " ", n.title));
    const meta = el("div", { class: "meta" }, el("span", { class: "chip kind", text: t("kind." + n.kind) }));
    if (n.file) meta.append(el("span", { class: "chip mono", text: n.line ? `${n.file}:${n.line}` : n.file }));
    if (n.span) meta.append(el("span", { class: "chip", title: n.span_basis || "", text: `${n.span[0]}–${n.span[1]} (${n.span[1] - n.span[0] + 1} ${t("lines")})` }));
    if (n.test) meta.append(el("span", { class: "chip", text: t("test") }));
    parts.push(meta);
    if (n.signature) parts.push(el("pre", { class: "sig mono", text: n.signature }));
    if (n.doc) parts.push(el("div", { class: "doc", text: n.doc }));
    if (n.outline && n.outline.length) {
      parts.push(sectionHeader("outline", n.outline.length));
      parts.push(el("ul", { class: "links" }, n.outline.map((o) => el("li", {}, kindBadge(o.kind), noteLink(o),
        el("span", { class: "at mono", text: o.line ? `${t("line")} ${o.line}` : "" })))));
    }
    const sections = [];
    for (const s of n.sections || []) {
      sections.push(sectionHeader(s.key, s.count));
      sections.push(el("ul", { class: "links" }, s.items.map((it) => linkItem(it, s.key))));
      if (s.count > s.items.length) sections.push(el("div", { class: "muted small", text: `+${s.count - s.items.length} ${t("more")}` }));
    }
    // a class or a file is read through its members and links; a function through its code
    const linksFirst = ["class", "file", "doc"].includes(n.kind);
    const code = n.code ? [codeBlock(n.code, n.file)] : [];
    parts.push(...(linksFirst ? [...sections, ...code] : [...code, ...sections]));
    setMain(...parts);
    renderOutline(n);
  }

  function sectionHeader(key, count) {
    return el("h2", { class: "sec", id: "sec-" + key }, t("sec." + key), el("span", { class: "n", text: String(count) }));
  }

  function linkItem(it, key) {
    if (key === "claims") {
      return el("li", {}, el("span", { class: "status st-" + it.status, text: it.status.replace(/_/g, " ") }),
        el("span", { text: it.title }));
    }
    const inferred = it.confidence && it.confidence !== "EXTRACTED";
    const kids = [kindBadge(it.kind), noteLink(it, inferred ? "inferred" : "")];
    if (inferred) kids.push(el("span", { class: "q", title: t("inferred") + (it.context ? ` (${it.context})` : ""), text: "?" }));
    if (key === "names_data" || key === "named_by") {
      // the id that links them, and where: the file named (names) or the line naming this note (named by)
      const rid = (it.relation || "").replace(/^names /, "");
      if (rid && rid !== it.title) kids.push(el("span", { class: "rel mono", text: rid }));
      const where = key === "names_data" ? it.file : it.at;
      if (where) kids.push(el("span", { class: "at mono", text: where }));
      return el("li", {}, kids);
    }
    const rel = it.relation && !["calls", "method", "contains", "imports", "references"].includes(it.relation) ? it.relation : "";
    if (rel) kids.push(el("span", { class: "rel", text: rel }));
    if (it.at) kids.push(el("span", { class: "at mono", text: it.at }));
    return el("li", {}, kids);
  }

  function codeBlock(code, file) {
    const hl = highlightLines(code.lines, code.lang);
    const tbody = el("tbody");
    hl.forEach((html, i) => {
      const src = el("td", { class: "src" });
      src.innerHTML = html || " "; // tokens were escaped by highlightLines
      tbody.append(el("tr", {}, el("td", { class: "ln", text: String(code.start + i) }), src));
    });
    const box = el("div", { class: "code mono" }, el("table", {}, tbody));
    const shown = code.end - code.start + 1;
    if (code.total > shown) box.append(el("div", { class: "more", text: `${file}: +${code.total - shown} ${t("moreLines")}` }));
    return box;
  }

  function renderOutline(n) {
    const box = $("#outline");
    box.replaceChildren();
    for (const s of n.sections || []) {
      box.append(el("a", { href: "#", onclick: (ev) => { ev.preventDefault(); const h = document.getElementById("sec-" + s.key); if (h) h.scrollIntoView({ behavior: "smooth", block: "start" }); } },
        `${t("sec." + s.key)} (${s.count})`));
    }
  }

  async function renderHome() {
    current = null;
    document.title = "Verinoda";
    let s;
    try { s = await api("/api/stats"); } catch (e) { showError(e); return; }
    const cards = el("div", { class: "cards" },
      [[s.notes, t("notes")], [s.files, t("filesN")], [s.links, t("links")], [s.data_notes, t("dataNotes")]]
        .map(([v, l]) => el("div", { class: "card" }, el("div", { class: "v", text: Number(v).toLocaleString() }), el("div", { class: "l", text: l }))));
    const hubs = el("ul", { class: "links" }, (s.hubs || []).map((h) => el("li", {}, kindBadge(h.kind), noteLink(h),
      el("span", { class: "at mono", text: `${h.degree} · ${h.file || ""}` }))));
    setMain(el("div", { class: "home" }, el("h1", { text: s.project }), el("p", { class: "muted", text: t("welcome") }),
      cards, sectionHeader("hubs", (s.hubs || []).length), hubs));
    $("#outline").replaceChildren();
    local.setData([], []);
  }

  // -- file tree -------------------------------------------------------------------------------
  let treeData = null;
  const openFolders = new Set();
  async function loadTree() {
    try { treeData = await api("/api/tree"); } catch (_) { return; }
    const box = $("#tree");
    box.replaceChildren(...treeData.children.map((c) => treeNode(c, "")));
  }
  function treeNode(node, prefix) {
    const path = prefix ? `${prefix}/${node.name}` : node.name;
    if (!node.children) {
      return el("a", { class: "tnode tfile", href: noteHref(node.id), "data-path": node.path, title: node.path }, kindBadge(node.kind), " ", node.name);
    }
    const kids = el("div", { class: "tchildren", hidden: !openFolders.has(path) });
    const head = el("div", { class: "tnode tfolder" + (openFolders.has(path) ? " open" : ""), "data-folder": path, role: "treeitem" }, node.name);
    const fill = () => { if (!kids.childElementCount) kids.append(...node.children.map((c) => treeNode(c, path))); };
    if (openFolders.has(path)) fill();
    head.addEventListener("click", () => {
      const open = kids.hidden;
      kids.hidden = !open; head.classList.toggle("open", open);
      if (open) { openFolders.add(path); fill(); } else openFolders.delete(path);
    });
    return el("div", {}, head, kids);
  }
  function markTree(file) {
    for (const a of document.querySelectorAll(".tnode.active")) a.classList.remove("active");
    if (!file || !treeData) return;
    const parts = file.split("/");
    let prefix = "";
    for (const p of parts.slice(0, -1)) {
      prefix = prefix ? `${prefix}/${p}` : p;
      const head = document.querySelector(`[data-folder="${CSS.escape(prefix)}"]`);
      if (head && head.nextSibling.hidden) head.click();
    }
    const a = document.querySelector(`.tfile[data-path="${CSS.escape(file)}"]`);
    if (a) { a.classList.add("active"); a.scrollIntoView({ block: "nearest" }); }
  }

  // -- search ----------------------------------------------------------------------------------
  const input = $("#search"), results = $("#results");
  let searchSeq = 0, shownSeq = 0, active = -1, items = [];
  let timer = null;
  function cancelSearch() { clearTimeout(timer); timer = null; searchSeq++; }
  input.addEventListener("input", () => {
    clearTimeout(timer); items = []; active = -1;
    timer = setTimeout(() => { timer = null; runSearch(); }, 140);
  });
  input.addEventListener("keydown", async (ev) => {
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      if (!items.length) return;
      active = (active + (ev.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      paintActive();
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      if (timer !== null || shownSeq !== searchSeq) { // the results on screen are for another query
        clearTimeout(timer); timer = null;
        await runSearch();
      }
      if (active < 0 && items.length) active = 0;
      if (items[active]) go(items[active].id);
    } else if (ev.key === "Escape") { cancelSearch(); results.hidden = true; input.blur(); }
  });
  input.addEventListener("focus", () => { if (items.length) results.hidden = false; });
  document.addEventListener("click", (ev) => { if (!$("#searchbox").contains(ev.target)) results.hidden = true; });
  async function runSearch() {
    const q = input.value.trim();
    const seq = ++searchSeq;
    if (!q) { results.hidden = true; items = []; return; }
    let r;
    try { r = await api("/api/search?q=" + encodeURIComponent(q)); } catch (_) { if (seq === searchSeq) items = []; return; }
    if (seq !== searchSeq) return;
    shownSeq = seq;
    items = r.results || [];
    active = items.length ? 0 : -1;
    results.replaceChildren(...(items.length ? items.map((it, i) => el("div", {
      class: "result", role: "option", onclick: () => go(it.id), onmousemove: () => { active = i; paintActive(); },
    }, kindBadge(it.kind), el("span", { class: "t", text: it.title }), el("span", { class: "p", text: it.file || "" }),
    el("span", { class: "w", text: (I18N[lang].why || {})[it.why] || it.why || "" }))) : [el("div", { class: "result muted", text: t("noResults") })]));
    results.hidden = false;
    paintActive();
  }
  function paintActive() {
    [...results.children].forEach((c, i) => c.classList.toggle("active", i === active));
    const a = results.children[active]; if (a) a.scrollIntoView({ block: "nearest" });
  }
  function go(id) { cancelSearch(); results.hidden = true; input.blur(); location.hash = noteHref(id); }

  // -- force-directed graph on a canvas ---------------------------------------------------------
  const PALETTE = ["#8e7cf5", "#4fa3d9", "#e0a34a", "#5cc08a", "#e0605a", "#c77dd6", "#56c2c9", "#d9d05a",
    "#8fa3ff", "#f08aa6", "#7fbf5a", "#d98c4f", "#6fd3a0", "#b88ae6", "#e6c36a", "#5aa0c8"];
  const REL_COLOR = { calls: "#8e7cf5", imports: "#5a7fa8", imports_from: "#5a7fa8", references: "#808088", uses: "#808088",
    method: "#66666e", contains: "#66666e", inherits: "#e0a34a", implements: "#e0a34a", names: "#3fb8a8" };
  const areaColor = new Map(); // "@folder" -> colour, the largest part of the tree first
  const groupColor = (g) => g === "data" ? (colors.data || "#3fb8a8") : g === "external" ? "#6a6a70"
    : typeof g === "string" && g.startsWith("@") ? areaColor.get(g) || "#8a8f98"
    : g === "other" || g === null || g === undefined ? "#8a8f98" : PALETTE[Math.abs(Number(g)) % PALETTE.length];

  function quadChild(q, n) {
    const h = q.s / 2, i = (n.x >= q.x0 + h ? 1 : 0) + (n.y >= q.y0 + h ? 2 : 0);
    return q.kids[i] || (q.kids[i] = { x0: q.x0 + (i & 1) * h, y0: q.y0 + (i >> 1) * h, s: h, m: 0, cx: 0, cy: 0, n: null, kids: null, more: null });
  }
  function quadInsert(q, n, depth) {
    q.cx = (q.cx * q.m + n.x) / (q.m + 1); q.cy = (q.cy * q.m + n.y) / (q.m + 1); q.m++;
    if (q.kids === null) {
      if (q.m === 1) { q.n = n; return; }
      if (depth > 28) { (q.more || (q.more = [])).push(n); return; } // (nearly) the same point
      const old = q.n; q.n = null; q.kids = [null, null, null, null];
      if (old) quadInsert(quadChild(q, old), old, depth + 1);
    }
    quadInsert(quadChild(q, n), n, depth + 1);
  }
  function buildQuad(ns) {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of ns) { if (n.x < x0) x0 = n.x; if (n.y < y0) y0 = n.y; if (n.x > x1) x1 = n.x; if (n.y > y1) y1 = n.y; }
    const root = { x0, y0, s: Math.max(x1 - x0, y1 - y0) + 1, m: 0, cx: 0, cy: 0, n: null, kids: null, more: null };
    for (const n of ns) quadInsert(root, n, 0);
    return root;
  }
  function charge(n, q, strength, theta2) {
    if (q.m === 0) return;
    let dx = q.cx - n.x, dy = q.cy - n.y, d2 = dx * dx + dy * dy;
    if (q.kids === null || (q.s * q.s) / theta2 < d2) {
      let m = q.m;
      if (q.kids === null && (q.n === n || (q.more && q.more.includes(n)))) m -= 1; // not itself
      if (m <= 0) return;
      if (d2 < 1e-4) { // (nearly) the same point: a small push that is never zero
        dx = ((n.index * 7919) % 13 - 6.5) * 0.01; dy = ((n.index * 104729) % 11 - 5.5) * 0.01; d2 = dx * dx + dy * dy;
      }
      if (d2 < 30) d2 = Math.sqrt(30 * d2); // soften very close pairs
      const k = strength * m / d2;
      n.vx += dx * k; n.vy += dy * k;
      return;
    }
    for (const c of q.kids) if (c) charge(n, c, strength, theta2);
  }

  const graphs = [];
  class ForceGraph {
    constructor(canvas, opts) {
      this.c = canvas; this.ctx = canvas.getContext("2d");
      this.o = Object.assign({ charge: -260, distance: 55, gravity: 0.035, labels: true, arrows: false, onOpen: null, onClick: null }, opts);
      this.nodes = []; this.links = []; this.byId = new Map(); this.adj = new Map();
      this.tf = { x: 0, y: 0, k: 1 }; this.alpha = 0; this.hover = null; this.center = null;
      this.hidden = new Set(); this.filter = ""; this.dirty = true; this.running = false; this.drag = null; this.pan = null;
      this.w = 10; this.h = 10; this.dpr = 1;
      this.bind();
      new ResizeObserver(() => this.resize()).observe(canvas.parentElement);
      graphs.push(this);
    }
    resize() {
      const r = this.c.parentElement.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
      if (r.width < 2 || r.height < 2) return;
      this.w = r.width; this.h = r.height; this.dpr = dpr;
      this.c.width = Math.round(r.width * dpr); this.c.height = Math.round(r.height * dpr);
      if (this.autoFit) this.fit(); // until the user pans or zooms, the graph follows the pane's size
      this.dirty = true; this.kick();
    }
    setData(nodes, links, opts) {
      opts = opts || {};
      const old = this.byId;
      this.nodes = nodes.map((n, i) => {
        const a = i * 2.39996, r = 12 * Math.sqrt(i + 1);
        let o = old.get(n.id);
        // a kept position must be a real one, and not the spot the new centre is pinned to
        if (o && (!isFinite(o.x) || !isFinite(o.y) || (o.x === 0 && o.y === 0 && n.id !== opts.center))) o = null;
        return Object.assign({}, n, { index: i, x: o ? o.x : Math.cos(a) * r, y: o ? o.y : Math.sin(a) * r, vx: 0, vy: 0, fx: null, fy: null });
      });
      this.byId = new Map(this.nodes.map((n) => [n.id, n]));
      this.links = links.map((l) => Object.assign({}, l, { s: this.byId.get(l.source), t: this.byId.get(l.target) })).filter((l) => l.s && l.t && l.s !== l.t);
      this.adj = new Map(this.nodes.map((n) => [n.id, new Set()]));
      const deg = new Map();
      for (const l of this.links) {
        this.adj.get(l.s.id).add(l.t.id); this.adj.get(l.t.id).add(l.s.id);
        deg.set(l.s, (deg.get(l.s) || 0) + 1); deg.set(l.t, (deg.get(l.t) || 0) + 1);
      }
      for (const n of this.nodes) {
        n.deg = deg.get(n) || 0;
        const size = n.size !== undefined ? n.size : n.deg;
        n.r = 3.2 + Math.min(14, Math.sqrt(Math.max(size, n.deg)) * 1.5);
      }
      const unlinked = this.nodes.filter((n) => !n.deg).length;
      this.orphans = unlinked > 0 && unlinked < this.nodes.length; // a ring needs linked notes to go round
      this.center = opts.center ? this.byId.get(opts.center) || null : null;
      if (this.center) { this.center.x = this.center.y = 0; this.center.fx = 0; this.center.fy = 0; this.center.r += 3; }
      this.hover = null;
      this.alpha = 1;
      // a small graph settles before it is shown; a large one settles on screen, frame by frame
      if (this.nodes.length < 150) for (let i = 0; i < 160; i++) this.tick();
      this.autoFit = true;
      this.fit();
      this.dirty = true; this.kick();
    }
    visible(n) { return !this.hidden.has(String(n.group)); }
    tick() {
      const ns = this.nodes, a = this.alpha, o = this.o;
      if (!ns.length) { this.alpha = 0; return; }
      const q = buildQuad(ns);
      for (const n of ns) charge(n, q, o.charge * a, 0.81);
      for (const l of this.links) {
        const s = l.s, t = l.t;
        let dx = t.x + t.vx - s.x - s.vx, dy = t.y + t.vy - s.y - s.vy;
        const d = Math.sqrt(dx * dx + dy * dy) || 1e-6;
        const strength = 1 / Math.max(1, Math.min(s.deg, t.deg));
        const k = ((d - o.distance) / d) * a * strength;
        dx *= k; dy *= k;
        const bias = s.deg / (s.deg + t.deg || 1);
        t.vx -= dx * bias; t.vy -= dy * bias; s.vx += dx * (1 - bias); s.vy += dy * (1 - bias);
      }
      // unlinked notes ring the linked ones (as in Obsidian) instead of piling up among them
      let ring = 0;
      if (this.orphans) {
        const d2 = [];
        for (const n of ns) if (n.deg) d2.push(n.x * n.x + n.y * n.y);
        d2.sort((x, y) => x - y);
        ring = Math.sqrt(d2[Math.floor(d2.length * 0.8)]) * 1.1 + 40;
      }
      for (const n of ns) {
        if (!n.deg && ring) { // a firm spring: the whole graph's charge pushes them outwards
          const d = Math.sqrt(n.x * n.x + n.y * n.y) || 1e-6, k = ((d - ring) / d) * 0.5 * a;
          n.vx -= n.x * k; n.vy -= n.y * k;
        } else {
          const gr = n.deg ? o.gravity : o.gravity * 6; // an unlinked note would otherwise drift away
          n.vx -= n.x * gr * a; n.vy -= n.y * gr * a;
        }
        if (n.fx !== null) { n.x = n.fx; n.y = n.fy; n.vx = n.vy = 0; continue; }
        n.vx *= 0.6; n.vy *= 0.6; n.x += n.vx; n.y += n.vy;
        if (!isFinite(n.x) || !isFinite(n.y)) { // never let one bad value spread through the quadtree
          const a = n.index * 2.39996, r = 12 * Math.sqrt(n.index + 1);
          n.x = Math.cos(a) * r; n.y = Math.sin(a) * r; n.vx = n.vy = 0;
        }
      }
      this.alpha += (this.drag ? 0.3 - this.alpha : -this.alpha) * 0.03;
    }
    kick() {
      if (this.running) return;
      this.running = true;
      requestAnimationFrame(() => this.frame());
    }
    frame() {
      const moving = this.alpha > 0.005 || this.drag;
      if (moving) {
        const t0 = performance.now();
        let n = 0;
        do { this.tick(); n++; } while (!this.drag && this.alpha > 0.005 && n < 12 && performance.now() - t0 < 10);
        if (this.autoFit && !this.drag) this.fit();
        this.dirty = true;
      }
      if (this.dirty) { this.draw(); this.dirty = false; }
      if (moving) requestAnimationFrame(() => this.frame()); else this.running = false;
    }
    fit() {
      if (this.w < 60 || this.h < 60) return; // not laid out yet: resize() fits once it is
      const ns = this.nodes.filter((n) => this.visible(n));
      if (!ns.length) { this.tf = { x: 0, y: 0, k: 1 }; return; }
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const n of ns) { x0 = Math.min(x0, n.x - n.r); y0 = Math.min(y0, n.y - n.r); x1 = Math.max(x1, n.x + n.r); y1 = Math.max(y1, n.y + n.r); }
      // room for the labels under the nodes and beside the outermost ones
      const k = Math.min(2.2, (this.w - 170) / Math.max(1, x1 - x0), (this.h - 60) / Math.max(1, y1 - y0));
      this.tf = { k: Math.max(0.02, k), x: -((x0 + x1) / 2) * k, y: -((y0 + y1) / 2) * k };
      this.dirty = true; this.kick();
    }
    toGraph(sx, sy) { return [(sx - this.w / 2 - this.tf.x) / this.tf.k, (sy - this.h / 2 - this.tf.y) / this.tf.k]; }
    hit(sx, sy) {
      const [gx, gy] = this.toGraph(sx, sy);
      let best = null, bd = Infinity;
      for (const n of this.nodes) {
        if (!this.visible(n)) continue;
        const dx = n.x - gx, dy = n.y - gy, d = dx * dx + dy * dy, rr = n.r + 4 / this.tf.k;
        if (d <= rr * rr && d < bd) { best = n; bd = d; }
      }
      return best;
    }
    matches(n) { return this.filter && (n.title || "").toLowerCase().includes(this.filter); }
    draw() {
      const ctx = this.ctx, k = this.tf.k;
      ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      ctx.clearRect(0, 0, this.w, this.h);
      ctx.translate(this.w / 2 + this.tf.x, this.h / 2 + this.tf.y);
      ctx.scale(k, k);
      const focus = this.hover, nb = focus ? this.adj.get(focus.id) : null;
      const dim = !!focus || !!this.filter;
      for (const l of this.links) {
        if (!this.visible(l.s) || !this.visible(l.t)) continue;
        const on = focus ? (l.s === focus || l.t === focus) : this.filter ? (this.matches(l.s) || this.matches(l.t)) : true;
        ctx.globalAlpha = dim ? (on ? 0.85 : 0.05) : 0.4;
        ctx.strokeStyle = REL_COLOR[l.relation] || "#777";
        ctx.lineWidth = (l.weight ? Math.min(4, 0.7 + Math.log2(1 + l.weight) * 0.6) : 1) / Math.max(0.35, k);
        ctx.setLineDash(l.confidence && l.confidence !== "EXTRACTED" ? [4 / k, 3 / k] : []);
        ctx.beginPath(); ctx.moveTo(l.s.x, l.s.y); ctx.lineTo(l.t.x, l.t.y); ctx.stroke();
        if (this.o.arrows && k > 0.6 && on) this.arrow(l);
      }
      ctx.setLineDash([]);
      for (const n of this.nodes) {
        if (!this.visible(n)) continue;
        const on = focus ? (n === focus || nb.has(n.id)) : this.filter ? this.matches(n) : true;
        ctx.globalAlpha = dim ? (on ? 1 : 0.15) : 1;
        ctx.fillStyle = groupColor(n.group);
        ctx.beginPath(); ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2); ctx.fill();
        if (n === this.center || n === focus || (this.filter && this.matches(n))) {
          ctx.lineWidth = 2.2 / k; ctx.strokeStyle = colors.accent || "#8e7cf5"; ctx.stroke();
        }
      }
      if (!this.o.labels && !focus) { ctx.globalAlpha = 1; return; }
      ctx.textAlign = "center"; ctx.textBaseline = "top";
      ctx.font = `${12 / k}px system-ui, -apple-system, "Segoe UI", sans-serif`;
      for (const n of this.nodes) {
        if (!this.visible(n)) continue;
        const near = n === focus || (nb && nb.has(n.id)) || n === this.center || this.matches(n);
        if (!near && !(this.o.labels && (this.nodes.length <= 40 || k * n.r > 7.5))) continue;
        if (dim && !near) continue;
        ctx.globalAlpha = near || !dim ? 1 : 0.3;
        ctx.fillStyle = n === focus ? (colors.accent || "#8e7cf5") : (colors.text || "#ddd");
        const label = n.title.length > 42 ? n.title.slice(0, 40) + "…" : n.title;
        ctx.fillText(label, n.x, n.y + n.r + 2 / k);
      }
      ctx.globalAlpha = 1;
    }
    arrow(l) {
      const ctx = this.ctx, k = this.tf.k, dx = l.t.x - l.s.x, dy = l.t.y - l.s.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
      const ux = dx / d, uy = dy / d, x = l.t.x - ux * (l.t.r + 1), y = l.t.y - uy * (l.t.r + 1), s = 6 / k;
      ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x - ux * s - uy * s * 0.5, y - uy * s + ux * s * 0.5);
      ctx.lineTo(x - ux * s + uy * s * 0.5, y - uy * s - ux * s * 0.5); ctx.closePath();
      ctx.fillStyle = ctx.strokeStyle; ctx.fill();
    }
    bind() {
      const c = this.c, pos = (ev) => { const r = c.getBoundingClientRect(); return [ev.clientX - r.left, ev.clientY - r.top]; };
      const tip = $("#tooltip");
      c.addEventListener("wheel", (ev) => {
        ev.preventDefault();
        const [sx, sy] = pos(ev), [gx, gy] = this.toGraph(sx, sy);
        this.autoFit = false;
        const k = Math.min(8, Math.max(0.02, this.tf.k * Math.exp(-ev.deltaY * 0.0015)));
        this.tf = { k, x: sx - this.w / 2 - gx * k, y: sy - this.h / 2 - gy * k };
        this.dirty = true; this.kick();
      }, { passive: false });
      const release = () => {
        if (this.drag && this.drag.n !== this.center) { this.drag.n.fx = null; this.drag.n.fy = null; }
        this.drag = null; this.pan = null;
      };
      c.addEventListener("contextmenu", release);
      window.addEventListener("blur", release);
      c.addEventListener("mousedown", (ev) => {
        if (ev.button !== 0) return;
        const [sx, sy] = pos(ev), n = this.hit(sx, sy);
        if (n) { this.drag = { n, sx, sy, moved: false }; if (n !== this.center) { n.fx = n.x; n.fy = n.y; } }
        else this.pan = { sx, sy, x: this.tf.x, y: this.tf.y, moved: false };
      });
      window.addEventListener("mousemove", (ev) => {
        const [sx, sy] = pos(ev), held = this.drag || this.pan;
        // the button came up outside the page: a move with no button down, away from where it went down
        // (a browser also sends moves with no button while it is held and the pointer is still)
        if (held && ev.buttons === 0 && Math.abs(sx - held.sx) + Math.abs(sy - held.sy) > 3) release();
        if (this.drag) {
          if (Math.abs(sx - this.drag.sx) + Math.abs(sy - this.drag.sy) > 3) this.drag.moved = true;
          if (this.drag.moved && this.drag.n !== this.center) {
            const [gx, gy] = this.toGraph(sx, sy); this.drag.n.fx = gx; this.drag.n.fy = gy; this.alpha = Math.max(this.alpha, 0.2); this.kick();
          }
          return;
        }
        if (this.pan) {
          if (Math.abs(sx - this.pan.sx) + Math.abs(sy - this.pan.sy) > 3) { this.pan.moved = true; this.autoFit = false; }
          this.tf.x = this.pan.x + sx - this.pan.sx; this.tf.y = this.pan.y + sy - this.pan.sy;
          this.dirty = true; this.kick(); return;
        }
        if (ev.target !== c) return;
        const n = this.hit(sx, sy);
        if (n !== this.hover) { this.hover = n; this.dirty = true; this.kick(); }
        c.style.cursor = n ? "pointer" : "grab";
        if (n) {
          tip.hidden = false; tip.textContent = n.file ? `${n.title} — ${n.file}` : n.title;
          tip.style.left = `${ev.clientX + 14}px`; tip.style.top = `${ev.clientY + 12}px`;
        } else tip.hidden = true;
      });
      window.addEventListener("mouseup", () => {
        if (this.drag) {
          const n = this.drag.n;
          if (!this.drag.moved && this.o.onClick) this.o.onClick(n);
          if (n !== this.center) { n.fx = null; n.fy = null; }
          this.drag = null;
        }
        this.pan = null;
      });
      c.addEventListener("mouseleave", () => { if (this.hover) { this.hover = null; this.dirty = true; this.kick(); } tip.hidden = true; });
      c.addEventListener("dblclick", (ev) => { const [sx, sy] = pos(ev), n = this.hit(sx, sy); if (n && this.o.onOpen) this.o.onOpen(n); });
    }
  }

  // -- local graph -----------------------------------------------------------------------------
  const local = new ForceGraph($("#local"), { charge: -180, distance: 48, arrows: true, onClick: (n) => { location.hash = noteHref(n.id); } });
  let localSeq = 0;
  async function loadLocal(id) {
    if ($("#app").classList.contains("no-right")) return;
    const seq = ++localSeq;
    const qs = new URLSearchParams({ id, depth: $("#local-depth").value, tests: $("#local-tests").checked ? "1" : "0",
      data: $("#local-data").checked ? "1" : "0", external: $("#local-external").checked ? "1" : "0" });
    let g;
    try { g = await api("/api/local?" + qs); } catch (_) { return; }
    if (seq !== localSeq) return;
    local.setData(g.nodes, g.edges, { center: g.center });
  }
  for (const id of ["local-depth", "local-tests", "local-data", "local-external"])
    $("#" + id).addEventListener("change", () => current && loadLocal(current));

  // -- global graph ----------------------------------------------------------------------------
  const globalG = new ForceGraph($("#global"), { charge: -120, distance: 40, gravity: 0.05,
    onClick: (n) => { location.hash = noteHref(n.id); } });
  let globalKey = null, globalData = null, globalSeq = 0;
  async function showGraph() {
    $("#graphview").hidden = false;
    globalG.resize();
    const key = `${$("#global-tests").checked}|${$("#global-data").checked}`;
    if (key === globalKey) { globalG.fit(); return; }
    const seq = ++globalSeq;
    $("#graph-info").textContent = t("loading");
    let g;
    try { g = await api(`/api/global?tests=${$("#global-tests").checked ? 1 : 0}&data=${$("#global-data").checked ? 1 : 0}`); }
    catch (e) { if (seq === globalSeq) $("#graph-info").textContent = e.message; return; }
    if (seq !== globalSeq) return; // a newer request (the boxes changed again) is on its way
    globalKey = key; globalData = g;
    applyColors(g.nodes);
    globalG.setData(g.nodes, g.edges);
    $("#graph-info").textContent = `${g.nodes.length} ${t("filesN")}, ${g.edges.length} ${t("links")}` +
      (g.hidden_files ? ` · ${g.hidden_files} ${t("hidden")}` : "");
    renderLegend(g);
  }
  // colour the files by the part of the tree they are in (default) or by the index's communities
  let colorBy = store.get("vn.colorBy", "folder") === "community" ? "community" : "folder";
  $("#graph-color").value = colorBy;
  function applyColors(nodes) {
    const count = new Map();
    for (const n of nodes) {
      if (n.community === undefined) n.community = n.group;
      n.group = n.kind === "data" ? "data" : colorBy === "folder" ? "@" + n.area : n.community;
      if (String(n.group).startsWith("@")) count.set(n.group, (count.get(n.group) || 0) + 1);
    }
    areaColor.clear();
    [...count.entries()].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1))
      .forEach(([k], i) => areaColor.set(k, PALETTE[i % PALETTE.length]));
  }
  $("#graph-color").addEventListener("change", (ev) => {
    colorBy = ev.target.value === "community" ? "community" : "folder";
    store.set("vn.colorBy", colorBy);
    if (!globalData) return;
    applyColors(globalG.nodes);
    globalG.hidden.clear(); globalG.dirty = true; globalG.kick();
    renderLegend({ nodes: globalG.nodes, groups: globalData.groups });
  });
  function renderLegend(g) {
    const used = new Map();
    for (const n of g.nodes) used.set(String(n.group), (used.get(String(n.group)) || 0) + 1);
    const names = new Map((g.groups || []).map((x) => [String(x.id), x.name]));
    const order = [...used.entries()].sort((a, b) => b[1] - a[1]).slice(0, 24);
    $("#legend").replaceChildren(...order.map(([grp, count]) => {
      const sw = el("span", { class: "sw" });
      sw.style.background = groupColor(/^-?\d+$/.test(grp) ? Number(grp) : grp);
      const it = el("div", { class: "it" + (globalG.hidden.has(grp) ? " off" : "") }, sw,
        el("span", { text: `${grp === "data" ? t("dataFiles") : grp.startsWith("@") ? grp.slice(1) : names.get(grp) || grp} (${count})` }));
      it.addEventListener("click", () => {
        if (globalG.hidden.has(grp)) globalG.hidden.delete(grp); else globalG.hidden.add(grp);
        it.classList.toggle("off"); globalG.dirty = true; globalG.kick();
      });
      return it;
    }));
  }
  $("#graph-filter").addEventListener("input", (ev) => { globalG.filter = ev.target.value.trim().toLowerCase(); globalG.dirty = true; globalG.kick(); });
  $("#global-labels").addEventListener("change", (ev) => { globalG.o.labels = ev.target.checked; globalG.dirty = true; globalG.kick(); });
  for (const id of ["global-tests", "global-data"]) $("#" + id).addEventListener("change", () => showGraph());
  $("#graph-fit").addEventListener("click", () => { globalG.autoFit = true; globalG.fit(); });
  let beforeGraph = "#/";
  $("#graph-close").addEventListener("click", () => { location.hash = beforeGraph; });
  $("#btn-graph").addEventListener("click", () => { location.hash = "#/graph"; });

  // -- routing and chrome ----------------------------------------------------------------------
  function route() {
    const h = location.hash || "#/";
    if (h === "#/graph") { showGraph(); return; }
    $("#graphview").hidden = true;
    $("#tooltip").hidden = true;
    beforeGraph = h;
    if (h.startsWith("#/n/")) openNote(decodeURIComponent(h.slice(4)));
    else renderHome();
  }
  window.addEventListener("hashchange", route);
  $("#btn-back").addEventListener("click", () => history.back());
  $("#btn-forward").addEventListener("click", () => history.forward());
  $("#btn-lang").addEventListener("click", () => {
    lang = lang === "tr" ? "en" : "tr"; store.set("vn.lang", lang); applyI18n();
    if (globalData) renderLegend(globalData);
    route();
  });
  $("#btn-theme").addEventListener("click", () => { theme = theme === "dark" ? "light" : "dark"; store.set("vn.theme", theme); applyTheme(); });
  const togglePane = (cls, key) => {
    const app = $("#app"); app.classList.toggle(cls); store.set(key, app.classList.contains(cls) ? "1" : "0");
    if (cls === "no-right" && !app.classList.contains(cls) && current) setTimeout(() => { local.resize(); loadLocal(current); }, 0);
  };
  $("#btn-left").addEventListener("click", () => togglePane("no-left", "vn.noLeft"));
  $("#btn-right").addEventListener("click", () => togglePane("no-right", "vn.noRight"));
  if (store.get("vn.noLeft", "0") === "1") $("#app").classList.add("no-left");
  if (store.get("vn.noRight", "0") === "1") $("#app").classList.add("no-right");
  document.addEventListener("keydown", (ev) => {
    const typing = /INPUT|SELECT|TEXTAREA/.test((ev.target && ev.target.tagName) || "");
    if ((ev.key === "k" && (ev.ctrlKey || ev.metaKey)) || (ev.key === "/" && !typing)) { ev.preventDefault(); input.focus(); input.select(); }
    else if (ev.key === "Escape" && !$("#graphview").hidden) { location.hash = beforeGraph; }
    else if (ev.key === "g" && !typing && !ev.ctrlKey && !ev.metaKey) { location.hash = "#/graph"; }
  });

  // -- start -----------------------------------------------------------------------------------
  window.__verinoda = { local, global: globalG }; // for tests and debugging
  applyI18n();
  applyTheme();
  api("/api/stats").then((s) => { $("#project").textContent = s.project; }).catch(() => {});
  loadTree();
  route();
})();
