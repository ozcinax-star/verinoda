/* Notes page: a list of notes and a command bar opened with Ctrl+K. No libraries. */
"use strict";
(() => {
  const $ = (sel) => document.querySelector(sel);
  const pal = { box: $("#palette"), input: $("#pal-input"), list: $("#pal-list") };

  function openPalette(text = "") {
    pal.box.hidden = false;
    pal.input.value = text;
    pal.input.focus();
  }
  function closePalette() {
    pal.box.hidden = true;
  }
  function verbItems() {
    return [
      { label: "Search notes", run: () => openPalette("search ") },
      { label: "Close", run: () => closePalette() },
    ];
  }
  function renderList(items) {
    pal.list.innerHTML = "";
    for (const it of items) {
      const li = document.createElement("li");
      li.textContent = it.label;
      pal.list.appendChild(li);
    }
  }
  function init() {
    renderList(verbItems());
  }
  document.addEventListener("keydown", (ev) => {
    if ((ev.key === "k" || ev.key === "K") && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      if (pal.box.hidden) openPalette(); else closePalette();
    }
  });
  init();
})();
