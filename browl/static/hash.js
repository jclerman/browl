// The hash-IRI script (DESIGN.md §2.3, U-13). The server never sees a URL's fragment, so this fetches the fragment's
// entity from `hash?path=…&fragment=…`, with the page's own query, and swaps the result's <main> content and title
// into this page, again on every fragment change.
(() => {
  "use strict";
  const script = document.currentScript;
  const lookup = script.dataset.lookup;
  const path = script.dataset.path;
  const prefixOnly = script.dataset.prefixOnly === "true";
  const main = document.querySelector("main");
  const served = Array.from(main.childNodes, (node) => node.cloneNode(true));
  const servedTitle = document.title;
  const servedSearch = location.search;
  let shown = false; // whether <main> shows a fetched page rather than the served one
  let controller = null;

  async function show() {
    controller?.abort();
    controller = new AbortController();
    const signal = controller.signal;
    const fragment = location.hash.slice(1);
    if (!fragment && !prefixOnly) {
      if (shown) {
        if (location.search !== servedSearch) {
          location.reload(); // e.g. another tab was chosen, so the served page isn't this URL's any more
          return;
        }
        main.replaceChildren(...served.map((node) => node.cloneNode(true)));
        window.htmx?.process(main);
        document.title = servedTitle;
        shown = false;
      }
      return;
    }
    const query = new URLSearchParams(location.search);
    query.set("path", path);
    query.set("fragment", fragment);
    let html;
    try {
      const response = await fetch(`${lookup}?${query}`, { headers: { Accept: "text/html" }, signal });
      if (response.status === 503) {
        location.reload(); // "loading" or "not available": the served page refreshes itself, which <main> can't
        return;
      }
      if (!(response.headers.get("Content-Type") ?? "").startsWith("text/html")) {
        throw new Error("not an HTML page"); // e.g. a proxy's error; only browl's pages are moved into <main>
      }
      html = await response.text();
    } catch (error) {
      html = "<title>Error</title><main><h1>Error</h1><p>The page couldn't be loaded. Please try again.</p></main>";
    }
    if (signal.aborted) {
      return; // the fragment changed again while this was loading
    }
    const page = new DOMParser().parseFromString(html, "text/html");
    const content = page.querySelector("main");
    main.replaceChildren(...(content ? content.childNodes : []));
    window.htmx?.process(main); // e.g. the tabs (`tabs.js`)
    document.title = page.title;
    shown = true;
  }

  window.addEventListener("hashchange", show);
  show();
})();
