// An entity page's toggles (DESIGN.md §2.9): "Scroll as one page" (U-44) and "Hierarchy" (U-45). The stylesheet
// reacts to the checkboxes themselves, so they work without JS; this only remembers their states in localStorage,
// across pages, including those the hash-IRI script moves into <main>.
(() => {
  "use strict";
  // Each toggle's checkbox id: its storage key, and its state when none is remembered.
  const TOGGLES = {
    "one-page": { key: "browl.onePage", checked: false },
    "hierarchy-shown": { key: "browl.hierarchyShown", checked: true },
  };

  function restore() {
    for (const [id, { key, checked }] of Object.entries(TOGGLES)) {
      const toggle = document.getElementById(id);
      if (!toggle) {
        continue;
      }
      try {
        const stored = localStorage.getItem(key);
        toggle.checked = stored === null ? checked : stored === "true";
      } catch {
        // not remembered
      }
    }
  }

  document.addEventListener("change", (event) => {
    const toggle = TOGGLES[event.target.id];
    if (toggle) {
      try {
        localStorage.setItem(toggle.key, String(event.target.checked));
      } catch {
        // not remembered
      }
    }
  });

  // A page from the back-forward cache, or another tab's change, may show a stale state.
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
      restore();
    }
  });
  window.addEventListener("storage", (event) => {
    if (Object.values(TOGGLES).some(({ key }) => key === event.key)) {
      restore();
    }
  });

  restore();
  const main = document.querySelector("main");
  if (main) {
    new MutationObserver(restore).observe(main, { childList: true });
  }
})();
