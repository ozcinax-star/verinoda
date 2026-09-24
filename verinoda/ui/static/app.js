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
  // an exported file (`verinoda ui --export`) carries its data and answers the API itself
  const OFFLINE = (() => {
    const e = document.getElementById("verinoda-data");
    if (!e) return null;
    try { const d = JSON.parse(e.textContent); return d && d.format === "verinoda-export" ? d : null; } catch (_) { return null; }
  })();
  async function api(path) {
    if (OFFLINE) return offlineApi(path);
    const r = await fetch(path, { headers: { Accept: "application/json" } });
    let j = {};
    try { j = await r.json(); } catch (_) { /* not JSON */ }
    if (!r.ok) { const e = new Error(j.message || r.statusText); e.code = j.error; e.status = r.status; throw e; }
    return j;
  }
  const noteHref = (id) => "#/n/" + encodeURIComponent(id);
  const TOKEN = OFFLINE ? "" : (($("meta[name=verinoda-token]") || {}).content || "");
  async function apiPost(path, body) {
    const r = await fetch(path, { method: "POST", body: JSON.stringify(body),
      headers: { "Content-Type": "application/json", "X-Verinoda-Token": TOKEN, Accept: "application/json" } });
    let j = {};
    try { j = await r.json(); } catch (_) { /* not JSON */ }
    if (!r.ok) { const e = new Error(j.message || r.statusText); e.code = j.error; e.status = r.status; throw e; }
    return j;
  }

  // -- language ------------------------------------------------------------------------------
  const I18N = {
    en: {
      search: "Search notes, or ask a question  (/)   ·   commands: Ctrl+K", files: "Files", localGraph: "Local graph", depth: "Depth",
      tests: "Tests", dataFiles: "Data files", external: "External", outline: "Outline", graphView: "Graph view",
      highlight: "Highlight notes…", labels: "Labels", fit: "Fit", close: "Close", back: "Back", forward: "Forward",
      colorBy: "Colour by", byFolder: "folder", byCommunity: "community",
      editor: "Editor", openIn: "Open in", noEditor: "no editor",
      myNote: "My note", myNotes: "My notes", addNote: "+ Note", edit: "Edit", save: "Save", cancel: "Cancel",
      del: "Delete", sure: "Delete it?", keep: "Read it: still right", noteHint: "Markdown: **bold**, `code`, - lists, [[Name]] links a note. Ctrl+Enter saves.",
      nst: { fresh: "up to date", changed: "code changed", gone: "code gone" },
      changedSince: "Changed", changedInfo: "changed since the index", affectedInfo: "use them",
      refreshed: "The index was updated: the page shows it now.", updating: "Updating the index…",
      staleNote: "This file changed since the index: the links and lines may be off. `verinoda update` takes it in.",
      ask: "Answer the question", answerFor: "Answer", answerMore: "Also relevant", answerNone: "Nothing in the project answers it.",
      answerStale: "changed since the index (the lines may be off):", answerExp: "also searched",
      impact: "Impact", impactTitle: "What may be affected", impactNone: "Nothing in the project uses it.",
      impactDepth: "links back", impactStop: "stopped at", impactTests: "in tests", withTests: "with tests",
      pathBtn: "Path…", pathTo: "Search the note to reach…", pathNone: "No chain of calls, imports or references links them.",
      pathBack: "(the other way round: the target reaches this note)", pathTitle: "Path", close2: "close",
      noteOn: "on", noNotes: "No notes of your own yet: open a note and press + Note.",
      theme: "Light / dark", toggleFiles: "Show or hide files", toggleGraph: "Show or hide the local graph",
      searchOffline: "Search files and symbols  (/)   ·   commands: Ctrl+K",
      offlineHome: "Exported view: the graph and a note per file, without the code; `verinoda ui` in the project shows every note with its code. Exported",
      offlineCode: "the code is not in the exported file; `verinoda ui` shows it",
      offlineMissing: "This note is not in the exported file (it holds the graph and the file notes); `verinoda ui` shows every note.",
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
        claims: "Claims", outline: "Outline", hubs: "Most connected", myNotes: "My notes", answerMore: "Also relevant",
      },
      kind: {
        class: "class", method: "method", function: "function", file: "file", doc: "document", section: "section",
        data: "data file", symbol: "symbol", external: "external", claim: "claim",
      },
      why: { name: "name", "name starts with": "name starts with", "name contains": "in the name", path: "in the path", question: "for the question" },
      legendTitle: "click: hide or show · double-click: fly to this region", mode3dTitle: "Show the graph in three dimensions (V)", tour: "Tour", tourTitle: "A guided tour of the project's regions (T)",
      keysTitle: "Keyboard shortcuts",
      palHint: "focus X · region X · impact X · path A to B · tour · or ask a question",
      palFoot: "↑↓ choose · ↵ run · Tab complete · Esc close",
      hint3d: "3D: drag to turn, wheel to zoom, click a file to fly to it. Ctrl+K gives commands, ? shows every shortcut.",
      hudOpen: "Open", hudFollow: "Follow", hudUnfollow: "Stop following", hudWalk: "Next linked", hudWalkMore: "N walks through them all",
      hudLinked: "Linked files", hudRegionTitle: "Fly to this region", hudInOut: "links in · links out", following: "following",
      walkAt: "{i} of {n} linked to {name} (N / Shift+N)", hudTop: "Most connected here", hudAll: "Everything",
      prevRegion: "Previous", nextRegion: "Next", tourResume: "Resume", tourPause: "Pause", tourAt: "Tour: region {i} of {n}",
      tourDone: "That was the tour: the whole project again.",
      hudPin: "Watch", hudUnpin: "Stop watching", watchedChanged: "Watched and changed since the index: {list}",
      keysPin: "Watch the selected file or region (changes are announced)",
      say: {
        data: "A data file: code names it by its resource id.",
        docOut: "{name} mentions {outs} files of the project.", docIn: "{name} is mentioned by {ins} files.",
        docBoth: "{name} mentions {outs} files of the project and is mentioned by {ins}.",
        both: "{name} is used by {ins} files and uses {outs}.",
        used: "{name} is used by {ins} files and uses none of the project's files: something the rest stands on.",
        uses: "{name} uses {outs} files and nothing uses it: an entry point, a script or a test.",
        alone: "{name} has no links to other files.",
        region: "{name}: {n} files, {links} links among them.",
        talks: "It works most with {list}.",
        island: "It has no links to other regions.",
        impact: "Changing {name} may affect {n} notes in {files} files (up to three links back).",
      },
      verb: { focus: "Fly to", open: "Open" },
      cmd: { graph: "Open the graph view", to3d: "Show the graph in 3D", to2d: "Show the graph flat (2D)", tour: "Take the tour of the project's regions",
        changes: "Show what changed since the index", fit: "Show everything", home: "Start page", lang: "Türkçe" },
      pal: { commands: "Commands", ask: "Question", notes: "Notes", regions: "Regions", try: "Try" },
      ex: { focus: "fly to a note in 3D", region: "frame a part of the project", impact: "what a change may affect",
        path: "how two notes are linked: path A to B", open: "read a note", ask: "a question, answered from the code" },
      palNoMatch: "No note matches one of the two names.", step1: "direct", stepN: "{n} steps away", spaceK: "Space",
      keys: { general: "Everywhere", palette: "Command bar", search: "Search box", graph: "Graph view", esc: "Close, clear, go back", help: "This list",
        graphH: "Graph view", view: "2D / 3D", dragK: "drag", drag: "Turn (3D) or move (2D)", rdragK: "right-drag", rdrag: "Move sideways",
        wheelK: "wheel", wheel: "Zoom", orbit: "Turn", zoom: "Zoom", clickK: "click", click: "Fly to a file", dblK: "double-click", open: "Open its note",
        num: "Fly to a numbered file in the panel", walk: "Walk the selected file's links", back: "Back along your trail", follow: "Follow the selected file",
        spin: "Turn slowly / stop (pause the tour)", regions: "Previous / next region", tour: "Tour of the regions", impact: "Impact of the selected file",
        changes: "What changed since the index", labels: "Labels", reset: "Show everything" },
    },
    tr: {
      search: "Not ara ya da soru sor  (/)   ·   komutlar: Ctrl+K", files: "Dosyalar", localGraph: "Yerel graf", depth: "Derinlik",
      tests: "Testler", dataFiles: "Veri dosyaları", external: "Dış", outline: "Ana hat", graphView: "Graf görünümü",
      highlight: "Notları vurgula…", labels: "Etiketler", fit: "Sığdır", close: "Kapat", back: "Geri", forward: "İleri",
      colorBy: "Renk", byFolder: "klasör", byCommunity: "topluluk",
      editor: "Editör", openIn: "Aç:", noEditor: "editör yok",
      myNote: "Notum", myNotes: "Notlarım", addNote: "+ Not", edit: "Düzenle", save: "Kaydet", cancel: "Vazgeç",
      del: "Sil", sure: "Silinsin mi?", keep: "Okudum: hâlâ doğru", noteHint: "Markdown: **kalın**, `kod`, - liste, [[Ad]] bir nota bağlar. Ctrl+Enter kaydeder.",
      nst: { fresh: "güncel", changed: "kod değişti", gone: "kod yok" },
      changedSince: "Değişenler", changedInfo: "indeksten sonra değişti", affectedInfo: "bunları kullanıyor",
      refreshed: "İndeks güncellendi: sayfa artık onu gösteriyor.", updating: "İndeks güncelleniyor…",
      staleNote: "Bu dosya indeksten sonra değişti: bağlantılar ve satırlar kaymış olabilir. `verinoda update` onu alır.",
      ask: "Soruyu cevapla", answerFor: "Cevap", answerMore: "Ayrıca ilgili", answerNone: "Projede bunu cevaplayan bir şey yok.",
      answerStale: "indeksten sonra değişti (satırlar kaymış olabilir):", answerExp: "ayrıca arandı",
      impact: "Etki", impactTitle: "Etkilenebilecekler", impactNone: "Projede bunu kullanan yok.",
      impactDepth: "bağlantı geri", impactStop: "şurada durdu:", impactTests: "testlerde", withTests: "testlerle",
      pathBtn: "Yol…", pathTo: "Ulaşılacak notu ara…", pathNone: "Aralarında çağrı, import ya da başvuru zinciri yok.",
      pathBack: "(ters yönde: hedef bu nota ulaşıyor)", pathTitle: "Yol", close2: "kapat",
      noteOn: "", noNotes: "Henüz kendi notun yok: bir not aç ve + Not'a bas.",
      theme: "Açık / koyu", toggleFiles: "Dosyaları göster ya da gizle", toggleGraph: "Yerel grafı göster ya da gizle",
      searchOffline: "Dosya ya da sembol ara  (/)   ·   komutlar: Ctrl+K",
      offlineHome: "Dışa aktarılmış görünüm: graf ve her dosyanın notu, kod olmadan; projede `verinoda ui` her notu koduyla gösterir. Dışa aktarım",
      offlineCode: "kod dışa aktarılan dosyada yok; `verinoda ui` gösterir",
      offlineMissing: "Bu not dışa aktarılan dosyada yok (graf ve dosya notları var); `verinoda ui` her notu gösterir.",
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
        myNotes: "Notlarım", answerMore: "Ayrıca ilgili",
      },
      kind: {
        class: "sınıf", method: "metot", function: "fonksiyon", file: "dosya", doc: "belge", section: "bölüm",
        data: "veri dosyası", symbol: "sembol", external: "dış", claim: "iddia",
      },
      why: { name: "ad", "name starts with": "ad böyle başlıyor", "name contains": "adında geçiyor", path: "yolunda geçiyor", question: "soruya göre" },
      legendTitle: "tıkla: gizle ya da göster · çift tıkla: bu bölgeye uç", mode3dTitle: "Grafı üç boyutlu göster (V)", tour: "Tur", tourTitle: "Projenin bölgelerinde rehberli tur (T)",
      keysTitle: "Klavye kısayolları",
      palHint: "odak X · bölge X · etki X · yol A ile B · tur · ya da bir soru sor",
      palFoot: "↑↓ seç · ↵ çalıştır · Tab tamamla · Esc kapat",
      hint3d: "3D: sürükleyerek döndür, tekerlekle yakınlaş, bir dosyaya tıkla ve ona uç. Ctrl+K komutları, ? bütün kısayolları gösterir.",
      hudOpen: "Aç", hudFollow: "İzle", hudUnfollow: "İzlemeyi bırak", hudWalk: "Sıradaki bağlantı", hudWalkMore: "N hepsini tek tek gezer",
      hudLinked: "Bağlı dosyalar", hudRegionTitle: "Bu bölgeye uç", hudInOut: "gelen · giden bağlantı", following: "izleniyor",
      walkAt: "{name} ile bağlantılı {n} dosyanın {i}. (N / Shift+N)", hudTop: "Burada en çok bağlantılı", hudAll: "Hepsi",
      prevRegion: "Önceki", nextRegion: "Sonraki", tourResume: "Sürdür", tourPause: "Duraklat", tourAt: "Tur: {n} bölgenin {i}.",
      tourDone: "Tur bitti: projenin tamamı yeniden karşında.",
      hudPin: "İzlemeye al", hudUnpin: "İzlemeyi kaldır", watchedChanged: "İzlenen ve indeksten sonra değişen: {list}",
      keysPin: "Seçili dosyayı ya da bölgeyi izle (değişince söylenir)",
      say: {
        data: "Bir veri dosyası: kod onu kaynak kimliğiyle anıyor.",
        docOut: "{name} projenin {outs} dosyasından söz ediyor.", docIn: "{ins} dosya {name} belgesinden söz ediyor.",
        docBoth: "{name} projenin {outs} dosyasından söz ediyor, {ins} dosya da ondan söz ediyor.",
        both: "{name}: {ins} dosya onu kullanıyor, o {outs} dosyayı kullanıyor.",
        used: "{name}: {ins} dosya onu kullanıyor, kendisi projenin hiçbir dosyasını kullanmıyor: geri kalanın dayandığı bir parça.",
        uses: "{name}: {outs} dosyayı kullanıyor, onu kullanan yok: bir giriş noktası, betik ya da test.",
        alone: "{name} başka dosyalara bağlı değil.",
        region: "{name}: {n} dosya, aralarında {links} bağlantı.",
        talks: "En çok birlikte çalıştığı: {list}.",
        island: "Başka bölgelerle bağlantısı yok.",
        impact: "{name} değişirse {files} dosyada {n} not etkilenebilir (en çok üç bağlantı geri).",
      },
      verb: { focus: "Uç", open: "Aç" },
      cmd: { graph: "Graf görünümünü aç", to3d: "Grafı 3D göster", to2d: "Grafı düz (2D) göster", tour: "Projenin bölgelerinde tur at",
        changes: "İndeksten sonra değişenleri göster", fit: "Hepsini göster", home: "Başlangıç sayfası", lang: "English" },
      pal: { commands: "Komutlar", ask: "Soru", notes: "Notlar", regions: "Bölgeler", try: "Dene" },
      ex: { focus: "3D'de bir nota uç", region: "projenin bir bölümüne odaklan", impact: "bir değişiklik neyi etkileyebilir",
        path: "iki not nasıl bağlı: yol A ile B", open: "bir notu oku", ask: "koddan cevaplanan bir soru" },
      palNoMatch: "İki addan biriyle eşleşen not yok.", step1: "doğrudan", stepN: "{n} adım öte", spaceK: "Boşluk",
      keys: { general: "Her yerde", palette: "Komut çubuğu", search: "Arama kutusu", graph: "Graf görünümü", esc: "Kapat, temizle, geri dön", help: "Bu liste",
        graphH: "Graf görünümü", view: "2D / 3D", dragK: "sürükle", drag: "Döndür (3D) ya da kaydır (2D)", rdragK: "sağ sürükle", rdrag: "Yana kaydır",
        wheelK: "tekerlek", wheel: "Yakınlaş", orbit: "Döndür", zoom: "Yakınlaş", clickK: "tıkla", click: "Dosyaya uç", dblK: "çift tık", open: "Notunu aç",
        num: "Paneldeki numaralı dosyaya uç", walk: "Seçili dosyanın bağlantılarını gez", back: "İzinden geri dön", follow: "Seçili dosyayı izle",
        spin: "Yavaşça döndür / durdur (turu duraklat)", regions: "Önceki / sonraki bölge", tour: "Bölgelerde tur", impact: "Seçili dosyanın etkisi",
        changes: "İndeksten sonra değişenler", labels: "Etiketler", reset: "Hepsini göster" },
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
    if (G3) { G3.dirty = true; G3.kick(); }
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

  // -- open in an editor (a served page knows the project's folder; an exported file does not) --------
  const EDITORS = { vscode: ["VS Code", "vscode"], cursor: ["Cursor", "cursor"], vscodium: ["VSCodium", "vscodium"] };
  let editor = store.get("vn.editor", "vscode");
  if (editor !== "none" && !EDITORS[editor]) editor = "vscode";
  let projectRoot = null; // from /api/stats
  let statsReady = Promise.resolve(null); // a note is drawn once the project's folder is known
  function editorHref(file, line) {
    if (OFFLINE || !projectRoot || !file || editor === "none") return null;
    const abs = (projectRoot.replace(/\\/g, "/").replace(/\/+$/, "") + "/" + file).replace(/^\/+/, "");
    const path = encodeURI(abs).replace(/#/g, "%23").replace(/\?/g, "%3F");
    return `${EDITORS[editor][1]}://file/${path}${line ? ":" + line : ""}`;
  }
  function atLink(at) { // "file:line" as a link that opens the editor there (text when it cannot)
    const m = /^(.*):(\d+)$/.exec(at || "");
    const href = m ? editorHref(m[1], m[2]) : editorHref(at, null);
    return href ? el("a", { class: "at mono", href, title: `${t("openIn")} ${EDITORS[editor][0]}` }, at)
      : el("span", { class: "at mono", text: at });
  }

  // -- note rendering --------------------------------------------------------------------------
  const KIND_LETTER = { class: "C", method: "M", function: "F", file: "F", doc: "D", section: "§", data: "{}", symbol: "S", external: "E", claim: "!", question: "?" };
  const kindBadge = (kind) => el("span", { class: "kbadge k-" + (kind || "symbol"), title: t("kind." + kind), text: KIND_LETTER[kind] || "·" });
  function noteLink(item, cls) {
    if (!item.id) return el("span", { class: cls || "", title: item.file || "" }, item.title); // no note to open
    return el("a", { href: noteHref(item.id), class: cls || "", title: [item.file, item.line ? `:${item.line}` : ""].join("") }, item.title);
  }
  let current = null;
  const main = $("#note");

  function setMain(...kids) { main.replaceChildren(...kids); $("#main").scrollTop = 0; }

  async function openNote(id, quiet = false) {
    current = id;
    const keep = quiet ? $("#main").scrollTop : null;
    if (!quiet) setMain(el("div", { class: "empty", text: t("loading") }));
    let n;
    try { n = await api("/api/note?id=" + encodeURIComponent(id)); }
    catch (e) {
      if (current === id) {
        showError(e);
        $("#outline").replaceChildren(); localSeq++; local.setData([], []); markTree(null);
      }
      return;
    }
    await statsReady;
    if (current !== id) return;
    renderNote(n);
    if (keep !== null) $("#main").scrollTop = keep;
    loadLocal(id);
    markTree(n.file);
    document.title = `${n.title} · Verinoda`;
  }

  function showError(e) {
    setMain(el("div", { class: "empty error", text: e.code === "no_index" ? t("noIndex") : `${e.status || ""} ${e.message}` }));
  }

  function renderNote(n) {
    lastNote = { id: n.id, file: n.file, shown: false }; // the graph view opens on it
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
    const own = n.file && n.kind !== "data" ? editorHref(n.file, n.line) : n.file ? editorHref(n.file, n.span && n.span[0]) : null;
    if (own) meta.append(el("a", { class: "chip editor", href: own, title: `${n.file}${n.line ? ":" + n.line : ""}` }, `↗ ${EDITORS[editor][0]}`));
    parts.push(meta);
    if (n.stale) parts.push(el("div", { class: "unote st-changed small", text: t("staleNote") }));
    if (OFFLINE && n.code_lines) parts.push(el("div", { class: "muted small", text: `${n.code_lines} ${t("lines")} · ${t("offlineCode")}` }));
    const editable = !OFFLINE && TOKEN && n.can_note;
    if (!n.user_note && editable) meta.append(el("button", { class: "chip addnote", onclick: () => editUserNote(n, "") }, t("addNote")));
    if (n.kind !== "data" && n.kind !== "external") {
      meta.append(el("button", { class: "chip addnote", onclick: () => showImpact(n) }, t("impact")));
      meta.append(el("button", { class: "chip addnote", onclick: () => askPath(n) }, t("pathBtn")));
    }
    if (n.user_note) parts.push(userNoteBox(n));
    for (const u of n.user_notes || []) parts.push(userNoteBox(n, u)); // an exported file: the notes on the file's symbols
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

  // -- impact and paths --------------------------------------------------------------------------
  function panel(id, title, ...kids) { // one panel under the note's header, replacing an earlier one
    const box = el("div", { class: "unote panel", id }, el("div", { class: "unote-head" }, el("strong", { text: title }),
      el("span", { class: "spacer" }), el("button", { class: "linkish", onclick: () => box.remove() }, t("close2"))), ...kids);
    const old = $("#" + id);
    if (old) old.replaceWith(box);
    else { const meta = $("#note .meta"); if (meta) meta.after(box); else main.prepend(box); }
    return box;
  }
  async function showImpact(n, tests = true) {
    const box = panel("impact", t("impactTitle"), el("div", { class: "muted small", text: t("loading") }));
    let r;
    try { r = await api(`/api/impact?id=${encodeURIComponent(n.id)}&depth=3&tests=${tests ? 1 : 0}`); }
    catch (e) { box.append(el("div", { class: "empty error", text: e.message })); return; }
    if (current !== n.id) return;
    const toggle = el("label", { class: "small" }, el("input", { type: "checkbox", checked: tests, onchange: (ev) => showImpact(n, ev.target.checked) }), " " + t("withTests"));
    const head = el("div", { class: "muted small" }, `${r.count} ${t("notes")} · ${r.files} ${t("filesN")}` +
      (r.tests ? ` (${r.tests} ${t("impactTests")})` : "") + (r.truncated ? ` · ${t("impactStop")} ${r.count}` : ""), " ", toggle);
    const body = [head];
    if (!r.items.length) body.push(el("p", { class: "muted", text: t("impactNone") }));
    for (const d of [...new Set(r.items.map((x) => x.depth))]) {
      const group = r.items.filter((x) => x.depth === d);
      body.push(el("div", { class: "sec small", text: `${d} ${t("impactDepth")} · ${group.length}` }));
      body.push(el("ul", { class: "links" }, group.slice(0, 80).map((it) => el("li", {}, kindBadge(it.kind), noteLink(it),
        el("span", { class: "rel", text: `${it.relation} → ${it.via_title}` }), it.at ? atLink(it.at) : null))));
      if (group.length > 80) body.push(el("div", { class: "muted small", text: `+${group.length - 80} ${t("more")}` }));
    }
    box.replaceWith(panel("impact", `${t("impactTitle")} · ${n.title}`, ...body));
    // the right pane shows the same: the note and what depends on it
    const ids = new Set([n.id, ...r.items.map((x) => x.id)]);
    local.setData([{ id: n.id, title: n.title, kind: n.kind, group: n.community ? n.community.id : "other" },
      ...r.items.slice(0, 220).map((x) => ({ id: x.id, title: x.title, kind: x.kind, group: x.group, depth: x.depth }))],
      r.items.slice(0, 220).filter((x) => ids.has(x.via)).map((x) => ({ source: x.id, target: x.via, relation: x.relation })), { center: n.id });
  }
  function askPath(n) {
    const input = el("input", { type: "search", class: "pathq", placeholder: t("pathTo"), autocomplete: "off" });
    const list = el("ul", { class: "links" });
    const out = el("div");
    let seq = 0, timer = null;
    input.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const q = input.value.trim(), my = ++seq;
        if (!q) { list.replaceChildren(); return; }
        let r;
        try { r = await api("/api/search?q=" + encodeURIComponent(q)); } catch (_) { return; }
        if (my !== seq) return;
        list.replaceChildren(...(r.results || []).slice(0, 8).map((it) => el("li", {}, kindBadge(it.kind),
          el("a", { href: "#", onclick: (ev) => { ev.preventDefault(); showPath(n, it, out); } }, it.title),
          el("span", { class: "at mono", text: it.file || "" }))));
      }, 150);
    });
    panel("path", `${t("pathTitle")} · ${n.title}`, input, list, out);
    input.focus();
  }
  async function showPath(n, target, out) {
    let r;
    try { r = await api(`/api/path?from=${encodeURIComponent(n.id)}&to=${encodeURIComponent(target.id)}`); }
    catch (e) { out.replaceChildren(el("div", { class: "empty error", text: e.message })); return; }
    if (!r.found) { out.replaceChildren(el("p", { class: "muted", text: t("pathNone") })); return; }
    const chain = el("ol", { class: "links chain" }, r.steps.map((s, i) => el("li", {},
      i ? el("span", { class: "rel", text: `↳ ${s.relation}` }) : null, kindBadge(s.kind), noteLink(s), s.at ? atLink(s.at) : null)));
    out.replaceChildren(r.direction === "backward" ? el("p", { class: "muted small", text: t("pathBack") }) : "", chain);
    const ids = r.steps.map((s) => s.id);
    local.setData(r.steps.map((s) => ({ id: s.id, title: s.title, kind: s.kind, group: s.group })),
      ids.slice(1).map((id, i) => ({ source: ids[i], target: id, relation: r.steps[i + 1].relation })), { center: n.id });
  }

  // -- notes of your own -------------------------------------------------------------------------
  function mdInline(text) { // `code`, **bold**, [[Name]]: DOM nodes, never HTML
    const out = [];
    const rx = /`([^`]+)`|\*\*([^*]+)\*\*|\[\[([^\]]+)\]\]/g;
    let last = 0, m;
    while ((m = rx.exec(text))) {
      if (m.index > last) out.push(text.slice(last, m.index));
      if (m[1] !== undefined) out.push(el("code", { text: m[1] }));
      else if (m[2] !== undefined) out.push(el("strong", { text: m[2] }));
      else {
        const name = m[3].trim();
        out.push(el("a", { href: "#", class: "wikilink", onclick: (ev) => { ev.preventDefault(); openByName(name); } }, name));
      }
      last = rx.lastIndex;
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  }
  function mdBlocks(text) {
    const lines = text.replace(/\r\n/g, "\n").split("\n"), out = [];
    for (let i = 0; i < lines.length;) {
      if (/^```/.test(lines[i])) { // fenced code
        const body = [];
        for (i++; i < lines.length && !/^```/.test(lines[i]); i++) body.push(lines[i]);
        i++;
        out.push(el("pre", { class: "mono", text: body.join("\n") }));
      } else if (/^\s*[-*] /.test(lines[i])) {
        const ul = el("ul");
        for (; i < lines.length && /^\s*[-*] /.test(lines[i]); i++) ul.append(el("li", {}, mdInline(lines[i].replace(/^\s*[-*] /, ""))));
        out.push(ul);
      } else if (!lines[i].trim()) { i++; }
      else {
        const para = el("p");
        for (let first = true; i < lines.length && lines[i].trim() && !/^```/.test(lines[i]) && !/^\s*[-*] /.test(lines[i]); i++, first = false) {
          if (!first) para.append(el("br"));
          para.append(...mdInline(lines[i]));
        }
        out.push(para);
      }
    }
    return out;
  }
  async function openByName(name) { // [[Name]]: the note of that name, else the best match
    let r;
    try { r = await api("/api/search?q=" + encodeURIComponent(name)); } catch (_) { return; }
    const items = r.results || [], lower = name.toLowerCase().replace(/\(\)$/, "");
    const hit = items.find((x) => x.title.toLowerCase().replace(/\(\)$/, "") === lower) || items[0];
    if (hit) location.hash = noteHref(hit.id);
  }
  function userNoteBox(n, u) {
    const note = u || n.user_note, own = !u;
    const st = note.status || "fresh";
    const head = el("div", { class: "unote-head" }, el("strong", { text: own ? t("myNote") : `${t("myNote")} · ${note.label || note.subject}` }),
      el("span", { class: "unote-st st-" + st, title: note.why || "", text: t("nst." + st) }));
    if (note.written) head.append(el("span", { class: "muted small", text: note.written.replace("T", " ").replace("Z", " UTC") }));
    const box = el("div", { class: "unote st-" + st, id: own ? "sec-mynote" : null }, head, el("div", { class: "unote-body" }, mdBlocks(note.text || "")));
    if (st !== "fresh" && note.why) box.append(el("div", { class: "muted small", text: note.why }));
    if (own && !OFFLINE && TOKEN) {
      const bar = el("div", { class: "unote-bar" }, el("button", { onclick: () => editUserNote(n, note.text) }, t("edit")));
      if (st === "changed") bar.append(el("button", { onclick: () => writeUserNote(n, "", true) }, t("keep")));
      const del = el("button", { class: "danger" }, t("del"));
      del.addEventListener("click", () => { // two steps, no dialog
        if (del.dataset.armed) writeUserNote(n, ""); else { del.dataset.armed = "1"; del.textContent = t("sure"); }
      });
      bar.append(del);
      box.append(bar);
    }
    return box;
  }
  function editUserNote(n, text) {
    const area = el("textarea", { class: "mono", rows: 8, spellcheck: "true" });
    area.value = text || "";
    const save = () => writeUserNote(n, area.value), cancel = () => route();
    area.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); save(); }
      else if (ev.key === "Escape") { ev.preventDefault(); cancel(); }
    });
    const box = el("div", { class: "unote editing", id: "sec-mynote" }, el("div", { class: "unote-head" }, el("strong", { text: t("myNote") })),
      area, el("div", { class: "muted small", text: t("noteHint") }),
      el("div", { class: "unote-bar" }, el("button", { class: "primary", onclick: save }, t("save")), el("button", { onclick: cancel }, t("cancel"))));
    const old = $("#sec-mynote");
    if (old) old.replaceWith(box);
    else { const meta = $("#note .meta"); if (meta) meta.after(box); else main.prepend(box); }
    area.focus();
  }
  async function writeUserNote(n, text, keep = false) {
    try { await apiPost("/api/usernote", { id: n.id, text, keep }); }
    catch (e) {
      const box = $("#sec-mynote") || main;
      box.append(el("div", { class: "empty error", text: `${e.status || ""} ${e.message}` }));
      return;
    }
    route(); // the note again, from the server
  }

  function sectionHeader(key, count) {
    return el("h2", { class: "sec", id: "sec-" + key }, t("sec." + key), el("span", { class: "n", text: String(count) }));
  }

  function linkItem(it, key) {
    if (key === "claims") {
      return el("li", {}, el("span", { class: "status st-" + it.status, text: it.status.replace(/_/g, " ") }),
        el("span", { text: it.title }), it.at ? el("span", { class: "at mono", text: it.at }) : null);
    }
    const inferred = it.confidence && it.confidence !== "EXTRACTED";
    const kids = [kindBadge(it.kind), noteLink(it, inferred ? "inferred" : "")];
    if (inferred) kids.push(el("span", { class: "q", title: t("inferred") + (it.context ? ` (${it.context})` : ""), text: "?" }));
    if (key === "names_data" || key === "named_by") {
      // the id that links them, and where: the file named (names) or the line naming this note (named by)
      const rid = (it.relation || "").replace(/^names /, "");
      if (rid && rid !== it.title) kids.push(el("span", { class: "rel mono", text: rid }));
      const where = key === "names_data" ? it.file : it.at;
      if (where) kids.push(atLink(where));
      if (it.snippet) kids.push(el("div", { class: "snip mono", text: it.snippet }));
      return el("li", {}, kids);
    }
    const rel = it.relation && !["calls", "method", "contains", "imports", "references"].includes(it.relation) ? it.relation : "";
    if (rel) kids.push(el("span", { class: "rel", text: rel }));
    if (it.at) kids.push(atLink(it.at));
    if (it.snippet) kids.push(el("div", { class: "snip mono", text: it.snippet })); // the line the link is written on
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
    if (n.user_note) box.append(el("a", { href: "#", onclick: (ev) => { ev.preventDefault(); const h = $("#sec-mynote"); if (h) h.scrollIntoView({ behavior: "smooth", block: "start" }); } },
      `${t("myNote")} · ${t("nst." + (n.user_note.status || "fresh"))}`));
    for (const s of n.sections || []) {
      box.append(el("a", { href: "#", onclick: (ev) => { ev.preventDefault(); const h = document.getElementById("sec-" + s.key); if (h) h.scrollIntoView({ behavior: "smooth", block: "start" }); } },
        `${t("sec." + s.key)} (${s.count})`));
    }
  }

  // the start page is what the address shows when it names nothing else
  const onHome = () => { const h = location.hash || "#/"; return !(h === "#/graph" || h.startsWith("#/n/") || (h.startsWith("#/q/") && !OFFLINE)); };
  async function renderHome() {
    current = null;
    document.title = "Verinoda";
    let s;
    try { s = await api("/api/stats"); } catch (e) { if (onHome()) showError(e); return; }
    if (!onHome()) return; // a note was opened while the numbers came
    const cards = el("div", { class: "cards" },
      [[s.notes, t("notes")], [s.files, t("filesN")], [s.links, t("links")], [s.data_notes, t("dataNotes")]]
        .map(([v, l]) => el("div", { class: "card" }, el("div", { class: "v", text: Number(v).toLocaleString() }), el("div", { class: "l", text: l }))));
    const hubs = el("ul", { class: "links" }, (s.hubs || []).map((h) => el("li", {}, kindBadge(h.kind), noteLink(h),
      el("span", { class: "at mono", text: `${h.degree} · ${h.file || ""}` }))));
    let mine = [];
    try { mine = OFFLINE ? (OFFLINE.user_notes || []) : ((await api("/api/usernotes")).notes || []); } catch (_) { mine = []; }
    if (!onHome()) return;
    const notesList = mine.length ? el("ul", { class: "links" }, mine.map((u) => {
      const li = el("li", {},
        el("span", { class: "unote-st st-" + u.status, title: u.why || "", text: t("nst." + u.status) }),
        u.id ? el("a", { href: noteHref(u.id) }, u.subject) : el("span", { text: u.subject }),
        el("span", { class: "at", text: (u.text || "").split("\n")[0].slice(0, 80) }));
      if (!OFFLINE && TOKEN && (u.status === "gone" || !u.id)) { // its code is gone: no note page to delete it on
        const del = el("button", { class: "danger small" }, t("del"));
        del.addEventListener("click", async () => {
          if (!del.dataset.armed) { del.dataset.armed = "1"; del.textContent = t("sure"); return; }
          try { await apiPost("/api/usernote", { subject: u.subject, delete: true }); } catch (e) { del.textContent = e.message; return; }
          route();
        });
        li.append(del);
      }
      return li;
    })) : el("p", { class: "muted small", text: t("noNotes") });
    setMain(el("div", { class: "home" }, el("h1", { text: s.project }), el("p", { class: "muted", text: t("welcome") }),
      OFFLINE ? el("p", { class: "muted small", text: `${t("offlineHome")} ${OFFLINE.generated || ""}` }) : null,
      cards, sectionHeader("myNotes", mine.length), notesList, sectionHeader("hubs", (s.hubs || []).length), hubs));
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
      if (items[active]) go(items[active].id, items[active].question);
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
    if (!OFFLINE && isQuestion(q)) items = [{ id: null, question: q, title: `${t("ask")}: «${q}»`, kind: "question" }, ...items];
    active = items.length ? 0 : -1;
    results.replaceChildren(...(items.length ? items.map((it, i) => el("div", {
      class: "result" + (it.question ? " ask" : ""), role: "option", onclick: () => go(it.id, it.question),
      onmousemove: () => { active = i; paintActive(); },
    }, kindBadge(it.kind), el("span", { class: "t", text: it.title }), el("span", { class: "p", text: it.file || "" }),
    el("span", { class: "w", text: (I18N[lang].why || {})[it.why] || it.why || "" }))) : [el("div", { class: "result muted", text: t("noResults") })]));
    results.hidden = false;
    paintActive();
  }
  function paintActive() {
    [...results.children].forEach((c, i) => c.classList.toggle("active", i === active));
    const a = results.children[active]; if (a) a.scrollIntoView({ block: "nearest" });
  }
  function go(id, question) {
    cancelSearch(); results.hidden = true; input.blur();
    location.hash = question ? "#/q/" + encodeURIComponent(question) : noteHref(id);
  }
  const QUESTION_WORDS = /^(how|what|where|why|which|who|when|does|do|is|are|can|nasıl|ne|neden|nerede|hangi|kim|niçin|niye)\b/i;
  function isQuestion(q) { return /\?\s*$/.test(q) || QUESTION_WORDS.test(q) || q.trim().split(/\s+/).length >= 3; }

  // -- an answer to a question: the passages `verinoda query` would give ---------------------------
  async function renderAnswer(question) {
    current = null;
    document.title = `${question} · Verinoda`;
    setMain(el("div", { class: "empty", text: t("loading") }));
    $("#outline").replaceChildren(); local.setData([], []); markTree(null);
    let r;
    try { r = await api("/api/answer?q=" + encodeURIComponent(question)); } catch (e) { showError(e); return; }
    if (location.hash !== "#/q/" + encodeURIComponent(question)) return; // another page was opened meanwhile
    const parts = [el("div", { class: "crumbs", text: t("answerFor") }), el("h1", { text: r.question })];
    if (r.expansions.length) parts.push(el("div", { class: "muted small", text: `${t("answerExp")}: ${r.expansions.join(", ")}` }));
    if (r.stale_files.length) parts.push(el("div", { class: "muted small", text: `${t("answerStale")} ${r.stale_files.join(", ")}` }));
    if (!r.items.length) parts.push(el("p", { class: "muted", text: t("answerNone") }));
    r.items.forEach((it, k) => {
      const first = it.lines ? it.lines[0] : null;
      const head = el("h2", { class: "sec", id: "ans-" + k }, kindBadge(it.kind), " ", it.id ? noteLink(it) : el("span", { text: it.title }),
        " ", atLink(first ? `${it.file}:${first}` : it.file));
      const lines = String(it.excerpt || "").split("\n");
      parts.push(head, el("div", { class: "why" }, it.why.map((w) => el("span", { class: "chip", text: w }))));
      if (it.excerpt) parts.push(codeBlock({ start: first || 1, end: (first || 1) + lines.length - 1, lines, total: lines.length, lang: it.lang }, it.file));
    });
    if (r.more.length) parts.push(sectionHeader("answerMore", r.more.length), el("ul", { class: "links" },
      r.more.map((m) => { const at = m.split(" ")[0]; return el("li", {}, atLink(at.replace(/-\d+$/, "")), el("span", { class: "rel", text: m.slice(at.length + 1) })); })));
    setMain(el("div", { class: "answer" }, ...parts));
    const box = $("#outline");
    r.items.forEach((it, k) => box.append(el("a", { href: "#", onclick: (ev) => { ev.preventDefault(); const h = $("#ans-" + k); if (h) h.scrollIntoView({ behavior: "smooth", block: "start" }); } }, it.title)));
  }

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
    marked(n) { return this.marks && this.marks.get(n.id); }
    draw() {
      const ctx = this.ctx, k = this.tf.k;
      ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      ctx.clearRect(0, 0, this.w, this.h);
      ctx.translate(this.w / 2 + this.tf.x, this.h / 2 + this.tf.y);
      ctx.scale(k, k);
      const focus = this.hover, nb = focus ? this.adj.get(focus.id) : null;
      const dim = !!focus || !!this.filter || !!(this.marks && this.marks.size);
      for (const l of this.links) {
        if (!this.visible(l.s) || !this.visible(l.t)) continue;
        const on = focus ? (l.s === focus || l.t === focus) : this.filter ? (this.matches(l.s) || this.matches(l.t))
          : this.marks && this.marks.size ? (this.marked(l.s) && this.marked(l.t)) : true;
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
        const mark = this.marked(n);
        const on = focus ? (n === focus || nb.has(n.id)) : this.filter ? this.matches(n) : this.marks && this.marks.size ? !!mark : true;
        ctx.globalAlpha = dim ? (on ? 1 : 0.15) : 1;
        ctx.fillStyle = groupColor(n.group);
        ctx.beginPath(); ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2); ctx.fill();
        if (mark) { // what changed since the index, and what uses it
          ctx.lineWidth = 3 / k; ctx.strokeStyle = mark === "changed" ? (colors.warn || "#e0a34a") : "#d9d05a"; ctx.stroke();
        }
        if (n === this.center || n === focus || (this.filter && this.matches(n))) {
          ctx.lineWidth = 2.2 / k; ctx.strokeStyle = colors.accent || "#8e7cf5"; ctx.stroke();
        }
      }
      if (!this.o.labels && !focus) { ctx.globalAlpha = 1; return; }
      ctx.textAlign = "center"; ctx.textBaseline = "top";
      ctx.font = `${12 / k}px system-ui, -apple-system, "Segoe UI", sans-serif`;
      for (const n of this.nodes) {
        if (!this.visible(n)) continue;
        const near = n === focus || (nb && nb.has(n.id)) || n === this.center || this.matches(n) || this.marked(n) === "changed";
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
    graphChrome();
    if (mode3d) G3.setActive(true); else globalG.resize();
    const key = `${$("#global-tests").checked}|${$("#global-data").checked}`;
    if (key === globalKey) {
      if (mode3d) { enter3d(false); focusCurrent(); } else { ensure2d(); globalG.fit(); }
      return;
    }
    const seq = ++globalSeq;
    $("#graph-info").textContent = t("loading");
    let g;
    try { g = await api(`/api/global?tests=${$("#global-tests").checked ? 1 : 0}&data=${$("#global-data").checked ? 1 : 0}`); }
    catch (e) { if (seq === globalSeq) $("#graph-info").textContent = e.message; return; }
    if (seq !== globalSeq) return; // a newer request (the boxes changed again) is on its way
    globalKey = key; globalData = g; globalGen++;
    applyColors(g.nodes);
    if (mode3d) { enter3d(false); focusCurrent(); } else ensure2d(); // the other view is filled when it is shown
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
    if (globalG.nodes.length) applyColors(globalG.nodes);
    if (G3 && G3.nodes.length) applyColors(G3.nodes);
    applyColors(globalData.nodes); // the next view filled from it gets the same colours
    globalG.hidden.clear(); globalG.dirty = true; globalG.kick();
    if (G3) { G3.dirty = true; G3.kick(); if (!G3.region && G3.selected) hudNode(G3.selected); }
    renderLegend({ nodes: globalData.nodes, groups: globalData.groups });
  });
  function renderLegend(g) {
    const used = new Map();
    for (const n of g.nodes) used.set(String(n.group), (used.get(String(n.group)) || 0) + 1);
    const names = new Map((g.groups || []).map((x) => [String(x.id), x.name]));
    const order = [...used.entries()].sort((a, b) => b[1] - a[1]).slice(0, 24);
    $("#legend").replaceChildren(...order.map(([grp, count]) => {
      const sw = el("span", { class: "sw" });
      sw.style.background = groupColor(/^-?\d+$/.test(grp) ? Number(grp) : grp);
      const it = el("div", { class: "it" + (globalG.hidden.has(grp) ? " off" : ""), title: G3 ? t("legendTitle") : null }, sw,
        el("span", { text: `${grp === "data" ? t("dataFiles") : grp.startsWith("@") ? grp.slice(1) : names.get(grp) || grp} (${count})` }));
      it.addEventListener("click", () => {
        if (globalG.hidden.has(grp)) globalG.hidden.delete(grp); else globalG.hidden.add(grp);
        it.classList.toggle("off"); globalG.dirty = true; globalG.kick();
        if (G3) { G3.dirty = true; G3.kick(); }
      });
      it.addEventListener("dblclick", () => { if (G3 && mode3d) { globalG.hidden.delete(grp); it.classList.remove("off"); focusRegion(grp); } });
      return it;
    }));
  }
  $("#changes-box").hidden = !!OFFLINE; // an exported file has no working tree to compare
  async function showChanges(on) {
    globalG.marks = null;
    if (G3) G3.marks = null;
    if (on) {
      let r;
      try { r = await api("/api/changes"); } catch (e) { $("#graph-info").textContent = e.message; return; }
      const marks = new Map();
      for (const c of [...r.edited, ...r.deleted]) if (c.id) marks.set(c.id, "changed");
      const users = new Map(); // the files that use a changed one (one link back)
      const links = mode3d && G3 ? G3.links : globalG.links;
      for (const l of links) if (marks.get(l.t.id) === "changed" && !marks.has(l.s.id)) users.set(l.s.id, "affected");
      for (const [id, v] of users) marks.set(id, v);
      globalG.marks = marks;
      if (G3) G3.marks = marks;
      const changed = r.edited.length + r.deleted.length;
      $("#graph-info").textContent = `${changed} ${t("changedInfo")}` + (r.added.length ? ` (+${r.added.length})` : "") +
        ` · ${users.size} ${t("affectedInfo")}`;
    } else if (globalData) {
      $("#graph-info").textContent = `${globalData.nodes.length} ${t("filesN")}, ${globalData.edges.length} ${t("links")}`;
    }
    globalG.dirty = true; globalG.kick();
    if (G3) { G3.dirty = true; G3.kick(); }
  }
  $("#global-changes").addEventListener("change", (ev) => showChanges(ev.target.checked));
  $("#graph-filter").addEventListener("input", (ev) => {
    globalG.filter = ev.target.value.trim().toLowerCase(); globalG.dirty = true; globalG.kick();
    if (G3) { G3.filter = globalG.filter; G3.dirty = true; G3.kick(); }
  });
  $("#global-labels").addEventListener("change", (ev) => {
    globalG.o.labels = ev.target.checked; globalG.dirty = true; globalG.kick();
    if (G3) { G3.o.labels = ev.target.checked; G3.dirty = true; G3.kick(); }
  });
  for (const id of ["global-tests", "global-data"]) $("#" + id).addEventListener("change", () => showGraph());
  $("#graph-fit").addEventListener("click", () => graphKey({ key: "r" }));
  let beforeGraph = "#/";
  $("#graph-close").addEventListener("click", () => { location.hash = beforeGraph; });
  $("#btn-graph").addEventListener("click", () => { location.hash = "#/graph"; });

  // -- the graph in three dimensions: fly to a file, follow it, walk its links, regions, a tour ------
  const G3 = window.VerinodaGraph3D ? new window.VerinodaGraph3D($("#global3d"), {
    color: (n) => groupColor(n.group), linkColor: (l) => REL_COLOR[l.relation] || "#777", theme: () => colors,
    onSelect: (n) => { stopTour(); if (n) select3d(n); else clear3d(); },
    onOpen: (n) => { location.hash = noteHref(n.id); },
    onHover: (n, ev) => {
      const tip = $("#tooltip");
      if (!n || !ev) { tip.hidden = true; return; }
      tip.hidden = false; tip.textContent = n.file ? `${n.title} — ${n.file}` : n.title;
      tip.style.left = `${ev.clientX + 14}px`; tip.style.top = `${ev.clientY + 12}px`;
    },
  }) : null;
  if (G3) G3.hidden = globalG.hidden; // one legend hides groups in both views
  let mode3d = !!G3 && store.get("vn.graph3d", "0") === "1";
  let globalGen = 0, hudTargets = [], walk = null, tour = null, lastNote = null, regionKey = null;
  const trail = [], hud = $("#hud");
  const fill = (s, v) => String(s).replace(/\{(\w+)\}/g, (_, k) => (v[k] !== undefined ? v[k] : ""));
  const until = (f, ms = 10000) => new Promise((ok) => {
    const t0 = performance.now();
    (function check() { if (f() || performance.now() - t0 > ms) ok(); else setTimeout(check, 40); })();
  });

  function graphChrome() {
    for (const id of ["graph-3d", "graph-tour"]) $("#" + id).hidden = !G3;
    setTimeout(() => { renderWatched(); checkWatched(); }, 0);
    $("#graph-3d").classList.toggle("on", mode3d);
    $("#global").hidden = mode3d; $("#global3d").hidden = !mode3d;
    if (!mode3d) hud.hidden = true;
  }
  function ensure2d() {
    if (globalData && globalG.dataKey !== globalGen) { globalG.setData(globalData.nodes, globalData.edges); globalG.dataKey = globalGen; }
  }
  function enter3d(inflate) {
    if (!G3 || !globalData) return;
    G3.setActive(true);
    G3.filter = globalG.filter; G3.marks = globalG.marks; G3.o.labels = globalG.o.labels;
    if (G3.dataKey !== globalGen) {
      const first = !G3.nodes.length;
      // the flat graph the user was looking at: it inflates into depth instead of starting over
      const flat = inflate && globalG.dataKey === globalGen && globalG.nodes.length ? new Map(globalG.nodes.map((n) => [n.id, { x: n.x, y: n.y }])) : null;
      G3.setData(globalData.nodes, globalData.edges, { init: flat });
      G3.dataKey = globalGen;
      if (first) {
        if (flat) { G3.cam.yaw = 0; G3.cam.pitch = 0; }
        G3.fit(0);
        G3.flyTo({ yaw: 0.55, pitch: 0.32 }, flat ? 1800 : 1200);
        G3.spin = true;
      }
      if (G3.selected && !G3.region) hudNode(G3.selected);
    }
    if (store.get("vn.hint3d", "0") !== "1") { store.set("vn.hint3d", "1"); toast(t("hint3d")); }
  }
  function setMode(on) {
    if (on && !G3) return;
    mode3d = on; store.set("vn.graph3d", on ? "1" : "0");
    graphChrome();
    if (on) enter3d(true);
    else {
      stopTour(); if (G3) G3.setActive(false);
      ensure2d(); globalG.resize(); globalG.autoFit = true; globalG.fit();
    }
  }
  function focusCurrent() { // opened from a note: fly to its file
    if (!G3 || !lastNote || lastNote.shown) return;
    lastNote.shown = true;
    const id = fileNodeId(lastNote);
    if (id) select3d(G3.byId.get(id));
  }
  function fileNodeId(x) { // a note of the page -> its file, as the graph view shows files
    if (!G3 || !x) return null;
    if (G3.byId.has(x.id)) return x.id;
    if (!x.file) return null;
    const hit = G3.nodes.find((n) => n.file === x.file);
    return hit ? hit.id : null;
  }
  function groupName(key) {
    const names = new Map(((globalData && globalData.groups) || []).map((x) => [String(x.id), x.name]));
    return key === "data" ? t("dataFiles") : key.startsWith("@") ? key.slice(1) || "/" : names.get(key) || key;
  }
  function regions() { // the parts of the project as the graph is coloured: folders, or communities
    const src = G3 && G3.nodes.length ? G3.nodes : (globalData && globalData.nodes) || [], map = new Map();
    for (const n of src) {
      const k = String(n.group);
      if (globalG.hidden.has(k)) continue;
      if (!map.has(k)) map.set(k, { key: k, ids: [], name: groupName(k), color: groupColor(n.group) });
      map.get(k).ids.push(n.id);
    }
    return [...map.values()].sort((a, b) => b.ids.length - a.ids.length || (a.name < b.name ? -1 : 1));
  }

  // the panel beside the 3D graph: what is in view, in words, with numbered places to fly to
  const hudBtn = (label, key, run, cls) => el("button", { class: cls || null, title: key ? `${label} (${key})` : label, onclick: run },
    label, key ? " " : null, key ? el("kbd", { text: key }) : null);
  function hudRow(i, n, extra) {
    return el("li", {}, i < 9 ? el("kbd", { text: String(i + 1) }) : el("span", { class: "muted small", text: "·" }),
      el("a", { href: "#", title: n.file || n.title, onclick: (ev) => { ev.preventDefault(); stopTour(); select3d(n); } }, n.title),
      extra ? el("span", { class: "muted small", text: extra }) : null);
  }
  function trailView() {
    const last = trail.slice(-6).map((id) => G3.byId.get(id)).filter(Boolean);
    if (!last.length) return null;
    return el("div", { class: "hud-trail muted small" }, el("kbd", { text: "⌫" }), " ",
      ...last.map((n) => el("a", { href: "#", onclick: (ev) => { ev.preventDefault(); back3d(n.id); } }, n.title)));
  }
  const stepsAway = (d) => (d <= 1 ? t("step1") : fill(t("stepN"), { n: d }));
  function showHud(...kids) { hud.replaceChildren(...kids.filter(Boolean)); hud.hidden = false; }
  function swatch(color) { const s = el("span", { class: "sw" }); s.style.background = color; return s; }

  function select3d(n, { fly = true, push = true, keepWalk = false } = {}) {
    if (!G3 || !n) return;
    const node = G3.byId.get(n.id) || G3.byId.get(fileNodeId(n));
    if (!node) return;
    if (push && G3.selected && G3.selected !== node) { trail.push(G3.selected.id); if (trail.length > 40) trail.shift(); }
    if (!keepWalk) walk = null;
    regionKey = null; G3.selected = node; G3.region = null; G3.path = null;
    if (G3.follow) G3.follow = node;
    else G3.spin = false;
    if (fly) G3.flyToNode(node.id);
    hudNode(node);
    G3.dirty = true; G3.kick();
  }
  function clear3d() {
    walk = null; regionKey = null;
    if (!G3) return;
    G3.selected = null; G3.follow = null; G3.region = null; G3.path = null; G3.dirty = true; G3.kick();
    hud.hidden = true; hudTargets = [];
  }
  function back3d(id) {
    if (!G3 || !trail.length) return;
    let to = trail.pop();
    while (id && trail.length && to !== id) to = trail.pop();
    const n = G3.byId.get(to);
    if (n) select3d(n, { push: false });
  }
  function neighbours(n) {
    return [...G3.adj.get(n.id)].map((id) => G3.byId.get(id)).filter((m) => m && G3.visible(m))
      .sort((a, b) => b.deg - a.deg || (a.title < b.title ? -1 : 1));
  }
  function hudNode(n) {
    const nb = neighbours(n);
    let ins = 0, outs = 0;
    for (const l of G3.links) { if (l.t === n) ins++; else if (l.s === n) outs++; }
    hudTargets = nb.slice(0, 9).map((m) => m.id);
    const g = String(n.group);
    const say = n.kind === "data" ? t("say.data")
      : n.kind === "doc" ? (ins && outs ? t("say.docBoth") : outs ? t("say.docOut") : ins ? t("say.docIn") : t("say.alone")) // a document mentions, it does not use
      : ins && outs ? t("say.both") : ins ? t("say.used") : outs ? t("say.uses") : t("say.alone");
    showHud(
      el("div", { class: "hud-head" }, kindBadge(n.kind), el("strong", { text: n.title })),
      el("div", { class: "hud-sub muted small mono", text: n.file || "" }),
      el("div", { class: "hud-chips" },
        el("a", { href: "#", class: "chip", title: t("hudRegionTitle"), onclick: (ev) => { ev.preventDefault(); focusRegion(g); } },
          swatch(groupColor(n.group)), groupName(g)),
        el("span", { class: "chip", title: t("hudInOut") }, `← ${ins} · ${outs} →`),
        G3.follow === n ? el("span", { class: "chip", text: t("following") }) : null),
      el("div", { class: "hud-say", text: fill(say, { name: n.title, ins, outs }) }),
      walk ? el("div", { class: "muted small", text: fill(t("walkAt"), { i: walk.i + 1, n: walk.list.length, name: (G3.byId.get(walk.from) || {}).title || "" }) }) : null,
      el("div", { class: "hud-bar" },
        hudBtn(t("hudOpen"), "↵", () => { location.hash = noteHref(n.id); }, "primary"),
        hudBtn(G3.follow === n ? t("hudUnfollow") : t("hudFollow"), "F", toggleFollow),
        hudBtn(isPinned("n:" + n.id) ? t("hudUnpin") : t("hudPin"), "P", () => togglePin()),
        hudBtn(t("impact"), "I", () => impact3d(n)),
        nb.length ? hudBtn(t("hudWalk"), "N", () => walkStep(1)) : null),
      nb.length ? el("div", { class: "sec small", text: `${t("hudLinked")} · ${nb.length}` }) : null,
      nb.length ? el("ol", { class: "hud-list" }, nb.slice(0, 9).map((m, i) => hudRow(i, m, String(m.deg)))) : null,
      nb.length > 9 ? el("div", { class: "muted small", text: `+${nb.length - 9} ${t("more")} · ${t("hudWalkMore")}` }) : null,
      trailView());
  }
  function toggleFollow() {
    if (!G3 || !G3.selected) return;
    G3.follow = G3.follow === G3.selected ? null : G3.selected;
    G3.spin = !!G3.follow; // following: the camera circles it slowly
    if (G3.follow) G3.flyToNode(G3.follow.id);
    hudNode(G3.selected); G3.kick();
  }
  function walkStep(d) { // round a file: its linked files one by one, the file itself kept on the trail
    if (!G3 || !G3.selected) return;
    let first = false;
    if (!walk) {
      const list = neighbours(G3.selected).map((m) => m.id);
      if (!list.length) return;
      walk = { from: G3.selected.id, list, i: d > 0 ? -1 : 0 };
      first = true;
    }
    walk.i = (walk.i + d + walk.list.length) % walk.list.length;
    select3d(G3.byId.get(walk.list[walk.i]), { keepWalk: true, push: first });
  }

  function focusRegion(key) {
    if (!G3) return;
    const r = regions().find((x) => x.key === key);
    if (!r) return;
    walk = null; regionKey = key;
    G3.selected = null; G3.follow = null; G3.path = null; G3.region = new Set(r.ids); G3.spin = true;
    G3.fit(1100, r.ids);
    hudRegion(r);
  }
  function hudRegion(r) {
    const inside = new Set(r.ids), talk = new Map();
    let links = 0;
    for (const l of G3.links) {
      const a = inside.has(l.s.id), b = inside.has(l.t.id);
      if (a && b) links++;
      else if (a || b) { const k = String((a ? l.t : l.s).group); talk.set(k, (talk.get(k) || 0) + 1); }
    }
    const top = r.ids.map((id) => G3.byId.get(id)).filter(Boolean).sort((a, b) => b.deg - a.deg).slice(0, 9);
    hudTargets = top.map((n) => n.id);
    const partners = [...talk.entries()].sort((a, b) => b[1] - a[1]).slice(0, 3);
    const say = fill(t("say.region"), { name: r.name, n: r.ids.length, links }) + " " +
      (partners.length ? fill(t("say.talks"), { list: partners.map(([k, c]) => `${groupName(k)} (${c})`).join(", ") }) : t("say.island"));
    const bar = el("div", { class: "hud-progress" }, el("div"));
    if (tour) bar.firstChild.style.width = `${Math.round(((tour.i + 1) / tour.rs.length) * 100)}%`;
    showHud(
      el("div", { class: "hud-head" }, swatch(r.color), el("strong", { text: r.name })),
      tour ? el("div", { class: "muted small", text: fill(t("tourAt"), { i: tour.i + 1, n: tour.rs.length }) }) : null,
      tour ? bar : null,
      el("div", { class: "hud-say", text: say }),
      el("div", { class: "hud-bar" },
        hudBtn(t("prevRegion"), "[", () => regionStep(-1)), hudBtn(t("nextRegion"), "]", () => regionStep(1)),
        tour ? hudBtn(tour.paused ? t("tourResume") : t("tourPause"), t("spaceK"), pauseTour) : hudBtn(t("tour"), "T", startTour),
        hudBtn(isPinned("r:" + r.key) ? t("hudUnpin") : t("hudPin"), "P", () => togglePin()),
        hudBtn(t("hudAll"), "R", () => graphKey({ key: "r" }))),
      el("div", { class: "sec small", text: t("hudTop") }),
      el("ol", { class: "hud-list" }, top.map((n, i) => hudRow(i, n, String(n.deg)))));
  }
  function regionStep(d) {
    if (!G3) return;
    if (tour) { nextStop(d); return; }
    const rs = regions();
    if (!rs.length) return;
    const cur = rs.findIndex((r) => r.key === regionKey);
    const i = cur < 0 ? (d > 0 ? 0 : rs.length - 1) : (cur + d + rs.length) % rs.length;
    focusRegion(rs[i].key);
  }

  // a tour: the largest parts of the project one after another, each said in a sentence
  const TOUR_MS = 7000;
  const SIDE_PART = /^(tests?|.*_tests?|tests?_.*|fixtures?|examples?|samples?|benchmarks?|bench|docs?|vendor|third_party|scripts?)$/i;
  function tourStops() { // what a newcomer should see first: the product's own code, its busiest parts first
    const side = (r) => r.key === "data" || r.name.split("/").some((p) => SIDE_PART.test(p));
    const weight = (r) => r.ids.reduce((s, id) => s + ((G3.byId.get(id) || {}).deg || 0), 0);
    return regions().filter((r) => weight(r) > 0).sort((a, b) => side(a) - side(b) || weight(b) - weight(a)).slice(0, 10);
  }
  function startTour() {
    if (!G3) return;
    if (!mode3d) setMode(true);
    const rs = tourStops();
    if (!rs.length) return;
    tour = { rs, i: -1, timer: null, paused: false };
    $("#graph-tour").classList.add("on");
    nextStop(1);
  }
  function nextStop(d = 1) {
    if (!tour) return;
    clearTimeout(tour.timer);
    tour.i = Math.max(0, tour.i + d);
    if (tour.i >= tour.rs.length) { stopTour(true); return; }
    focusRegion(tour.rs[tour.i].key);
    if (!tour.paused) tour.timer = setTimeout(() => nextStop(1), TOUR_MS);
  }
  function pauseTour() {
    if (!tour) return;
    tour.paused = !tour.paused;
    clearTimeout(tour.timer);
    if (!tour.paused) tour.timer = setTimeout(() => nextStop(1), TOUR_MS);
    const r = tour.rs[tour.i];
    if (r) hudRegion(r);
  }
  function stopTour(done) {
    if (!tour) return;
    clearTimeout(tour.timer); tour = null;
    $("#graph-tour").classList.remove("on");
    if (done) { clear3d(); G3.fit(1400); G3.spin = true; toast(t("tourDone")); }
    else if (regionKey) { const r = regions().find((x) => x.key === regionKey); if (r) hudRegion(r); }
  }

  // impact and paths in 3D: the files concerned lit, the rest dimmed, the list in the panel
  async function impact3d(x) {
    if (!G3) return;
    let r;
    try { r = await api(`/api/impact?id=${encodeURIComponent(x.id)}&depth=3&tests=1`); } catch (e) { toast(e.message); return; }
    const start = fileNodeId(x), depth = new Map();
    if (start) depth.set(start, 0);
    for (const it of r.items) { const id = fileNodeId(it); if (id && !depth.has(id)) depth.set(id, it.depth); }
    const ids = [...depth.keys()];
    walk = null; regionKey = null; G3.path = null; G3.follow = null;
    G3.selected = start ? G3.byId.get(start) : null; G3.region = new Set(ids);
    G3.fit(1100, ids);
    const rows = ids.filter((id) => id !== start).map((id) => G3.byId.get(id)).filter(Boolean)
      .sort((a, b) => depth.get(a.id) - depth.get(b.id) || b.deg - a.deg);
    hudTargets = rows.slice(0, 9).map((n) => n.id);
    showHud(
      el("div", { class: "hud-head" }, el("strong", { text: `${t("impact")} · ${x.title}` })),
      el("div", { class: "hud-say", text: rows.length ? fill(t("say.impact"), { name: x.title, n: r.count, files: rows.length }) : t("impactNone") }),
      el("ol", { class: "hud-list" }, rows.slice(0, 30).map((n, i) => hudRow(i, n, stepsAway(depth.get(n.id))))),
      rows.length > 30 ? el("div", { class: "muted small", text: `+${rows.length - 30} ${t("more")}` }) : null,
      el("div", { class: "hud-bar" }, hudBtn(t("hudOpen"), null, () => { location.hash = noteHref(x.id); }),
        hudBtn(t("hudAll"), "R", () => graphKey({ key: "r" }))));
    G3.dirty = true; G3.kick();
  }
  async function path3d(a, b) {
    if (!G3) return;
    let r;
    try { r = await api(`/api/path?from=${encodeURIComponent(a.id)}&to=${encodeURIComponent(b.id)}`); } catch (e) { toast(e.message); return; }
    if (!r.found) { toast(t("pathNone")); return; }
    const chain = [];
    for (const s of r.steps) { const id = fileNodeId(s); if (id && chain[chain.length - 1] !== id) chain.push(id); }
    walk = null; regionKey = null; G3.follow = null; G3.selected = null;
    G3.path = chain; G3.region = new Set(chain); G3.fit(1100, chain);
    hudTargets = r.steps.slice(0, 9).map((s) => fileNodeId(s));
    showHud(
      el("div", { class: "hud-head" }, el("strong", { text: `${t("pathTitle")}: ${a.title} → ${b.title}` })),
      r.direction === "backward" ? el("div", { class: "muted small", text: t("pathBack") }) : null,
      el("ol", { class: "hud-list" }, r.steps.map((s, i) => el("li", {}, i < 9 ? el("kbd", { text: String(i + 1) }) : null,
        el("a", { href: noteHref(s.id), title: s.file || "" }, s.title), i ? el("span", { class: "muted small", text: s.relation }) : null))),
      el("div", { class: "hud-bar" }, hudBtn(t("hudAll"), "R", () => graphKey({ key: "r" }))));
    G3.dirty = true; G3.kick();
  }

  // keys of the graph view (the page's own keys are at the end)
  function graphKey(ev) {
    const k = ev.key;
    if (k === "v" || k === "V") { if (!G3) return false; setMode(!mode3d); return true; }
    if (k === "r" || k === "R") {
      if (mode3d && G3) { stopTour(); clear3d(); G3.fit(900); } else { globalG.autoFit = true; globalG.fit(); }
      return true;
    }
    if (k === "t" || k === "T") { if (!G3) return false; if (tour) stopTour(); else startTour(); return true; }
    if (k === "c") { const box = $("#changes-box"); if (box.hidden) return false; const b = $("#global-changes"); b.checked = !b.checked; showChanges(b.checked); return true; }
    if (k === "l") { const b = $("#global-labels"); b.checked = !b.checked; b.dispatchEvent(new Event("change")); return true; }
    if (!mode3d || !G3) return false;
    const sel = G3.selected;
    switch (k) {
      case "ArrowLeft": case "a": G3.spin = false; G3.orbit(0.14, 0); return true;
      case "ArrowRight": case "d": G3.spin = false; G3.orbit(-0.14, 0); return true;
      case "ArrowUp": case "w": G3.orbit(0, 0.1); return true;
      case "ArrowDown": case "s": G3.orbit(0, -0.1); return true;
      case "+": case "=": G3.zoom(0.8); return true;
      case "-": case "_": G3.zoom(1.25); return true;
      case " ": if (tour) pauseTour(); else { G3.spin = !G3.spin; G3.kick(); } return true;
      case "f": case "F": toggleFollow(); return true;
      case "p": case "P": togglePin(); return true;
      case "n": walkStep(1); return true;
      case "N": walkStep(-1); return true;
      case "Enter": if (sel) location.hash = noteHref(sel.id); return !!sel;
      case "Backspace": back3d(); return true;
      case "[": regionStep(-1); return true;
      case "]": regionStep(1); return true;
      case "i": case "I": if (sel) impact3d(sel); return !!sel;
      default:
        if (/^[1-9]$/.test(k) && hudTargets[Number(k) - 1]) { stopTour(); select3d(G3.byId.get(hudTargets[Number(k) - 1])); return true; }
    }
    return false;
  }
  $("#graph-3d").addEventListener("click", () => setMode(!mode3d));
  $("#graph-tour").addEventListener("click", () => { if (tour) stopTour(); else startTour(); });
  $("#graph-keys").addEventListener("click", () => showKeys());

  // -- a watch list: files and regions to keep an eye on; the page says when one of them changed ---------
  const WATCH_KEY = "vn.watched";
  let watched = (() => { try { return JSON.parse(store.get(WATCH_KEY, "[]")) || []; } catch (_) { return []; } })();
  const isPinned = (key) => watched.some((w) => w.key === key);
  let changedWatched = new Set(), announced = new Set();
  function saveWatched() { store.set(WATCH_KEY, JSON.stringify(watched.slice(-20))); renderWatched(); }
  function togglePin() {
    if (!G3) return;
    let item = null;
    if (G3.selected) item = { key: "n:" + G3.selected.id, id: G3.selected.id, title: G3.selected.title, file: G3.selected.file };
    else if (regionKey) item = { key: "r:" + regionKey, region: regionKey, title: groupName(regionKey) };
    if (!item) return;
    watched = isPinned(item.key) ? watched.filter((w) => w.key !== item.key) : [...watched, item];
    saveWatched();
    if (G3.selected) hudNode(G3.selected); else if (regionKey) { const r = regions().find((x) => x.key === regionKey); if (r) hudRegion(r); }
    checkWatched();
  }
  // a region is a folder ("@path") or a community, whichever way the graph was coloured when it was watched
  const inRegion = (n, key) => key === "data" ? n.kind === "data"
    : n.kind !== "data" && (key.startsWith("@") ? "@" + n.area === key : String(n.community) === key);
  function watchedFiles(w) { // the files a watched item stands for
    if (w.file) return [w.file];
    const src = (G3 && G3.nodes.length ? G3.nodes : (globalData && globalData.nodes) || []);
    return w.region ? src.filter((n) => inRegion(n, w.region)).map((n) => n.file).filter(Boolean) : [];
  }
  function openWatched(w) {
    stopTour();
    if (w.id) { select3d({ id: w.id, file: w.file }); return; }
    const mode = w.region === "data" ? colorBy : w.region.startsWith("@") ? "folder" : "community";
    if (mode !== colorBy) { const s = $("#graph-color"); s.value = mode; s.dispatchEvent(new Event("change")); }
    focusRegion(w.region);
  }
  function renderWatched() {
    const box = $("#watched");
    if (!G3) { box.hidden = true; return; }
    G3.pinned = new Set(watched.filter((w) => w.id).map((w) => w.id));
    box.replaceChildren(...watched.map((w) => el("span", {
      class: "chip" + (changedWatched.has(w.key) ? " changed" : ""), title: w.file || w.title,
      onclick: () => openWatched(w),
    }, (w.region ? "◎ " : "◆ ") + w.title)));
    box.hidden = !watched.length || !mode3d || $("#graphview").hidden;
    if (G3) { G3.dirty = true; G3.kick(); }
  }
  async function checkWatched() {
    if (OFFLINE || !watched.length || document.hidden) return;
    let r;
    try { r = await api("/api/changes"); } catch (_) { return; }
    const touched = new Set([...(r.edited || []), ...(r.deleted || [])].map((c) => c.file));
    changedWatched = new Set(watched.filter((w) => watchedFiles(w).some((f) => touched.has(f))).map((w) => w.key));
    const fresh = watched.filter((w) => changedWatched.has(w.key) && !announced.has(w.key));
    for (const w of fresh) announced.add(w.key);
    for (const k of [...announced]) if (!changedWatched.has(k)) announced.delete(k); // said again if it changes again
    if (fresh.length) toast(fill(t("watchedChanged"), { list: fresh.map((w) => w.title).join(", ") }));
    renderWatched();
  }
  if (!OFFLINE) setInterval(checkWatched, 15000);

  // -- the command bar (Ctrl+K): say what to do, in Turkish or English ---------------------------------
  const pal = { box: $("#palette"), input: $("#pal-input"), list: $("#pal-list"), items: [], active: 0, seq: 0, timer: null };
  const fold = (s) => String(s).toLowerCase().replace(/ı/g, "i").replace(/i̇/g, "i").replace(/ş/g, "s").replace(/ğ/g, "g")
    .replace(/ü/g, "u").replace(/ö/g, "o").replace(/ç/g, "c");
  const VERBS = [
    { verb: "path", re: /^(?:path|route|yol)\s+(.+?)\s*(?:\s(?:to|ile|and|ve)\s|->|→|>)\s*(.+)$/i },
    { verb: "path", re: /^(?:path|route|yol)\s+(\S+)\s+(\S+)$/i },
    { verb: "focus", re: /^(?:focus|fly|go|find|show|odak|odaklan|uç|git|bul|göster)\s+(.+)$/i },
    { verb: "open", re: /^(?:open|read|aç|oku)\s+(.+)$/i },
    { verb: "impact", re: /^(?:impact|affects?|etki|etkisi|etkiler)\s+(.+)$/i },
    { verb: "region", re: /^(?:region|area|bölge|alan)\s+(.+)$/i },
    { verb: "ask", re: /^(?:ask|sor)\s+(.+)$/i },
  ];
  function openPalette(text = "") {
    pal.box.hidden = false; pal.input.value = text; pal.active = 0;
    runPalette(); pal.input.focus();
  }
  function closePalette() { pal.box.hidden = true; pal.seq++; clearTimeout(pal.timer); pal.timer = null; }
  async function goGraph() {
    if ($("#graphview").hidden) location.hash = "#/graph";
    await until(() => !$("#graphview").hidden && globalData && (mode3d ? G3.dataKey === globalGen : globalG.dataKey === globalGen));
  }
  async function goGraph3d() {
    if (!G3) return;
    if ($("#graphview").hidden) { mode3d = true; store.set("vn.graph3d", "1"); }
    await goGraph();
    if (!mode3d) setMode(true);
    await until(() => G3.dataKey === globalGen);
  }
  const goNote = (x) => { location.hash = noteHref(x.id); };
  function flyTo(x) {
    if (!G3) { goNote(x); return; }
    goGraph3d().then(() => { const id = fileNodeId(x); if (id) { stopTour(); select3d(G3.byId.get(id)); } else goNote(x); });
  }
  async function searchNotes(q) {
    try { return (await api("/api/search?q=" + encodeURIComponent(q))).results || []; } catch (_) { return []; }
  }
  function commands() {
    const cs = [
      { words: "graph graf view görünüm harita map", label: t("cmd.graph"), key: "G", run: () => { location.hash = "#/graph"; } },
      G3 ? { words: "3d 2d view görünüm mode boyut üç düz", label: mode3d ? t("cmd.to2d") : t("cmd.to3d"), key: "V",
        run: () => { if ($("#graphview").hidden) { mode3d = !mode3d; store.set("vn.graph3d", mode3d ? "1" : "0"); location.hash = "#/graph"; } else setMode(!mode3d); } } : null,
      G3 ? { words: "tour tur gezinti gez bölgeler regions overview özet", label: t("cmd.tour"), key: "T", run: () => goGraph3d().then(() => { if (!tour) startTour(); }) } : null,
      OFFLINE ? null : { words: "changed changes değişen değişenler what ne diff", label: t("cmd.changes"), key: "C",
        run: () => goGraph().then(() => { const b = $("#global-changes"); b.checked = true; showChanges(true); }) },
      { words: "fit all hepsi hepsini sığdır reset everything", label: t("cmd.fit"), key: "R", run: () => goGraph().then(() => graphKey({ key: "r" })) },
      { words: "home start başlangıç ana sayfa", label: t("cmd.home"), run: () => { location.hash = "#/"; } },
      { words: "notes notlarım my notes kendi", label: t("myNotes"), run: () => { location.hash = "#/"; } },
      { words: "theme tema dark light koyu açık", label: t("theme"), run: () => $("#btn-theme").click() },
      { words: "language dil türkçe english ingilizce", label: t("cmd.lang"), run: () => $("#btn-lang").click() },
      { words: "help keys shortcuts kısayol kısayollar yardım tuşlar", label: t("keysTitle"), key: "?", run: () => showKeys() },
    ];
    return cs.filter(Boolean);
  }
  function matchCommands(q) {
    const cs = commands();
    if (!q) return cs;
    const words = fold(q).split(/\s+/).filter(Boolean);
    return cs.filter((c) => {
      const hay = fold(`${c.words} ${c.label}`).split(/[\s/·,:()]+/);
      return words.every((w) => hay.some((h) => h.startsWith(w)));
    });
  }
  function regionItems(q) {
    const f = fold(q);
    return regions().filter((r) => fold(r.name).includes(f)).slice(0, 6).map((r) => ({
      sec: t("pal.regions"), icon: swatch(r.color), label: `${t("hudRegionTitle")}: ${r.name}`, hint: `${r.ids.length} ${t("filesN")}`,
      run: () => goGraph3d().then(() => { stopTour(); focusRegion(r.key); }) }));
  }
  async function verbItems(verb, m) {
    if (verb === "ask") return [{ icon: "?", label: `${t("ask")}: «${m[1]}»`, run: () => { location.hash = "#/q/" + encodeURIComponent(m[1]); } }];
    if (verb === "region") return regionItems(m[1]);
    if (verb === "path") {
      const [a, b] = await Promise.all([searchNotes(m[1]), searchNotes(m[2])]);
      if (!a.length || !b.length) return [{ label: t("palNoMatch"), run: () => openPalette(pal.input.value) }];
      return [{ icon: "⇢", label: `${t("pathTitle")}: ${a[0].title} → ${b[0].title}`, hint: `${a[0].file || ""} → ${b[0].file || ""}`,
        run: () => (G3 ? goGraph3d().then(() => path3d(a[0], b[0])) : goNote(a[0])) }];
    }
    const hits = (await searchNotes(m[1])).slice(0, 8);
    const word = verb === "open" ? t("verb.open") : verb === "impact" ? t("impact") : t("verb.focus");
    return hits.map((x) => ({ icon: kindBadge(x.kind), label: `${word}: ${x.title}`, hint: x.file || "",
      run: () => (verb === "open" ? goNote(x) : verb === "impact" ? (G3 ? goGraph3d().then(() => impact3d(x)) : goNote(x)) : flyTo(x)) }));
  }
  async function runPalette() {
    const q = pal.input.value.trim(), my = ++pal.seq;
    let items = [];
    const hit = VERBS.map((v) => [v, q.match(v.re)]).find(([, m]) => m);
    if (hit) items = await verbItems(hit[0].verb, hit[1]);
    else if (!q) {
      items = matchCommands("").map((c) => ({ sec: t("pal.commands"), icon: "›", label: c.label, key: c.key, run: c.run }));
      const ex = lang === "tr" ? ["odak ", "bölge ", "etki ", "yol ", "aç ", "sor "] : ["focus ", "region ", "impact ", "path ", "open ", "ask "];
      const why = ["focus", "region", "impact", "path", "open", "ask"];
      items.push(...ex.map((w, i) => ({ sec: t("pal.try"), icon: "›", label: `${w.trim()} …`, hint: t("ex." + why[i]), complete: w })));
    } else {
      items = matchCommands(q).map((c) => ({ sec: t("pal.commands"), icon: "›", label: c.label, key: c.key, run: c.run }));
      if (!OFFLINE && isQuestion(q)) items.unshift({ sec: t("pal.ask"), icon: "?", label: `${t("ask")}: «${q}»`, run: () => { location.hash = "#/q/" + encodeURIComponent(q); } });
      items.push(...regionItems(q));
      const inGraph = !$("#graphview").hidden && mode3d;
      items.push(...(await searchNotes(q)).slice(0, 8).map((x) => ({ sec: t("pal.notes"), icon: kindBadge(x.kind), label: x.title, hint: x.file || "",
        run: () => (inGraph ? flyTo(x) : goNote(x)) })));
    }
    if (my !== pal.seq) return;
    pal.items = items; pal.active = 0; paintPalette();
  }
  function paintPalette() {
    let sec = null;
    const kids = [];
    pal.items.forEach((it, i) => {
      if (it.sec && it.sec !== sec) { sec = it.sec; kids.push(el("div", { class: "pal-sec", text: sec })); }
      kids.push(el("div", { class: "pal-it" + (i === pal.active ? " active" : ""), role: "option", onclick: () => runItem(it),
        onmousemove: () => markActive(i) },
      typeof it.icon === "string" ? el("span", { class: "muted", text: it.icon }) : it.icon || null,
      el("span", { class: "l", text: it.label }), it.hint ? el("span", { class: "h", text: it.hint }) : null,
      it.key ? el("kbd", { text: it.key }) : null));
    });
    if (!pal.items.length) kids.push(el("div", { class: "pal-sec", text: t("noResults") }));
    pal.list.replaceChildren(...kids);
  }
  function markActive(i) {
    pal.active = i;
    const its = [...pal.list.querySelectorAll(".pal-it")];
    its.forEach((e, j) => e.classList.toggle("active", j === i));
    if (its[i]) its[i].scrollIntoView({ block: "nearest" });
  }
  function runItem(it) {
    if (!it) return;
    if (it.complete) { pal.input.value = it.complete; pal.input.focus(); runPalette(); return; }
    closePalette(); it.run();
  }
  pal.box.addEventListener("mousedown", (ev) => { if (ev.target === pal.box) closePalette(); });
  pal.input.addEventListener("input", () => { clearTimeout(pal.timer); pal.timer = setTimeout(() => { pal.timer = null; runPalette(); }, 110); });
  pal.input.addEventListener("keydown", async (ev) => {
    if (ev.key === "Escape") { ev.preventDefault(); closePalette(); }
    else if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      if (pal.items.length) markActive((pal.active + (ev.key === "ArrowDown" ? 1 : -1) + pal.items.length) % pal.items.length);
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      if (pal.timer) { clearTimeout(pal.timer); pal.timer = null; await runPalette(); } // what is on screen is for an older text
      runItem(pal.items[pal.active]);
    } else if (ev.key === "Tab") {
      ev.preventDefault();
      const it = pal.items[pal.active];
      if (it && it.complete) runItem(it);
    }
  });

  // -- every shortcut on one sheet (?) -----------------------------------------------------------------
  function showKeys() {
    const H = "h", rows = [
      [H, t("keys.general")], ["Ctrl K", t("keys.palette")], ["/", t("keys.search")], ["G", t("keys.graph")], ["Esc", t("keys.esc")], ["?", t("keys.help")],
      [H, t("keys.graphH")], ["V", t("keys.view")], [t("keys.dragK"), t("keys.drag")], [t("keys.rdragK"), t("keys.rdrag")],
      [t("keys.wheelK"), t("keys.wheel")], ["← → ↑ ↓", t("keys.orbit")], ["+ −", t("keys.zoom")], [t("keys.clickK"), t("keys.click")],
      [`↵ · ${t("keys.dblK")}`, t("keys.open")], ["1 – 9", t("keys.num")], ["N · Shift N", t("keys.walk")], ["⌫", t("keys.back")],
      ["F", t("keys.follow")], ["P", t("keysPin")], [t("spaceK"), t("keys.spin")], ["[ ]", t("keys.regions")], ["T", t("keys.tour")], ["I", t("keys.impact")],
      ["C", t("keys.changes")], ["L", t("keys.labels")], ["R", t("keys.reset")],
    ];
    const box = el("div", { class: "keys-box", role: "dialog" },
      el("div", { class: "hud-head" }, el("strong", { text: t("keysTitle") }), el("span", { class: "spacer" }),
        el("button", { class: "linkish", onclick: () => { $("#keys").hidden = true; } }, t("close2"))),
      el("div", { class: "keys-cols" }, rows.map(([k, v]) => (k === H ? el("h3", { text: v })
        : el("div", { class: "keys-row" }, el("span", { text: v }), el("kbd", { text: k }))))));
    $("#keys").replaceChildren(box);
    $("#keys").hidden = false;
  }
  $("#keys").addEventListener("mousedown", (ev) => { if (ev.target.id === "keys") $("#keys").hidden = true; });

  // -- the API of an exported file, answered from its data ---------------------------------------
  function notFound(message) { const e = new Error(message); e.status = 404; e.code = "not_found"; return e; }
  function offlineApi(path) {
    const q = path.indexOf("?"), route = q < 0 ? path : path.slice(0, q);
    const p = new URLSearchParams(q < 0 ? "" : path.slice(q + 1)), D = OFFLINE;
    const flag = (k) => p.get(k) !== "0";
    switch (route) {
      case "/api/stats": return D.stats;
      case "/api/tree": return D.tree;
      case "/api/note": {
        const id = p.get("id") || "";
        if (!Object.prototype.hasOwnProperty.call(D.notes, id)) throw notFound(t("offlineMissing"));
        return D.notes[id];
      }
      case "/api/search": return { results: offlineSearch(p.get("q") || "") };
      case "/api/global": return offlineGlobal(flag("tests"), flag("data"));
      case "/api/impact": return offlineImpact(p.get("id") || "", flag("tests"));
      case "/api/path": return offlinePath(p.get("from") || "", p.get("to") || "");
      case "/api/local": return offlineLocal(p.get("id") || "", Math.min(3, Math.max(1, Number(p.get("depth")) || 1)), flag("tests"), flag("data"));
      default: throw notFound(route);
    }
  }
  const keepNode = (tests, data) => (n) => (tests || !n.test) && (data || n.kind !== "data");
  function offlineGlobal(tests, data) {
    // as the server does: the filters first, then the best connected files up to the cap
    const G = OFFLINE.global, cap = G.cap || 2500;
    const all = G.nodes.filter(keepNode(tests, data)), inAll = new Set(all.map((n) => n.id));
    const edges = G.edges.filter((e) => inAll.has(e.source) && inAll.has(e.target));
    const degree = new Map();
    for (const e of edges) for (const x of [e.source, e.target]) degree.set(x, (degree.get(x) || 0) + (e.weight || 1));
    const best = all.slice().sort((a, b) => (degree.get(b.id) || 0) - (degree.get(a.id) || 0) || (a.file < b.file ? -1 : a.file > b.file ? 1 : 0)).slice(0, cap);
    const kept = new Set(best.map((n) => n.id));
    const nodes = best.sort((a, b) => (a.file < b.file ? -1 : a.file > b.file ? 1 : 0))
      .map((n) => Object.assign({}, n, { degree: degree.get(n.id) || 0 }));
    return { nodes, edges: edges.filter((e) => kept.has(e.source) && kept.has(e.target)), hidden_files: all.length - nodes.length, groups: G.groups };
  }
  let offNodes = null, offAdj = null;
  function offlineLocal(id, depth, tests, data) {
    const G = OFFLINE.global;
    if (!offAdj) {
      offNodes = new Map(G.nodes.map((n) => [n.id, n]));
      offAdj = new Map(G.nodes.map((n) => [n.id, []]));
      for (const e of G.edges) if (offAdj.has(e.source) && offAdj.has(e.target)) { offAdj.get(e.source).push(e.target); offAdj.get(e.target).push(e.source); }
    }
    if (!offNodes.has(id)) { // a note that is not in the file-level graph: itself only
      const n = Object.prototype.hasOwnProperty.call(OFFLINE.notes, id) ? OFFLINE.notes[id] : null;
      return { center: id, nodes: n ? [{ id, title: n.title, kind: n.kind, file: n.file, group: "other" }] : [], edges: [] };
    }
    const keep = keepNode(tests, data), dist = new Map([[id, 0]]);
    let frontier = [id];
    for (let d = 1; d <= depth && frontier.length && dist.size < 220; d++) {
      const next = [];
      for (const u of frontier) for (const v of offAdj.get(u)) {
        if (dist.size >= 220) break;
        if (dist.has(v) || !keep(offNodes.get(v))) continue;
        dist.set(v, d); next.push(v);
      }
      frontier = next;
    }
    return { center: id, nodes: [...dist.keys()].map((k) => Object.assign({}, offNodes.get(k), { depth: dist.get(k) })),
      edges: G.edges.filter((e) => dist.has(e.source) && dist.has(e.target)) };
  }
  function offlineGraph() { // file -> the files it uses, and the files that use it
    if (!offlineGraph.out) {
      const out = new Map(), inn = new Map(), byId = new Map(OFFLINE.global.nodes.map((n) => [n.id, n]));
      for (const e of OFFLINE.global.edges) {
        if (!out.has(e.source)) out.set(e.source, []);
        if (!inn.has(e.target)) inn.set(e.target, []);
        out.get(e.source).push(e); inn.get(e.target).push(e);
      }
      Object.assign(offlineGraph, { out, inn, byId });
    }
    return offlineGraph;
  }
  function offlineImpact(id, tests) { // at file level: an exported file has no symbol graph
    const G = offlineGraph();
    if (!G.byId.has(id)) throw notFound(t("offlineMissing"));
    const dist = new Map([[id, 0]]), via = new Map(), items = [];
    let frontier = [id];
    for (let d = 1; d <= 3; d++) {
      const next = [];
      for (const v of frontier) for (const e of G.inn.get(v) || []) {
        const n = G.byId.get(e.source);
        if (!n || dist.has(e.source) || (!tests && n.test)) continue;
        dist.set(e.source, d); via.set(e.source, v); next.push(e.source);
        items.push(Object.assign({}, n, { depth: d, relation: e.relation, via: v, via_title: G.byId.get(v).title }));
      }
      frontier = next;
    }
    return { id, depth: 3, count: items.length, files: items.length, tests: items.filter((x) => x.test).length, truncated: false, items };
  }
  function offlinePath(from, to) {
    const G = offlineGraph();
    if (!G.byId.has(from) || !G.byId.has(to)) throw notFound(t("offlineMissing"));
    for (const [dir, a, b] of [["forward", from, to], ["backward", to, from]]) {
      const prev = new Map([[a, null]]);
      let queue = [a];
      while (queue.length && !prev.has(b)) {
        const next = [];
        for (const u of queue) for (const e of G.out.get(u) || []) {
          if (prev.has(e.target)) continue;
          prev.set(e.target, { u, rel: e.relation }); next.push(e.target);
        }
        queue = next;
      }
      if (prev.has(b)) {
        const steps = [];
        for (let cur = b; cur !== null; cur = prev.get(cur) ? prev.get(cur).u : null) {
          const st = prev.get(cur);
          steps.push(Object.assign({}, G.byId.get(cur), { relation: st ? st.rel : null }));
        }
        return { from, to, found: true, direction: dir, steps: steps.reverse() };
      }
    }
    return { from, to, found: false, direction: null, steps: [] };
  }
  let offRows = null;
  function offlineSearch(query) {
    const q = query.trim().split(/\s+/).join(" ").toLowerCase().replace(/\(\)$/, "");
    if (!q) return [];
    if (!offRows) { // a file's note and the symbols of its outline; a symbol opens the note of its file
      offRows = [];
      for (const n of Object.values(OFFLINE.notes)) {
        const name = String(n.title).toLowerCase();
        offRows.push({ id: n.id, title: n.title, kind: n.kind, file: n.file || "", name, own: name, path: String(n.file || "").toLowerCase(), test: !!n.test });
        for (const o of n.outline || []) {
          const nm = String(o.title).replace(/\(\)$/, "").toLowerCase();
          offRows.push({ id: n.id, title: o.title, kind: o.kind, file: n.file || "", line: o.line, name: nm, own: nm.split(".").pop(), path: "", test: !!n.test });
        }
      }
    }
    const dotted = q.includes("."), out = [];
    for (const r of offRows) {
      let s = 0, why = "";
      if (r.own === q || r.name === q) { s = 3; why = "name"; }
      else if (r.own.startsWith(q) || (dotted && r.name.startsWith(q))) { s = 2; why = "name starts with"; }
      else if (r.own.includes(q) || (dotted && r.name.includes(q))) { s = 1.5; why = "name contains"; }
      else if (r.path.includes(q)) { s = 1; why = "path"; }
      if (s) out.push({ s: s - (r.test ? 0.3 : 0), r, why });
    }
    out.sort((a, b) => b.s - a.s || a.r.title.length - b.r.title.length || (a.r.title < b.r.title ? -1 : a.r.title > b.r.title ? 1 : 0));
    return out.slice(0, 40).map(({ r, why }) => ({ id: r.id, title: r.title, kind: r.kind, file: r.line ? `${r.file}:${r.line}` : r.file, why }));
  }

  // -- a note's preview when a link to it is hovered -------------------------------------------------
  const preview = $("#preview"), previewCache = new Map();
  let previewTimer = null, previewFor = null;
  async function notePreview(id) {
    if (previewCache.has(id)) return previewCache.get(id);
    const n = await api("/api/note?id=" + encodeURIComponent(id));
    if (previewCache.size > 60) previewCache.delete(previewCache.keys().next().value);
    previewCache.set(id, n);
    return n;
  }
  function hidePreview() { clearTimeout(previewTimer); previewTimer = null; previewFor = null; preview.hidden = true; }
  function placePreview(a) {
    const r = a.getBoundingClientRect(), w = Math.min(460, window.innerWidth - 24);
    preview.style.width = w + "px";
    preview.style.left = Math.max(12, Math.min(r.left, window.innerWidth - w - 12)) + "px";
    const below = r.bottom + 8, h = preview.offsetHeight || 220;
    preview.style.top = (below + h < window.innerHeight - 8 ? below : Math.max(8, r.top - h - 8)) + "px";
  }
  async function showPreview(a, id) {
    let n;
    try { n = await notePreview(id); } catch (_) { return; }
    if (previewFor !== id) return;
    const kids = [el("div", { class: "pv-head" }, kindBadge(n.kind), el("strong", { text: n.title })),
      el("div", { class: "muted small mono", text: n.file ? `${n.file}${n.line ? ":" + n.line : ""}` : "" })];
    if (n.signature) kids.push(el("pre", { class: "sig mono", text: n.signature }));
    if (n.doc) kids.push(el("div", { class: "pv-doc", text: n.doc.split("\n").slice(0, 4).join("\n") }));
    if (n.user_note) kids.push(el("div", { class: "pv-mine" }, el("span", { class: "unote-st st-" + n.user_note.status, text: t("nst." + n.user_note.status) }),
      " " + n.user_note.text.split("\n")[0].slice(0, 140)));
    const counts = (n.sections || []).filter((s) => s.key !== "claims").slice(0, 5).map((s) => `${t("sec." + s.key)} ${s.count}`);
    if (counts.length) kids.push(el("div", { class: "muted small", text: counts.join(" · ") }));
    if (n.code && n.code.lines && n.code.lines.length) {
      const c = n.code, k = Math.min(8, c.lines.length);
      kids.push(codeBlock({ start: c.start, end: c.start + k - 1, lines: c.lines.slice(0, k), total: k, lang: c.lang }, n.file));
    }
    preview.replaceChildren(...kids);
    preview.hidden = false;
    placePreview(a);
  }
  document.addEventListener("mouseover", (ev) => {
    if (preview.contains(ev.target)) { clearTimeout(previewTimer); return; } // reading the card
    const a = ev.target.closest && ev.target.closest('a[href^="#/n/"]');
    if (!a) return;
    const id = decodeURIComponent(a.getAttribute("href").slice(4));
    if (id === current || id === previewFor) return;
    clearTimeout(previewTimer);
    previewFor = id;
    previewTimer = setTimeout(() => showPreview(a, id), 350);
  });
  document.addEventListener("mouseout", (ev) => {
    const a = ev.target.closest && ev.target.closest('a[href^="#/n/"], #preview');
    if (!a) return;
    const to = ev.relatedTarget;
    if (to && (preview.contains(to) || (to.closest && to.closest('a[href^="#/n/"]') === a))) return;
    clearTimeout(previewTimer);
    previewTimer = setTimeout(hidePreview, 250);
  });
  for (const evName of ["scroll", "click"]) document.addEventListener(evName, (ev) => { if (!preview.contains(ev.target)) hidePreview(); }, true);
  window.addEventListener("hashchange", hidePreview);

  // -- the page follows the index: when `verinoda update` (or --watch) rebuilt it, redraw ----------
  let indexKey = null, toastTimer = null;
  function toast(text, sticky = false) {
    let box = $("#toast");
    if (!box) { box = el("div", { id: "toast" }); document.body.append(box); }
    box.textContent = text; box.hidden = false;
    clearTimeout(toastTimer);
    if (!sticky) toastTimer = setTimeout(() => { box.hidden = true; }, 3500);
  }
  async function followIndex(first = false) { // first: the key of the index the page was drawn from, even in a hidden tab
    if (OFFLINE || (document.hidden && !first)) return;
    let r;
    try { r = await api("/api/version"); } catch (_) { return; }
    if (r.watch && r.watch.running) toast(t("updating"), true);
    if (indexKey === null) { indexKey = r.key; return; }
    if (r.key === indexKey) { if (!(r.watch && r.watch.running) && $("#toast") && $("#toast").textContent === t("updating")) $("#toast").hidden = true; return; }
    indexKey = r.key;
    previewCache.clear();
    globalKey = null;
    loadTree();
    const h = location.hash || "#/";
    if (h === "#/graph") showGraph();
    else if (h.startsWith("#/n/")) openNote(decodeURIComponent(h.slice(4)), true);
    else route();
    toast(t("refreshed"));
  }
  if (!OFFLINE) setInterval(() => followIndex(), 3000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) followIndex(); }); // back to the tab: look now

  // -- routing and chrome ----------------------------------------------------------------------
  function route() {
    const h = location.hash || "#/";
    if (h === "#/graph") { showGraph(); return; }
    $("#graphview").hidden = true;
    if (G3) { stopTour(); G3.setActive(false); }
    $("#tooltip").hidden = true;
    beforeGraph = h;
    if (h.startsWith("#/n/")) openNote(decodeURIComponent(h.slice(4)));
    else if (h.startsWith("#/q/") && !OFFLINE) renderAnswer(decodeURIComponent(h.slice(4)));
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
  $("#editor").value = editor;
  $("#editor").hidden = !!OFFLINE; // an exported file cannot know where the project is
  $("#editor").addEventListener("change", (ev) => {
    editor = ev.target.value === "none" || EDITORS[ev.target.value] ? ev.target.value : "vscode";
    store.set("vn.editor", editor);
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
    if ((ev.key === "k" || ev.key === "K") && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); if (pal.box.hidden) openPalette(); else closePalette(); return; }
    if (!pal.box.hidden) return; // the command bar has its own keys
    if (!$("#keys").hidden) { if (ev.key === "Escape" || ev.key === "?") { ev.preventDefault(); $("#keys").hidden = true; } return; }
    if (ev.key === "/" && !typing) { ev.preventDefault(); input.focus(); input.select(); return; }
    if (ev.key === "?" && !typing) { ev.preventDefault(); showKeys(); return; }
    if (ev.key === "Escape" && !preview.hidden) { hidePreview(); return; }
    if (ev.key === "Escape" && !$("#graphview").hidden) { // one step back at a time: the tour, what is lit, the view
      if (typing) { ev.target.blur(); return; }
      if (tour) { stopTour(); return; }
      if (mode3d && G3 && (G3.selected || G3.region || G3.path)) { clear3d(); return; }
      location.hash = beforeGraph; return;
    }
    if (typing || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (!$("#graphview").hidden && graphKey(ev)) { ev.preventDefault(); return; }
    if (ev.key === "g") location.hash = "#/graph";
  });

  // -- start -----------------------------------------------------------------------------------
  window.__verinoda = { local, global: globalG, g3: G3, offline: !!OFFLINE }; // for tests and debugging
  if (OFFLINE) {
    $("#search").dataset.i18nPlaceholder = "searchOffline";
    if (!location.hash) { // an exported file opens on its graph
      // a browser that refuses to rewrite a file:// address still gets there, through the hash itself
      try { history.replaceState(null, "", "#/graph"); } catch (_) { location.hash = "#/graph"; }
    }
  }
  applyI18n();
  applyTheme();
  statsReady = api("/api/stats").then((s) => {
    $("#project").textContent = s.project;
    if (!OFFLINE && s.root) projectRoot = s.root; // editor links need the project's folder
    return s;
  }).catch(() => null);
  loadTree();
  route();
  followIndex(true);
})();
