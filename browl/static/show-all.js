// The "Scroll as one page" toggle (DESIGN.md §2.9, U-44). The stylesheet reacts to the checkbox itself, so it works
// without JS; this only remembers its state in localStorage, across pages, including those the hash-IRI script moves
// into <main>. Storage that fails (e.g. blocked) just leaves the toggle unremembered.
(() => {
  "use strict";
  const KEY = "browl.showAll";
  const ID = "show-all";

  function restore() {
    const toggle = document.getElementById(ID);
    if (!toggle) {
      return;
    }
    try {
      toggle.checked = localStorage.getItem(KEY) === "true";
    } catch {
      // not remembered
    }
  }

  document.addEventListener("change", (event) => {
    if (event.target.id === ID) {
      try {
        localStorage.setItem(KEY, String(event.target.checked));
      } catch {
        // not remembered
      }
    }
  });

  restore();
  const main = document.querySelector("main");
  if (main) {
    new MutationObserver(restore).observe(main, { childList: true });
  }
})();
