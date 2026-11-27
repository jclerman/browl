// An entity page's controls (DESIGN.md §2.9): the "Scroll as one page" (U-44) and "Hierarchy" (U-45) toggles, and the
// hierarchy column's width. The stylesheet reacts to the checkboxes themselves, so they work without JS; this only
// remembers their states in localStorage, across pages, including those the hash-IRI script moves into <main>. The
// handle beside the column drags its width, or the arrow keys change it; a double click goes back to the default. And
// the header's copy buttons.
(() => {
  "use strict";
  // Each toggle's checkbox id: its storage key, and its state when none is remembered. Only the other state's value
  // ("true" or "false") changes it, so a stored value that's neither, e.g. from an older version, leaves the default.
  const TOGGLES = {
    "one-page": { key: "browl.onePage", checked: false },
    "hierarchy-shown": { key: "browl.hierarchyShown", checked: true },
  };

  const WIDTH_KEY = "browl.hierarchyWidth";
  const STEP = 16; // px, for an arrow key

  function remember(key, value) {
    try {
      if (value === null) {
        localStorage.removeItem(key);
      } else {
        localStorage.setItem(key, value);
      }
    } catch {
      // not remembered
    }
  }

  // A key's value: null if none is stored, undefined if storage fails.
  function remembered(key) {
    try {
      return localStorage.getItem(key);
    } catch {
      return undefined;
    }
  }

  // The width the stylesheet allows: from 10rem to half the page.
  function bounds(page) {
    const rem = parseFloat(getComputedStyle(document.documentElement).fontSize);
    const low = 10 * rem;
    return [low, Math.max(low, page.clientWidth / 2)];
  }

  // Set the column's width, kept within bounds, unless `free`: a remembered width is applied as it is, as the
  // stylesheet bounds it to the window, so a narrow window doesn't cut it for a wider one later.
  function setWidth(handle, width, { save = false, free = false } = {}) {
    const page = handle.closest(".entity-page");
    const [low, high] = bounds(page);
    const bounded = Math.round(Math.min(Math.max(width, low), high));
    page.style.setProperty("--hierarchy-width", `${free ? width : bounded}px`);
    handle.setAttribute("aria-valuenow", String(bounded));
    handle.setAttribute("aria-valuemin", String(Math.round(low)));
    handle.setAttribute("aria-valuemax", String(Math.round(high)));
    if (save) {
      remember(WIDTH_KEY, String(bounded));
    }
  }

  // Once for each page's handle: the one served, or one the hash-IRI script moves in.
  function setUpHandle() {
    const handle = document.querySelector(".hierarchy-resize");
    if (!handle || handle.dataset.ready) {
      return;
    }
    handle.dataset.ready = "true";
    handle.tabIndex = 0;
    const column = document.getElementById("hierarchy-column");
    const width = () => column.getBoundingClientRect().width;
    const stored = Number(remembered(WIDTH_KEY) ?? NaN); // NaN if none
    if (Number.isFinite(stored) && stored > 0) {
      setWidth(handle, stored, { free: true });
    }
    handle.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) {
        return;
      }
      event.preventDefault();
      handle.setPointerCapture(event.pointerId);
      const start = event.clientX - width();
      const move = (moved) => setWidth(handle, moved.clientX - start);
      handle.addEventListener("pointermove", move);
      // The drag ends when the capture does: on pointerup, or a pointercancel, e.g. a touch the browser takes over.
      handle.addEventListener(
        "lostpointercapture",
        () => {
          handle.removeEventListener("pointermove", move);
          setWidth(handle, width(), { save: true });
        },
        { once: true },
      );
    });
    handle.addEventListener("keydown", (event) => {
      const step = { ArrowLeft: -STEP, ArrowRight: STEP }[event.key];
      if (step) {
        event.preventDefault();
        setWidth(handle, width() + step, { save: true });
      }
    });
    handle.addEventListener("dblclick", () => {
      handle.closest(".entity-page").style.removeProperty("--hierarchy-width");
      handle.removeAttribute("aria-valuenow");
      remember(WIDTH_KEY, null);
    });
  }

  function restore() {
    for (const [id, { key, checked }] of Object.entries(TOGGLES)) {
      const toggle = document.getElementById(id);
      const stored = remembered(key);
      if (toggle && stored !== undefined) {
        toggle.checked = stored === String(!checked) ? !checked : checked;
      }
    }
  }

  // The header's copy buttons (DESIGN.md §2.8), hidden in the markup as they need this script: each copies its CURIE
  // or IRI, and for a moment says whether it did, in its tooltip and name, and in the header's status region, which
  // screen readers announce.
  const SAID_FOR = 1500; // ms
  const restoring = new WeakMap(); // a button's timer that puts back its resting label, `data-label`

  function label(button, text) {
    button.title = text;
    button.querySelector(".visually-hidden").textContent = text;
  }

  // Once for each page's buttons: those served, or those the hash-IRI script moves in.
  function setUpCopyButtons() {
    for (const button of document.querySelectorAll(".copy-button[hidden]")) {
      label(button, button.dataset.label);
      button.hidden = false;
    }
  }

  document.addEventListener("click", async (event) => {
    const button = event.target.closest(".copy-button");
    if (!button) {
      return;
    }
    let said = "Copied";
    try {
      await navigator.clipboard.writeText(button.dataset.copy);
    } catch {
      said = "Couldn't copy";
    }
    button.classList.toggle("copied", said === "Copied");
    label(button, said);
    const status = button.closest(".entity-header")?.querySelector(".copy-status");
    if (status) {
      status.textContent = said;
    }
    clearTimeout(restoring.get(button)); // a second click starts the moment afresh
    restoring.set(
      button,
      setTimeout(() => {
        label(button, button.dataset.label);
        button.classList.remove("copied");
        if (status) {
          status.textContent = "";
        }
      }, SAID_FOR),
    );
  });

  document.addEventListener("change", (event) => {
    const toggle = TOGGLES[event.target.id];
    if (toggle) {
      remember(toggle.key, String(event.target.checked));
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
  setUpHandle();
  setUpCopyButtons();
  const main = document.querySelector("main");
  if (main) {
    new MutationObserver(() => {
      restore();
      setUpHandle();
      setUpCopyButtons();
    }).observe(main, { childList: true });
  }
})();
