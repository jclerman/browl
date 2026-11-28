// The navigation bar's search box: a WAI-ARIA combobox over htmx's suggestions (U-10, U-17; DESIGN.md §2.9, Search),
// its `/` shortcut, and the search page's settings, kept in this browser. Not deferred, so a setting is applied before
// the page is first drawn.
(() => {
  "use strict";
  const BOX = "site-search-box";
  const LIST = "site-search-list";
  const STATUS = "site-search-status";
  const SETTINGS = ".search-settings"; // the search page's settings, hidden in the markup as they need this script
  const SHORTCUT = { key: "browl.slashShortcut", control: "slash-shortcut" };

  // Settings, each stored as JSON under its `browl.…` key. A setting's value is its fallback if none is stored, if
  // what is isn't JSON, or if storage fails, e.g. blocked; storing it then fails silently.
  function setting(key, fallback) {
    try {
      const value = localStorage.getItem(key);
      return value === null ? fallback : JSON.parse(value);
    } catch {
      return fallback;
    }
  }

  function setSetting(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      // not remembered
    }
  }
  // Whether Escape closed the list since the box's text last changed: an answer to a request already pending, or
  // still waiting out its pause, mustn't reopen it.
  let dismissed = false;

  const parts = () => ({
    box: document.getElementById(BOX),
    list: document.getElementById(LIST),
    status: document.getElementById(STATUS),
  });
  // The options the keys and the pointer reach: not "No matches".
  const options = (list) => [...list.querySelectorAll('[role="option"]:not([aria-disabled="true"])')];
  const active = (box) => {
    const id = box.getAttribute("aria-activedescendant");
    return id ? document.getElementById(id) : null;
  };

  function setActive(box, list, option) {
    for (const each of options(list)) {
      each.setAttribute("aria-selected", each === option ? "true" : "false");
    }
    if (option) {
      box.setAttribute("aria-activedescendant", option.id);
      option.scrollIntoView({ block: "nearest" });
    } else {
      box.removeAttribute("aria-activedescendant");
    }
  }

  function open(box, list) {
    if (list.children.length) {
      list.hidden = false;
      box.setAttribute("aria-expanded", "true");
    }
  }

  function close(box, list) {
    setActive(box, list, null);
    list.hidden = true;
    box.setAttribute("aria-expanded", "false");
  }

  function move(box, list, step) {
    const all = options(list);
    if (!all.length) {
      return;
    }
    open(box, list);
    const index = all.indexOf(active(box));
    // From none, down goes to the first and up to the last; past either end, back to none, in the box.
    const next = index === -1 ? (step > 0 ? 0 : all.length - 1) : index + step;
    setActive(box, list, all[next] ?? null);
  }

  function follow(option) {
    const link = option.querySelector("a[href]");
    if (link) {
      window.location.assign(link.href);
    }
  }

  document.addEventListener("keydown", (event) => {
    const { box, list } = parts();
    if (!box || event.target !== box || event.isComposing) {
      return;
    }
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        if (event.altKey) {
          open(box, list);
        } else {
          move(box, list, 1);
        }
        break;
      case "ArrowUp":
        event.preventDefault();
        move(box, list, -1);
        break;
      case "Enter": {
        const option = list.hidden ? null : active(box);
        if (option) {
          event.preventDefault();
          follow(option);
        }
        break; // else the form opens the full results page
      }
      case "Escape":
        if (!list.hidden) {
          event.preventDefault();
          close(box, list);
          dismissed = true;
        } else if (box.value) {
          event.preventDefault();
          box.value = "";
          list.replaceChildren(); // its options were the old text's
          // As typing would: htmx then asks for the blank text, which replaces any pending request, empties the
          // status, and makes typing the old text again ask for it.
          box.dispatchEvent(new Event("input", { bubbles: true }));
        }
        break;
      default:
    }
  });

  // The `/` shortcut, unless turned off on the search page (WCAG 2.1.4). Off, <html>'s `data-slash-shortcut` hides
  // the box's hint (the stylesheet), and the box no longer announces it.
  function applyShortcut(on) {
    if (on) {
      delete document.documentElement.dataset.slashShortcut;
    } else {
      document.documentElement.dataset.slashShortcut = "off";
    }
    const { box } = parts();
    if (box) {
      if (on) {
        box.setAttribute("aria-keyshortcuts", "/");
      } else {
        box.removeAttribute("aria-keyshortcuts");
      }
    }
    const control = document.getElementById(SHORTCUT.control);
    if (control) {
      control.checked = on;
    }
  }

  // The stored settings; only `false` turns the shortcut off.
  function applySettings() {
    applyShortcut(setting(SHORTCUT.key, true) !== false);
    for (const group of document.querySelectorAll(`${SETTINGS}[hidden]`)) {
      group.hidden = false;
    }
  }

  document.addEventListener("change", (event) => {
    if (event.target.id === SHORTCUT.control) {
      setSetting(SHORTCUT.key, event.target.checked);
      applyShortcut(event.target.checked); // on this page, even if storage fails
    }
  });

  // A page from the back-forward cache, or another tab's change, may show a stale setting.
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
      applySettings();
    }
  });
  window.addEventListener("storage", (event) => {
    if (event.key === SHORTCUT.key || event.key === null) {
      applySettings();
    }
  });

  // "/" focuses the box from anywhere on the page (DESIGN.md §2.9, Search), but not in a field, where it's typed, nor
  // with Ctrl, Meta or Alt, the browser's and the system's. Shift and AltGr (which Windows reports as Ctrl+Alt) aren't
  // modifiers here, as some layouts need them for "/".
  const editable = (target) =>
    target instanceof Element && (target.matches("input, textarea, select") || target.isContentEditable);
  const altGraph = (event) => event.getModifierState("AltGraph") || (event.ctrlKey && event.altKey);
  const modified = (event) => event.metaKey || (!altGraph(event) && (event.ctrlKey || event.altKey));

  document.addEventListener("keydown", (event) => {
    const off = document.documentElement.dataset.slashShortcut === "off";
    if (event.key !== "/" || off || modified(event) || event.isComposing) {
      return;
    }
    const { box } = parts();
    if (!box || event.defaultPrevented || editable(event.target)) {
      return;
    }
    event.preventDefault(); // so the "/" isn't typed into the box, nor starts the browser's quick find
    box.focus();
    box.select(); // so typing replaces a query already there, as on the search page
  });

  // A new list replaces the old: shown if it has options, with nothing active, and its size announced.
  document.addEventListener("htmx:afterSwap", (event) => {
    const { box, list, status } = parts();
    if (!box || event.detail.target !== list) {
      return;
    }
    close(box, list);
    if (document.activeElement === box && !dismissed) {
      open(box, list); // if it has options
    }
    const hits = options(list).filter((option) => !option.classList.contains("search-all")).length;
    if (!list.children.length) {
      status.textContent = "";
    } else {
      status.textContent = hits ? `${hits} ${hits === 1 ? "suggestion" : "suggestions"}` : "No matches";
    }
  });

  // A click on an option shouldn't take the focus from the box first, which would close the list under it.
  document.addEventListener("mousedown", (event) => {
    const { list } = parts();
    if (list && list.contains(event.target)) {
      event.preventDefault();
    }
  });

  document.addEventListener("input", (event) => {
    const { box } = parts();
    if (box && event.target === box) {
      dismissed = false;
    }
  });

  document.addEventListener("focusout", (event) => {
    const { box, list } = parts();
    if (box && event.target === box) {
      close(box, list);
    }
  });

  document.addEventListener("focusin", (event) => {
    const { box, list } = parts();
    if (box && event.target === box) {
      open(box, list);
    }
  });

  // Hovering marks the option the pointer is over, as the keys would.
  document.addEventListener("mouseover", (event) => {
    const { box, list } = parts();
    const option = list && event.target.closest?.('[role="option"]');
    if (option && options(list).includes(option)) {
      setActive(box, list, option);
    }
  });

  applySettings();
})();
