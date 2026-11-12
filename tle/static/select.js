// Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
// (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

// Turns each <select> into a dropdown with a search box and, for <select multiple>, checkboxes.
// The select stays in the form, hidden, so forms submit as before. Add data-native to keep one as it is,
// and data-search to word its search box.
(() => {
  const SEARCH_FROM = 8;
  const CHEVRON = '<svg class="ms-chev" width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M2.5 4.5 6 8l3.5-3.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const TICK = '<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><path d="M2 5.2 4.1 7.3 8 3" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  let count = 0;

  function enhance(select) {
    if (select.dataset.native !== undefined || select.dataset.ms) return;
    const id = `ms${++count}`;
    select.dataset.ms = id;
    const multi = select.multiple;
    const label = select.labels[0];
    // The label's own words, without any hint inside it.
    const labelText = label ? [...label.childNodes].filter((n) => !n.classList?.contains("hint")).map((n) => n.textContent).join("") : "";
    const name = (labelText || select.getAttribute("aria-label") || "options").trim();

    const root = document.createElement("div");
    root.className = "ms";
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "ms-trigger";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    if (select.style.width) root.style.width = select.style.width;
    trigger.innerHTML = `<span class="ms-value"></span>${CHEVRON}`;
    const value = trigger.firstElementChild;
    root.append(trigger);
    select.after(root);
    select.hidden = true;
    select.tabIndex = -1;
    // The <label for> now points at the button.
    if (label) { label.htmlFor = `${id}-btn`; trigger.id = `${id}-btn`; }
    else trigger.setAttribute("aria-label", name);

    let pop = null, search = null, list = null, shown = [], active = 0;

    // The placeholder ("Pick one", or a multi-select's empty "Any") is shown when nothing is
    // picked, and is never offered as a choice. An empty value in a single select ("Off") is a choice.
    const isPlaceholder = (o) => o.dataset.placeholder !== undefined || (multi && o.value === "");
    const choices = () => [...select.options].filter((o) => !isPlaceholder(o));
    const placeholder = () => [...select.options].find(isPlaceholder)?.text || "Any";

    function render() {
      const picked = choices().filter((o) => o.selected && !(o.value === "" && !multi));
      const current = select.options[select.selectedIndex];
      const text = !picked.length ? (multi || !current || isPlaceholder(current) ? placeholder() : current.text)
        : picked.length === 1 ? picked[0].text : `${picked[0].text} +${picked.length - 1}`;
      value.textContent = text;
      value.classList.toggle("ms-placeholder", !picked.length && (multi || !current || isPlaceholder(current)));
      trigger.classList.toggle("ms-on", picked.length > 0);
    }

    function changed() {
      select.dispatchEvent(new Event("input", { bubbles: true }));
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }

    function pick(option) {
      if (multi) {
        option.selected = !option.selected;
        changed();
        draw();
      } else {
        select.value = option.value;
        changed();
        close();
      }
    }

    function draw() {
      const q = search ? search.value.trim().toLowerCase() : "";
      shown = choices().filter((o) => !q || o.text.toLowerCase().includes(q));
      active = Math.min(active, Math.max(shown.length - 1, 0));
      list.innerHTML = "";
      if (!shown.length) list.innerHTML = '<li class="ms-none">No matches</li>';
      shown.forEach((o, i) => {
        const li = document.createElement("li");
        li.id = `${id}-o${i}`;
        li.setAttribute("role", "option");
        li.className = i === active ? "ms-opt ms-active" : "ms-opt";
        li.setAttribute("aria-selected", o.selected);
        li.innerHTML = multi ? `<span class="ms-box" aria-hidden="true">${o.selected ? TICK : ""}</span>` : "";
        const span = document.createElement("span");
        span.textContent = o.text;
        li.append(span);
        li.addEventListener("mousedown", (e) => { e.preventDefault(); pick(o); });
        li.addEventListener("mouseenter", () => { active = i; mark(); });
        list.append(li);
      });
      (search || list).setAttribute("aria-activedescendant", shown.length ? `${id}-o${active}` : "");
      const n = choices().filter((o) => o.selected).length;
      foot.hidden = !multi || !n;
      foot.firstElementChild.textContent = `${n} selected`;
    }

    function mark() {
      list.querySelectorAll(".ms-opt").forEach((li, i) => li.classList.toggle("ms-active", i === active));
      (search || list).setAttribute("aria-activedescendant", `${id}-o${active}`);
      list.querySelector(`#${id}-o${active}`)?.scrollIntoView({ block: "nearest" });
    }

    let foot;
    function open() {
      if (pop) return;
      pop = document.createElement("div");
      pop.className = "ms-pop";
      search = choices().length >= SEARCH_FROM ? document.createElement("input") : null;
      if (search) {
        search.type = "search";
        search.className = "ms-search";
        search.placeholder = select.dataset.search || `Search ${name.toLowerCase()}`;
        search.setAttribute("role", "combobox");
        search.setAttribute("aria-expanded", "true");
        search.setAttribute("aria-controls", `${id}-list`);
        search.addEventListener("input", () => { active = 0; draw(); });
        search.addEventListener("keydown", key);
        pop.append(search);
      }
      list = document.createElement("ul");
      list.id = `${id}-list`;
      list.className = "ms-list";
      list.setAttribute("role", "listbox");
      list.setAttribute("aria-label", name);
      if (multi) list.setAttribute("aria-multiselectable", "true");
      if (!search) { list.tabIndex = 0; list.addEventListener("keydown", key); }
      foot = document.createElement("div");
      foot.className = "ms-foot";
      foot.innerHTML = '<span class="muted"></span><button type="button" class="ms-clear-all">Clear</button>';
      foot.lastElementChild.addEventListener("click", () => {
        choices().forEach((o) => { o.selected = false; });
        changed();
        draw();
      });
      pop.append(list, foot);
      root.append(pop);
      active = 0;
      draw();
      active = multi ? 0 : Math.max(shown.indexOf(select.options[select.selectedIndex]), 0);
      mark();
      trigger.setAttribute("aria-expanded", "true");
      (search || list).focus({ preventScroll: true });
      document.addEventListener("mousedown", away);
    }

    function close(refocus = true) {
      if (!pop) return;
      pop.remove();
      pop = search = list = null;
      trigger.setAttribute("aria-expanded", "false");
      document.removeEventListener("mousedown", away);
      if (refocus) trigger.focus();
    }

    function away(e) { if (!root.contains(e.target)) close(false); }

    function key(e) {
      if (e.key === "ArrowDown") { e.preventDefault(); active = Math.min(active + 1, shown.length - 1); mark(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(active - 1, 0); mark(); }
      else if (e.key === "Enter" || (e.key === " " && e.target === list)) { e.preventDefault(); if (shown[active]) pick(shown[active]); }
      else if (e.key === "Escape") { e.preventDefault(); close(); }
      else if (e.key === "Tab") close(false);
    }

    trigger.addEventListener("click", () => (pop ? close() : open()));
    trigger.addEventListener("keydown", (e) => { if (e.key === "ArrowDown" && !pop) { e.preventDefault(); open(); } });
    // Pages that set the value in code send a change event; options added later are picked up too.
    select.addEventListener("change", render);
    new MutationObserver(() => { render(); if (pop) draw(); }).observe(select, { childList: true });
    select.form?.addEventListener("reset", () => setTimeout(render));
    render();
  }

  // A text box with a list of names (<input list>, e.g. Authors): the same menu as the selects,
  // in place of the browser's own. Names are comma-separated, so it suggests for the one being
  // typed and picking one fills it in, ready for the next.
  function suggest(input) {
    const source = input.list;
    if (!source || input.dataset.suggest) return;
    input.dataset.suggest = "1";
    const names = [...source.options].map((o) => o.value).filter(Boolean);
    input.removeAttribute("list");
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.autocomplete = "off";
    const wrap = document.createElement("div");
    wrap.className = "ms ms-suggest";
    input.replaceWith(wrap);
    wrap.appendChild(input);
    let pop = null, list = null, shown = [], active = 0;

    const parts = () => input.value.split(",");
    const typed = () => parts().at(-1).trim().toLowerCase();
    const chosen = () => parts().slice(0, -1).map((p) => p.trim().toLowerCase());
    const close = () => { pop?.remove(); pop = list = null; input.setAttribute("aria-expanded", "false"); };
    const pick = (name) => {
      const kept = parts().slice(0, -1).map((p) => p.trim()).filter(Boolean);
      input.value = [...kept, name].join(", ") + ", ";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      close();
      input.focus();
    };
    const mark = () => list?.querySelectorAll(".ms-opt").forEach((li, i) => {
      li.classList.toggle("ms-active", i === active);
      if (i === active) { li.scrollIntoView({ block: "nearest" }); input.setAttribute("aria-activedescendant", li.id); }
    });
    const draw = () => {
      const q = typed(), taken = chosen();
      shown = names.filter((n) => !taken.includes(n.toLowerCase()) && n.toLowerCase().includes(q) && n.toLowerCase() !== q);
      if (!shown.length || document.activeElement !== input) return close();
      if (!pop) {
        pop = document.createElement("div");
        pop.className = "ms-pop";
        list = document.createElement("ul");
        list.className = "ms-list";
        list.setAttribute("role", "listbox");
        list.id = `${input.id || "names"}-suggestions`;
        input.setAttribute("aria-controls", list.id);
        pop.appendChild(list);
        wrap.appendChild(pop);
        input.setAttribute("aria-expanded", "true");
      }
      active = Math.min(active, shown.length - 1);
      list.replaceChildren(...shown.map((name, i) => {
        const li = document.createElement("li");
        li.className = "ms-opt";
        li.id = `${list.id}-${i}`;
        li.setAttribute("role", "option");
        li.textContent = name;
        li.addEventListener("mousedown", (e) => { e.preventDefault(); pick(name); });
        return li;
      }));
      mark();
    };
    input.addEventListener("input", () => { active = 0; draw(); });
    input.addEventListener("focus", draw);
    input.addEventListener("blur", () => setTimeout(close, 100));
    input.addEventListener("keydown", (e) => {
      if (!pop) { if (e.key === "ArrowDown") { e.preventDefault(); draw(); } return; }
      if (e.key === "ArrowDown") { e.preventDefault(); active = Math.min(active + 1, shown.length - 1); mark(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(active - 1, 0); mark(); }
      else if (e.key === "Enter" || (e.key === "Tab" && typed())) { e.preventDefault(); pick(shown[active]); }
      else if (e.key === "Escape") { e.preventDefault(); close(); }
    });
  }

  window.tleSelect = enhance;
  window.tleSuggest = suggest;
  const all = () => {
    document.querySelectorAll("select").forEach(enhance);
    document.querySelectorAll("input[list]").forEach(suggest);
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", all);
  else all();
})();
