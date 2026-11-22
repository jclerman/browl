// Page tabs (DESIGN.md §2.9): the keyboard support, and the tabs' state and URL once htmx swaps a panel in. It listens
// on the document, so it also serves tabs the hash-IRI script adds.
(() => {
  "use strict";
  const TAB = '[role="tablist"] > [role="tab"]';

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
      if (event.target.closest?.(TAB) && (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey)) {
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
    history.replaceState(history.state, "", tab.href);
  });

  // A panel that couldn't be fetched (e.g. the site is loading, or the network failed) is left to the tab's page.
  for (const name of ["htmx:responseError", "htmx:sendError"]) {
    document.addEventListener(name, (event) => {
      const tab = tabOfRequest(event);
      if (tab) {
        location.assign(tab.href);
      }
    });
  }
})();
