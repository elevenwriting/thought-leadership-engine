// Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
// (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

// An emoji picker for Slack and ClickUp reactions: Slack's names for common emoji, a search box, and
// any other name (a workspace's custom emoji) can be typed in and used as it is.
(() => {
  // The list comes from tle/emoji.py, set on the page as window.TLE_EMOJI.
  const EMOJI = window.TLE_EMOJI || {};
  const names = Object.keys(EMOJI);

  function enhance(field) {
    if (field.dataset.ready) return;
    field.dataset.ready = "1";
    const input = field.querySelector("input[type=hidden]");
    const button = field.querySelector(".emoji-btn");
    const glyph = field.querySelector(".emoji-glyph");
    const label = field.querySelector(".emoji-name");
    let pop = null;

    function show() {
      const name = input.value.replace(/:/g, "").trim();
      glyph.textContent = EMOJI[name] || (name ? "◌" : "");
      label.textContent = name ? `:${name}:` : "Pick an emoji";
    }

    function choose(name) {
      input.value = name;
      show();
      input.dispatchEvent(new Event("change", { bubbles: true }));
      close();
    }

    function open() {
      pop = document.createElement("div");
      pop.className = "emoji-pop";
      pop.innerHTML = '<input type="search" class="ms-search" placeholder="Search, or type a custom emoji name" aria-label="Search emoji"><div class="emoji-grid" role="listbox" aria-label="Emoji"></div>';
      const search = pop.firstElementChild, grid = pop.lastElementChild;
      const draw = () => {
        const q = search.value.replace(/:/g, "").trim().toLowerCase();
        const found = names.filter((n) => n.includes(q));
        grid.innerHTML = "";
        for (const n of found) {
          const b = document.createElement("button");
          b.type = "button";
          b.className = "emoji-opt";
          b.title = `:${n}:`;
          b.setAttribute("role", "option");
          b.setAttribute("aria-label", n.replace(/_/g, " "));
          b.setAttribute("aria-selected", n === input.value);
          b.textContent = EMOJI[n];
          b.addEventListener("click", () => choose(n));
          grid.append(b);
        }
        if (q && !names.includes(q) && /^[a-z0-9_+\-']+$/.test(q)) {
          const custom = document.createElement("button");
          custom.type = "button";
          custom.className = "emoji-custom";
          custom.textContent = `Use :${q}:`;
          custom.addEventListener("click", () => choose(q));
          grid.append(custom);
        }
      };
      search.addEventListener("input", draw);
      search.addEventListener("keydown", (e) => {
        if (e.key === "Escape") { e.preventDefault(); close(); button.focus(); }
        if (e.key === "Enter") { e.preventDefault(); grid.querySelector("button")?.click(); }
      });
      draw();
      field.append(pop);
      button.setAttribute("aria-expanded", "true");
      search.focus();
      document.addEventListener("mousedown", away);
    }

    function close() {
      pop?.remove();
      pop = null;
      button.setAttribute("aria-expanded", "false");
      document.removeEventListener("mousedown", away);
    }

    function away(e) { if (!field.contains(e.target)) close(); }

    button.addEventListener("click", () => (pop ? close() : open()));
    show();
  }

  window.tleEmoji = (root) => root.querySelectorAll(".emoji-field").forEach(enhance);
  const all = () => window.tleEmoji(document);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", all);
  else all();
})();
