// Small progressive enhancements; the portal works without JavaScript.
document.addEventListener("click", (e) => {
  const confirmBtn = e.target.closest("[data-confirm]");
  if (confirmBtn && !window.confirm(confirmBtn.dataset.confirm)) {
    e.preventDefault();
    return;
  }
  const copyBtn = e.target.closest("[data-copy]");
  if (copyBtn) {
    const el = document.querySelector(copyBtn.dataset.copy);
    if (el && navigator.clipboard) {
      navigator.clipboard.writeText(el.textContent.trim()).then(() => {
        const old = copyBtn.textContent;
        copyBtn.textContent = "Copied";
        setTimeout(() => (copyBtn.textContent = old), 1500);
      });
    }
  }
});
document.addEventListener("change", (e) => {
  const sel = e.target.closest("[data-custom-toggle]");
  if (!sel) return;
  const date = sel.parentElement.querySelector(".custom-date");
  if (date) {
    date.hidden = sel.value !== "custom";
    date.required = sel.value === "custom";
  }
});
