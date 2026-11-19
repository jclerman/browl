// The hash-IRI script (DESIGN.md §2.3, U-13). The server never sees a URL's fragment, so for a path that hash
// entities share, this reads the fragment and shows that entity's page: it fetches the page from the lookup route
// (`hash?path=…&fragment=…`, with the URL's own query, such as the tab) and puts its main content and title in place of
// this page's. Whatever page comes back is shown, "not found" and "loading" included. It runs again when the fragment
// changes, as a link to another entity under the same path changes only the fragment. Without a fragment, a path with
// an entity of its own shows that entity, as served; one without shows "not found".
(function () {
  "use strict";
  const script = document.currentScript;
  const lookup = script.dataset.lookup;
  const path = script.dataset.path;
  const prefixOnly = script.dataset.prefixOnly === "true";
  let main = null;
  let served = null;
  let servedTitle = "";
  let latest = 0;

  async function show() {
    const fragment = location.hash.slice(1);
    if (!fragment && !prefixOnly) {
      if (main.dataset.hashShown) {
        main.replaceChildren(...served.map((node) => node.cloneNode(true)));
        document.title = servedTitle;
        delete main.dataset.hashShown;
      }
      return;
    }
    const request = ++latest;
    const query = new URLSearchParams(location.search);
    query.set("path", path);
    query.set("fragment", fragment);
    let html;
    try {
      const response = await fetch(`${lookup}?${query}`, { headers: { Accept: "text/html" } });
      html = await response.text();
    } catch (error) {
      html = "<title>Error</title><main><h1>Error</h1><p>The page couldn't be loaded. Please try again.</p></main>";
    }
    if (request !== latest) {
      return; // the fragment changed again while this was loading
    }
    const page = new DOMParser().parseFromString(html, "text/html");
    const content = page.querySelector("main");
    main.replaceChildren(...(content ? content.childNodes : []));
    main.dataset.hashShown = "true";
    document.title = page.title;
  }

  document.addEventListener("DOMContentLoaded", () => {
    main = document.querySelector("main");
    served = Array.from(main.childNodes, (node) => node.cloneNode(true));
    servedTitle = document.title;
    window.addEventListener("hashchange", show);
    show();
  });
})();
