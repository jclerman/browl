// Hierarchy trees (DESIGN.md §2.9, §2.13, U-17): opening and closing nodes in place, and the WAI-ARIA tree pattern's
// keyboard use. A node's toggle fetches its children with htmx the first time it opens (`tree_nodes.html`); after
// that, it's opened and closed here, without a request. It listens on the document, so it also serves trees that
// htmx or the hash-IRI script add.
(() => {
  "use strict";
  const TREE = '[role="tree"]';
  const ITEM = '[role="treeitem"]';
  const TOGGLE = ".tree-toggle";

  const groupOf = (item) => item.parentElement.querySelector(':scope > [role="group"]');
  const toggleOf = (item) => item.parentElement.querySelector(`:scope > ${TOGGLE}`);
  const itemOf = (li) => li.querySelector(`:scope > ${ITEM}`);
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
    for (let li = item.parentElement; li; li = parentLi(li)) {
      if (li.nextElementSibling) {
        return itemOf(li.nextElementSibling);
      }
    }
    return null;
  }

  function previousShown(item) {
    const li = item.parentElement;
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
    const first = firstItem(tree);
    if (first) {
      first.tabIndex = 0;
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

  // The item focused, e.g. by a click, is the roving one. A toggle a click focuses passes the focus to its item, so
  // the keys work. A tree the page gained later, e.g. through the hash-IRI script, is set up once focus first enters
  // it, which may be on a toggle, before the first item.
  document.addEventListener("focusin", (event) => {
    const target = event.target;
    const tree = target.closest?.(TREE);
    if (!tree) {
      return;
    }
    const item = target.matches(TOGGLE) ? itemOf(target.parentElement) : target.closest(ITEM);
    if (!tree.dataset.roving) {
      setUp(tree);
      const to = item ?? firstItem(tree);
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
          const parent = parentLi(item.parentElement);
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
    const item = itemOf(toggle.parentElement);
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
        groupOf(itemOf(toggle.parentElement)).setAttribute("aria-busy", String(name === "htmx:beforeRequest"));
      }
    });
  }

  // "Show more" replaces itself with the next page; where it had the focus, the page's first item takes it.
  let shownMore = null; // where the page goes, while it's swapped in
  document.addEventListener("htmx:beforeSwap", (event) => {
    const more = moreOfRequest(event);
    if (more) {
      const li = more.parentElement;
      shownMore = { list: li.parentElement, before: li.previousElementSibling, focused: document.activeElement === more };
    }
  });

  document.addEventListener("htmx:afterSwap", (event) => {
    const toggle = toggleOfRequest(event);
    if (toggle) {
      const item = itemOf(toggle.parentElement);
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

  // Children that couldn't be fetched (e.g. the site is loading, or the network failed) are left to the page that
  // shows them.
  for (const name of ["htmx:responseError", "htmx:sendError"]) {
    document.addEventListener(name, (event) => {
      const link = toggleOfRequest(event) ?? moreOfRequest(event);
      if (link) {
        location.assign(link.href);
      }
    });
  }
})();
