// An entity page's controls (DESIGN.md §2.9): the "Scroll as one page" (U-44) and "Hierarchy" (U-45) toggles, and the
// hierarchy column's width. The stylesheet reacts to the checkboxes themselves, so they work without JS; this only
// remembers their states in localStorage, across pages, including those the hash-IRI script moves into <main>. The
// handle beside the column drags its width, or the arrow keys change it; a double click goes back to the default.
(() => {
  "use strict";
  // Each toggle's checkbox id: its storage key, and its state when none is remembered.
  const TOGGLES = {
    "one-page": { key: "browl.onePage", checked: false },
    "hierarchy-shown": { key: "browl.hierarchyShown", checked: true },
  };

  const WIDTH_KEY = "browl.hierarchyWidth";
  const STEP = 16; // px, for an arrow key

  // The width the stylesheet allows: from 10rem to half the page.
  function bounds(page) {
    const rem = parseFloat(getComputedStyle(document.documentElement).fontSize);
    return [10 * rem, page.clientWidth / 2];
  }

  function setWidth(handle, width, save) {
    const page = handle.closest(".entity-page");
    const [low, high] = bounds(page);
    const bounded = Math.round(Math.min(Math.max(width, low), Math.max(low, high)));
    page.style.setProperty("--hierarchy-width", `${bounded}px`);
    handle.setAttribute("aria-valuenow", String(bounded));
    handle.setAttribute("aria-valuemin", String(Math.round(low)));
    handle.setAttribute("aria-valuemax", String(Math.round(high)));
    if (save) {
      try {
        localStorage.setItem(WIDTH_KEY, String(bounded));
      } catch {
        // not remembered
      }
    }
  }

  function setUpHandle() {
    const handle = document.querySelector(".hierarchy-resize");
    if (!handle || handle.dataset.ready) {
      return;
    }
    handle.dataset.ready = "true";
    handle.tabIndex = 0;
    const column = document.getElementById("hierarchy-column");
    const width = () => column.getBoundingClientRect().width;
    let stored = null;
    try {
      stored = localStorage.getItem(WIDTH_KEY);
    } catch {
      // not remembered
    }
    if (stored !== null && Number(stored) > 0) {
      setWidth(handle, Number(stored), false);
    }
    handle.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) {
        return;
      }
      event.preventDefault();
      handle.setPointerCapture(event.pointerId);
      const start = event.clientX - width();
      const move = (moved) => setWidth(handle, moved.clientX - start, false);
      handle.addEventListener("pointermove", move);
      handle.addEventListener(
        "pointerup",
        () => {
          handle.removeEventListener("pointermove", move);
          setWidth(handle, width(), true);
        },
        { once: true },
      );
    });
    handle.addEventListener("keydown", (event) => {
      const step = { ArrowLeft: -STEP, ArrowRight: STEP }[event.key];
      if (step) {
        event.preventDefault();
        setWidth(handle, width() + step, true);
      }
    });
    handle.addEventListener("dblclick", () => {
      handle.closest(".entity-page").style.removeProperty("--hierarchy-width");
      handle.removeAttribute("aria-valuenow");
      try {
        localStorage.removeItem(WIDTH_KEY);
      } catch {
        // not remembered
      }
    });
  }

  function restore() {
    setUpHandle();
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
