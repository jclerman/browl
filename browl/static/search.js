// The navigation bar's search box: the WAI-ARIA combobox pattern's keys over the suggestions htmx fetches (U-10,
// U-17; DESIGN.md §2.9, Search). Without it, the box is a plain form that opens the full results page.
(() => {
  "use strict";
  const BOX = "site-search-box";
  const LIST = "site-search-list";
  const STATUS = "site-search-status";
  // Whether Escape closed the list since the box's text last changed: an answer to a request already pending, or
  // still waiting out its pause, mustn't reopen it.
  let dismissed = false;

  const parts = () => ({
    box: document.getElementById(BOX),
    list: document.getElementById(LIST),
    status: document.getElementById(STATUS),
  });
  // The options the keys and the pointer reach: not "No matches".
  const options = (list) => [...list.querySelectorAll('[role="option"]:not([aria-disabled="true"])')];
  const active = (box) => {
    const id = box.getAttribute("aria-activedescendant");
    return id ? document.getElementById(id) : null;
  };

  function setActive(box, list, option) {
    for (const each of options(list)) {
      each.setAttribute("aria-selected", each === option ? "true" : "false");
    }
    if (option) {
      box.setAttribute("aria-activedescendant", option.id);
      option.scrollIntoView({ block: "nearest" });
    } else {
      box.removeAttribute("aria-activedescendant");
    }
  }

  function open(box, list) {
    if (list.children.length) {
      list.hidden = false;
      box.setAttribute("aria-expanded", "true");
    }
  }

  function close(box, list) {
    setActive(box, list, null);
    list.hidden = true;
    box.setAttribute("aria-expanded", "false");
  }

  function move(box, list, step) {
    const all = options(list);
    if (!all.length) {
      return;
    }
    open(box, list);
    const index = all.indexOf(active(box));
    // From none, down goes to the first and up to the last; past either end, back to none, in the box.
    const next = index === -1 ? (step > 0 ? 0 : all.length - 1) : index + step;
    setActive(box, list, all[next] ?? null);
  }

  function follow(option) {
    const link = option.querySelector("a[href]");
    if (link) {
      window.location.assign(link.href);
    }
  }

  document.addEventListener("keydown", (event) => {
    const { box, list } = parts();
    if (!box || event.target !== box || event.isComposing) {
      return;
    }
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        if (event.altKey) {
          open(box, list);
        } else {
          move(box, list, 1);
        }
        break;
      case "ArrowUp":
        event.preventDefault();
        move(box, list, -1);
        break;
      case "Enter": {
        const option = list.hidden ? null : active(box);
        if (option) {
          event.preventDefault();
          follow(option);
        }
        break; // else the form opens the full results page
      }
      case "Escape":
        if (!list.hidden) {
          event.preventDefault();
          close(box, list);
          dismissed = true;
        } else if (box.value) {
          event.preventDefault();
          box.value = "";
          list.replaceChildren(); // its options were the old text's
          // As typing would: htmx then asks for the blank text, which replaces any pending request, empties the
          // status, and makes typing the old text again ask for it.
          box.dispatchEvent(new Event("input", { bubbles: true }));
        }
        break;
      default:
    }
  });

  // A new list replaces the old: shown if it has options, with nothing active, and its size announced.
  document.addEventListener("htmx:afterSwap", (event) => {
    const { box, list, status } = parts();
    if (!box || event.detail.target !== list) {
      return;
    }
    setActive(box, list, null);
    if (list.children.length && document.activeElement === box && !dismissed) {
      open(box, list);
    } else {
      close(box, list);
    }
    const hits = options(list).filter((option) => !option.classList.contains("search-all")).length;
    if (!list.children.length) {
      status.textContent = "";
    } else {
      status.textContent = hits ? `${hits} ${hits === 1 ? "suggestion" : "suggestions"}` : "No matches";
    }
  });

  // A click on an option shouldn't take the focus from the box first, which would close the list under it.
  document.addEventListener("mousedown", (event) => {
    const { list } = parts();
    if (list && list.contains(event.target)) {
      event.preventDefault();
    }
  });

  document.addEventListener("input", (event) => {
    const { box } = parts();
    if (box && event.target === box) {
      dismissed = false;
    }
  });

  document.addEventListener("focusout", (event) => {
    const { box, list } = parts();
    if (box && event.target === box) {
      close(box, list);
    }
  });

  document.addEventListener("focusin", (event) => {
    const { box, list } = parts();
    if (box && event.target === box) {
      open(box, list);
    }
  });

  // Hovering marks the option the pointer is over, as the keys would.
  document.addEventListener("mouseover", (event) => {
    const { box, list } = parts();
    const option = list && event.target.closest?.('[role="option"]');
    if (option && options(list).includes(option)) {
      setActive(box, list, option);
    }
  });
})();
