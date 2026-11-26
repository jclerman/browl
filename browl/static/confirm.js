// Forms that ask before they're sent (`form[data-confirm]`), such as the admins page's Remove: declining the question
// cancels the submission. Without JS, the form is sent at once.
document.addEventListener("submit", (event) => {
  const question = event.target.closest?.("form[data-confirm]")?.dataset.confirm;
  if (question && !window.confirm(question)) {
    event.preventDefault();
  }
});
