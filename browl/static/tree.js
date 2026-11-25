// Hierarchy trees: opening and closing nodes in place, and the WAI-ARIA tree pattern's keys (DESIGN.md §2.9 "Tree
// pages"). It listens on the document, so it also serves trees added later.
(() => {
  "use strict";
  const TREE = '[role="tree"]';
  const ITEM = '[role="treeitem"]';
  const TOGGLE = ".tree-toggle";

  // A node's li holds its row (its toggle, item and counts) and its group; a paging link's, its item alone.
  const liOf = (item) => item.closest("li");
  const groupOf = (item) => liOf(item).querySelector(':scope > [role="group"]');
  const toggleOf = (item) => item.parentElement.querySelector(`:scope > ${TOGGLE}`);
  const itemOfToggle = (toggle) => toggle.parentElement.querySelector(`:scope > ${ITEM}`);
  const itemOf = (li) => li.querySelector(`:scope > ${ITEM}, :scope > .tree-row > ${ITEM}`);
  const isOpen = (item) => item.getAttribute("aria-expanded") === "true";

  // The items shown, walked through the tree's structure, so a key reads only the items near it.
  const firstItem = (tree) => tree.querySelector(ITEM);
  const parentLi = (li) => (li.parentElement.matches(TREE) ? null : li.parentElement.parentElement);

  function lastShownIn(li) {
    let item = itemOf(li);
    while (isOpen(item) && groupOf(item).lastElementChild) {
      item = itemOf(groupOf(item).lastElementChild);
    }
    return item;
  }

  function nextShown(item) {
    if (isOpen(item) && groupOf(item).firstElementChild) {
      return itemOf(groupOf(item).firstElementChild);
    }
    for (let li = liOf(item); li; li = parentLi(li)) {
      if (li.nextElementSibling) {
        return itemOf(li.nextElementSibling);
      }
    }
    return null;
  }

  function previousShown(item) {
    const li = liOf(item);
    if (li.previousElementSibling) {
      return lastShownIn(li.previousElementSibling);
    }
    const parent = parentLi(li);
    return parent && itemOf(parent);
  }

  // The item a request's toggle opens, or whose "Show more" made it.
  const sourceOf = (event) => event.detail.requestConfig?.elt ?? event.detail.elt;
  const toggleOfRequest = (event) => {
    const elt = sourceOf(event);
    return elt?.matches?.(TOGGLE) ? elt : null;
  };
  const moreOfRequest = (event) => {
    const elt = sourceOf(event);
    return elt?.matches?.(`.tree-paging > ${ITEM}`) ? elt : null;
  };
  const siblingsOfRequest = (event) => {
    const elt = sourceOf(event);
    return elt?.matches?.(`.tree-siblings > ${ITEM}`) ? elt : null;
  };

  // Only one item is in the Tab sequence, the roving one; the toggles are the mouse's, as the keys open and close
  // nodes. Applied to a tree once, and to what's added to it after, leaving the roving item as it is.
  function tame(container) {
    for (const item of container.querySelectorAll(`${ITEM}:not([tabindex])`)) {
      item.tabIndex = -1;
    }
    for (const toggle of container.querySelectorAll(TOGGLE)) {
      toggle.tabIndex = -1;
      toggle.setAttribute("aria-hidden", "true");
    }
  }

  function setUp(tree) {
    if (tree.dataset.roving) {
      return;
    }
    tree.dataset.roving = "true";
    tame(tree);
    const first = startItem(tree);
    if (first) {
      first.tabIndex = 0;
    }
  }

  // Where the keys start: in the hierarchy column, at the page's own entity.
  const startItem = (tree) => tree.querySelector(`${ITEM}[aria-current="page"]`) ?? firstItem(tree);

  // The hierarchy column scrolls within itself, not the page, to show the page's entity: a third of the way down, and
  // its toggle at the left, if it's out of view.
  function showCurrent() {
    for (const column of document.querySelectorAll(".hierarchy-column")) {
      const current = column.querySelector(`${ITEM}[aria-current="page"]`);
      if (!current) {
        continue;
      }
      const shown = column.getBoundingClientRect();
      const at = current.parentElement.getBoundingClientRect(); // its line, from its toggle
      if (at.bottom > shown.top + column.clientHeight) {
        column.scrollTop += at.top - shown.top - column.clientHeight / 3;
      }
      if (at.left + Math.min(at.width, column.clientWidth / 2) > shown.left + column.clientWidth) {
        column.scrollLeft += at.left - shown.left;
      }
    }
  }

  function rove(item) {
    for (const other of item.closest(TREE).querySelectorAll(`${ITEM}[tabindex="0"]`)) {
      other.tabIndex = -1;
    }
    item.tabIndex = 0;
  }

  function focusItem(item) {
    rove(item);
    item.focus();
  }

  function setOpen(item, open) {
    groupOf(item).hidden = !open;
    item.setAttribute("aria-expanded", open ? "true" : "false");
    relink(toggleOf(item), open);
  }

  // A toggle's link (for a modifier-click, or a failed fetch) follows its node's state: once open, it leaves the
  // node's IRI out of `open=`, and once closed, adds it back after the others. The hierarchy column's links are to
  // the nodes' pages, whatever their state.
  function relink(toggle, open) {
    if (toggle.closest(".hierarchy-column")) {
      return;
    }
    const iri = new URL(toggle.getAttribute("hx-get"), location.href).searchParams.get("node");
    const url = new URL(toggle.href);
    const params = [...url.searchParams].filter(([name, value]) => name !== "open" || value !== iri);
    if (!open) {
      params.splice(params.findLastIndex(([name]) => name === "open") + 1, 0, ["open", iri]);
    }
    url.search = new URLSearchParams(params).toString();
    toggle.setAttribute("href", url.pathname + url.search + url.hash);
  }

  // Open a node: in place if its children are loaded, else through its toggle, which htmx fetches them for.
  function open(item) {
    const group = groupOf(item);
    if (group.hasAttribute("data-loaded")) {
      setOpen(item, true);
    } else {
      toggleOf(item).click();
    }
  }

  // Close a node; the focus, or the roving item, if hidden in it, moves to it.
  function close(item) {
    const group = groupOf(item);
    const hadFocus = group.contains(document.activeElement);
    const roving = group.querySelector(`${ITEM}[tabindex="0"]`);
    setOpen(item, false);
    if (hadFocus) {
      focusItem(item);
    } else if (roving) {
      rove(item);
    }
  }

  for (const tree of document.querySelectorAll(TREE)) {
    setUp(tree);
  }
  showCurrent();

  // A tree page's open rows stick below the navigation bar, whose height varies as it wraps.
  const bar = document.querySelector(".site-bar");
  if (bar && document.querySelector(".tree-page")) {
    new ResizeObserver(() => {
      document.documentElement.style.setProperty("--site-bar-height", `${bar.offsetHeight}px`);
    }).observe(bar);
  }

  const main = document.querySelector("main");
  if (main) {
    new MutationObserver(showCurrent).observe(main, { childList: true }); // a page the hash-IRI script moves in
  }

  // The item focused, e.g. by a click, is the roving one. A toggle a click focuses passes the focus to its item, so
  // the keys work. A tree the page gained later, e.g. through the hash-IRI script, is set up once focus first enters
  // it, which may be on a toggle, before the first item.
  document.addEventListener("focusin", (event) => {
    const target = event.target;
    const tree = target.closest?.(TREE);
    if (!tree) {
      return;
    }
    const item = target.matches(TOGGLE) ? itemOfToggle(target) : target.closest(ITEM);
    if (!tree.dataset.roving) {
      setUp(tree);
      const to = item ?? startItem(tree);
      if (to) {
        focusItem(to);
      }
    } else if (item === target) {
      rove(item);
    } else if (item) {
      focusItem(item);
    }
  });

  document.addEventListener("keydown", (event) => {
    const item = event.target.closest?.(ITEM);
    const tree = item?.closest(TREE);
    if (!tree || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) {
      return;
    }
    const group = item.hasAttribute("aria-expanded") ? groupOf(item) : null;
    let to = null;
    switch (event.key) {
      case "ArrowDown":
        to = nextShown(item);
        break;
      case "ArrowUp":
        to = previousShown(item);
        break;
      case "Home":
        to = firstItem(tree);
        break;
      case "End":
        to = lastShownIn(tree.lastElementChild);
        break;
      case "ArrowRight":
        if (group && !isOpen(item)) {
          open(item);
        } else if (group) {
          to = group.querySelector(ITEM);
        }
        break;
      case "ArrowLeft":
        if (group && isOpen(item)) {
          close(item);
        } else {
          const parent = parentLi(liOf(item));
          to = parent && itemOf(parent);
        }
        break;
      default:
        return; // Enter follows the link: the entity's page, or the next page of nodes
    }
    event.preventDefault();
    if (to) {
      focusItem(to);
    }
  });

  // A toggle whose children are loaded opens or closes them in place, with no request.
  document.addEventListener("htmx:confirm", (event) => {
    const toggle = toggleOfRequest(event);
    if (!toggle) {
      return;
    }
    const item = itemOfToggle(toggle);
    if (groupOf(item).hasAttribute("data-loaded")) {
      event.preventDefault();
      if (isOpen(item)) {
        close(item);
      } else {
        open(item);
      }
    }
  });

  for (const name of ["htmx:beforeRequest", "htmx:afterRequest"]) {
    document.addEventListener(name, (event) => {
      const toggle = toggleOfRequest(event);
      if (toggle) {
        groupOf(itemOfToggle(toggle)).setAttribute("aria-busy", String(name === "htmx:beforeRequest"));
      }
    });
  }

  // "Show more" replaces itself with the next page; where it had the focus, the page's first item takes it.
  let shownMore = null; // where the page goes, while it's swapped in
  let siblingsFocused = false; // whether "Show its siblings", which they replace, had the focus
  document.addEventListener("htmx:beforeSwap", (event) => {
    const siblings = siblingsOfRequest(event);
    if (siblings) {
      siblingsFocused = document.activeElement === siblings;
    }
    const more = moreOfRequest(event);
    if (more) {
      const li = more.parentElement;
      shownMore = { list: li.parentElement, before: li.previousElementSibling, focused: document.activeElement === more };
    }
  });

  document.addEventListener("htmx:afterSwap", (event) => {
    if (siblingsOfRequest(event)) {
      shownSiblings(event.detail.target);
      return;
    }
    const toggle = toggleOfRequest(event);
    if (toggle) {
      const item = itemOfToggle(toggle);
      const group = groupOf(item);
      group.setAttribute("data-loaded", "");
      if (item.closest(TREE)?.dataset.roving) {
        tame(group);
      }
      setOpen(item, true);
      return;
    }
    if (!moreOfRequest(event) || !shownMore) {
      return;
    }
    const { list, before, focused } = shownMore;
    shownMore = null;
    if (list.closest(TREE)?.dataset.roving) {
      tame(list);
    }
    const first = before ? before.nextElementSibling : list.firstElementChild;
    const item = first && itemOf(first);
    const tree = list.closest(TREE);
    if (focused && item) {
      focusItem(item);
    } else if (tree?.dataset.roving && !tree.querySelector(`${ITEM}[tabindex="0"]`)) {
      const to = item ?? firstItem(tree); // "Show more" was the roving item
      if (to) {
        rove(to);
      }
    }
  });

  // The page's entity's siblings, in place of its first parent's children shown: the entity, among them, takes the
  // focus if "Show its siblings" had it, or the roving tabindex if that was it, and stays in view.
  function shownSiblings(list) {
    const tree = list.closest(TREE);
    const current = list.querySelector(`${ITEM}[aria-current="page"]`);
    if (tree?.dataset.roving) {
      tame(list);
      if (current && (siblingsFocused || !tree.querySelector(`${ITEM}[tabindex="0"]`))) {
        rove(current);
      }
    }
    if (current && siblingsFocused) {
      current.focus({ preventScroll: true });
    }
    siblingsFocused = false;
    showCurrent();
  }

  // Children that couldn't be fetched (e.g. the site is loading, or the network failed) are left to the page that
  // shows them.
  for (const name of ["htmx:responseError", "htmx:sendError"]) {
    document.addEventListener(name, (event) => {
      const link = toggleOfRequest(event) ?? moreOfRequest(event) ?? siblingsOfRequest(event);
      if (link) {
        location.assign(link.href);
      }
    });
  }
})();
