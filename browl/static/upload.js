// The upload form's in-page upload (P-31): sends the form from the page, showing how much of the file has been sent,
// with the Upload button disabled until the response comes, so the upload can't be sent twice. A successful upload's
// response is the redirect to the upload page, which then loads, as after the plain form. A refused upload's page
// takes the place of this one's content, polling while a job runs, as the plain form's would. The status doesn't poll
// while the form is sent, so a poll that finds the refused upload's job ended can't load the page afresh, losing the
// refusal. Without JS, the form posts as usual (U-17).
(() => {
  "use strict";
  const FORM = "form.upload";
  const UNITS = ["bytes", "kB", "MB", "GB", "TB"];
  let sending = false; // the form is being sent

  // A size in decimal units, as the status shows them (Jinja's `filesizeformat`), e.g. "1.6 GB".
  function formatSize(bytes) {
    let unit = 0;
    while (bytes >= 1000 && unit < UNITS.length - 1) {
      bytes /= 1000;
      unit += 1;
    }
    return unit === 0 ? `${bytes} bytes` : `${bytes.toFixed(1)} ${UNITS[unit]}`;
  }

  function send(form) {
    const button = form.querySelector('button[type="submit"]');
    const panel = form.querySelector(".upload-progress");
    const bar = panel.querySelector("progress");
    const text = panel.querySelector("p");
    const file = form.querySelector('input[type="file"]').files[0];
    const size = file?.size;

    function show(sent) {
      const percent = size ? Math.min(100, Math.round((100 * sent) / size)) : null;
      if (percent === null) {
        bar.removeAttribute("value");
      } else {
        bar.value = percent;
      }
      const of = size ? ` of ${formatSize(size)} (${percent}%)` : "";
      text.textContent = `Sending the file: ${formatSize(sent)}${of}`;
      panel.hidden = false;
    }

    // Show why the upload failed where the server's refusal would be, and let it be tried again.
    function fail(message) {
      panel.hidden = true;
      let error = document.getElementById("upload-error");
      if (!error) {
        error = document.createElement("p");
        error.id = "upload-error";
        error.className = "failure";
        error.setAttribute("role", "alert");
        form.before(error);
      }
      error.textContent = message;
      button.disabled = false;
      sending = false;
    }

    const data = new FormData(form); // in the form's order: the reasoner before the file, as the server needs
    const request = new XMLHttpRequest();
    request.open("POST", form.action);
    // The body's size is a little more than the file's, so this counts the file's share of what's been sent.
    request.upload.addEventListener("progress", (progress) => {
      show(progress.lengthComputable && size ? Math.max(0, size - (progress.total - progress.loaded)) : 0);
    });
    request.upload.addEventListener("load", () => {
      bar.value = 100;
      text.textContent = `Sent ${formatSize(size ?? 0)}; waiting for the server…`;
    });
    request.addEventListener("load", () => {
      sending = false;
      if (request.status < 400) {
        // The redirect was followed; load its page, as the plain form would.
        window.location.assign(request.responseURL || form.action);
        return;
      }
      const main = new DOMParser().parseFromString(request.responseText, "text/html").querySelector("main");
      if (main?.querySelector("#upload-error")) {
        document.querySelector("main").replaceWith(main);
        window.htmx?.process(main);
      } else {
        fail(`The upload failed: HTTP ${request.status}.`);
      }
    });
    request.addEventListener("error", () => fail("The upload failed: the connection to the server was lost."));
    button.disabled = true;
    sending = true;
    show(0);
    request.send(data);
  }

  // The status's polls wait until the form is sent: htmx tries again at the next poll.
  document.addEventListener("htmx:beforeRequest", (event) => {
    if (sending && event.target.closest?.("#job-status")) {
      event.preventDefault();
    }
  });

  // On the document, so it serves a form that a refused upload's page put in place.
  document.addEventListener("submit", (event) => {
    const form = event.target.closest?.(FORM);
    if (!form) {
      return;
    }
    event.preventDefault();
    if (!form.querySelector('button[type="submit"]').disabled) {
      send(form);
    }
  });
})();
