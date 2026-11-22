// Page tabs (DESIGN.md §2.9): the keyboard support, and the tabs' state and URL once htmx swaps a panel in; and the
// Usage tab's paging (§2.8), whose pages htmx swaps in too, each a history entry. It listens on the document, so it
// also serves tabs the hash-IRI script adds.
(() => {
  "use strict";
  const TAB = '[role="tablist"] > [role="tab"]';
  const PAGER = ".pager";
  // The history state's key for the Usage panel an entry shows, which Back and Forward fetch; htmx's own is `htmx`.
  const PANEL = "browlPanel";

  const tabsOf = (tab) => Array.from(tab.parentElement.querySelectorAll(':scope > [role="tab"]'));
  const isSelected = (tab) => tab.getAttribute("aria-selected") === "true";

  // Only the selected tab is in the Tab sequence; set once focus first enters a tab list.
  function rove(tabs) {
    for (const tab of tabs) {
      tab.tabIndex = isSelected(tab) ? 0 : -1;
    }
  }

  document.addEventListener("focusin", (event) => {
    const tab = event.target.closest?.(TAB);
    if (!tab || tab.parentElement.dataset.roving) {
      return;
    }
    const tabs = tabsOf(tab);
    tab.parentElement.dataset.roving = "true";
    rove(tabs);
    const from = event.relatedTarget;
    if (!isSelected(tab) && !(from && tab.parentElement.contains(from))) {
      tabs.find(isSelected)?.focus(); // focus entering the list lands on the selected tab
    }
  });

  document.addEventListener("keydown", (event) => {
    const tab = event.target.closest?.(TAB);
    if (!tab || event.altKey || event.ctrlKey || event.metaKey) {
      return;
    }
    const tabs = tabsOf(tab);
    const index = tabs.indexOf(tab);
    const to = {
      ArrowRight: tabs[(index + 1) % tabs.length],
      ArrowLeft: tabs[(index - 1 + tabs.length) % tabs.length],
      Home: tabs[0],
      End: tabs[tabs.length - 1],
    }[event.key];
    if (to) {
      event.preventDefault();
      to.focus();
    } else if (event.key === " ") {
      event.preventDefault(); // Enter follows the link already; Space would scroll
      tab.click();
    }
  });

  // A click with a modifier key (e.g. to open the tab in a new browser tab) is left to the browser: stopped here, in
  // the capture phase, before htmx's handler on the tab sees it.
  document.addEventListener(
    "click",
    (event) => {
      if (event.target.closest?.(`${TAB}, ${PAGER} a`) && (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey)) {
        event.stopPropagation();
      }
    },
    true,
  );

  // The tab that made a request; `detail.elt` is the swap's target in some events, such as htmx:afterSwap. Not an
  // element within a tab, such as a label fetched once the page renders, which makes requests of its own.
  const tabOfRequest = (event) => {
    const elt = event.detail.requestConfig?.elt ?? event.detail.elt;
    return elt?.matches?.(TAB) ? elt : null;
  };

  // The selected tab's panel is already shown, so it isn't fetched again.
  document.addEventListener("htmx:beforeRequest", (event) => {
    const tab = tabOfRequest(event);
    if (tab && isSelected(tab)) {
      event.preventDefault();
    }
  });

  document.addEventListener("htmx:afterSwap", (event) => {
    const tab = tabOfRequest(event);
    if (!tab) {
      return;
    }
    const tabs = tabsOf(tab);
    for (const other of tabs) {
      other.setAttribute("aria-selected", other === tab ? "true" : "false");
    }
    rove(tabs);
    if (tab.parentElement.contains(document.activeElement)) {
      tab.focus(); // e.g. a first click, whose focus went to the tab selected before
    }
    event.detail.target.setAttribute("aria-labelledby", tab.id);
    // Replaced, not pushed: switching tabs isn't a navigation, and htmx's own history would fight the hash-IRI script.
    history.replaceState(withPanel(history.state), "", tab.href);
  });

  // A panel that couldn't be fetched (e.g. the site is loading, or the network failed) is left to the tab's page, or
  // the Usage page's.
  for (const name of ["htmx:responseError", "htmx:sendError"]) {
    document.addEventListener(name, (event) => {
      const tab = tabOfRequest(event);
      const control = controlOfRequest(event);
      if (tab) {
        location.assign(tab.href);
      } else if (control?.matches(PAGER)) {
        location.reload(); // Back's or Forward's, whose URL is already the page's
      } else if (control) {
        location.assign(pageUrl(control));
      }
    });
  }

  // The Usage tab's paging: Previous, Next and the form (`usage.html`) fetch a page's panel with htmx, and push its
  // page's URL; Back and Forward fetch the panel each entry records. Not htmx's own history, which would fight the
  // hash-IRI script.

  const shownPager = () => document.querySelector(`#tab-panel ${PAGER}`);

  // A history state that records the Usage panel shown, if it has paging; for another tab's, the tabs drop it.
  function withPanel(state) {
    const next = { ...state };
    delete next[PANEL];
    const pager = shownPager();
    if (pager) {
      next[PANEL] = pager.dataset.panel;
    }
    return next;
  }

  // The paging's control that made a request, a link or the form; or the pager itself, as Back's or Forward's source.
  const controlOfRequest = (event) => {
    const elt = event.detail.requestConfig?.elt ?? event.detail.elt;
    return elt?.closest?.(PAGER) ? elt : null;
  };

  // The URL a control loads without JS; the first page's without `page`, as the server redirects it.
  function pageUrl(control) {
    if (control.matches("a")) {
      return control.href;
    }
    const url = new URL(control.action);
    const query = new URLSearchParams(new FormData(control));
    if (query.get("page") === "1") {
      query.delete("page");
    }
    url.search = query;
    return url.href;
  }

  // The entry left records its own panel, e.g. that of the page as served, so Back to it swaps that panel in.
  document.addEventListener("htmx:beforeRequest", (event) => {
    const control = controlOfRequest(event);
    if (control && !control.matches(PAGER)) {
      history.replaceState(withPanel(history.state), "");
    }
  });

  let hadFocus = false; // whether the panel had the focus before a page was swapped in
  document.addEventListener("htmx:beforeSwap", (event) => {
    if (controlOfRequest(event)) {
      hadFocus = event.detail.target.contains(document.activeElement);
    }
  });

  document.addEventListener("htmx:afterSwap", (event) => {
    const control = controlOfRequest(event);
    if (!control) {
      return;
    }
    if (!control.matches(PAGER)) {
      history.pushState(withPanel(history.state), "", pageUrl(control));
    }
    const panel = event.detail.target;
    panel.scrollTop = 0;
    // htmx refocuses the control with the same `id`; the panel, if the new page lacks it (e.g. Next on the last).
    if (hadFocus && !panel.contains(document.activeElement)) {
      panel.focus({ preventScroll: true });
    }
  });

  let hash = location.hash;
  window.addEventListener("hashchange", () => {
    hash = location.hash;
  });

  window.addEventListener("popstate", (event) => {
    const moved = location.hash !== hash; // a fragment change, which the hash-IRI script handles
    hash = location.hash;
    if (moved) {
      return;
    }
    // Any other entry of this page's is one the paging pushed, or one that showed a tab since: if it records no Usage
    // panel, or the Usage tab isn't shown, its URL is another tab's. Even after a reload, as pushed entries stay.
    const url = event.state?.[PANEL];
    const pager = shownPager();
    if (url && pager) {
      htmx.ajax("GET", url, { source: pager, target: "#tab-panel" });
    } else {
      location.reload();
    }
  });
})();
