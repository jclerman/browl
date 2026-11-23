// Links that htmx loads in place (`a[href][hx-get]`), such as page tabs, Usage paging and tree toggles: a click with
// a modifier key (e.g. to open the page in a new browser tab) is left to the browser. It's stopped here, in the
// capture phase, before htmx's handler on the link sees it.
document.addEventListener(
  "click",
  (event) => {
    const modified = event.ctrlKey || event.metaKey || event.shiftKey || event.altKey;
    if (modified && event.target.closest?.("a[href][hx-get]")) {
      event.stopPropagation();
    }
  },
  true,
);
