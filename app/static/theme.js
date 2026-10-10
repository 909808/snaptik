/* Applies the saved (or system) colour theme before first paint so the page never flashes. */
(function () {
  var KEY = "snaptik-theme";
  var root = document.documentElement;
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function saved() {
    try {
      var value = localStorage.getItem(KEY);
      return value === "light" || value === "dark" ? value : null;
    } catch (e) {
      return null; // storage can be blocked (private mode, strict settings)
    }
  }

  root.dataset.theme = saved() || (media && media.matches ? "dark" : "light");

  if (media && media.addEventListener) {
    media.addEventListener("change", function (event) {
      if (!saved()) root.dataset.theme = event.matches ? "dark" : "light";
    });
  }
})();
