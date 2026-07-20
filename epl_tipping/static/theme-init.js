(() => {
  const storageKey = "epl-tipping-theme";
  let savedTheme = null;
  try {
    savedTheme = window.localStorage.getItem(storageKey);
  } catch (_) {
    savedTheme = null;
  }
  const systemTheme = window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
  document.documentElement.dataset.theme = ["light", "dark"].includes(savedTheme)
    ? savedTheme
    : systemTheme;
})();
