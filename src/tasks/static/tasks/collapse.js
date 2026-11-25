// Klappbare Kacheln: Ein Button mit data-collapse="<name>" schaltet <name> in
// <html data-collapsed="…"> (Liste, durch Leerzeichen getrennt). Das CSS blendet danach aus.
// Der Zustand hängt an <html> statt an der Kachel, weil HTMX die Kachel regelmäßig komplett ersetzt.
// Blockierend im <head> geladen, damit eingeklappte Kacheln beim Laden nicht aufblitzen.
// Namen in DEFAULT_COLLAPSED sind eingeklappt, bis man sie aufklappt; gemerkt wird dann das Aufklappen.
(function () {
  var DEFAULT_COLLAPSED = [];
  var root = document.documentElement;

  function load(key) {
    try { return (localStorage.getItem(key) || "").split(" ").filter(Boolean); } catch (e) { return []; }
  }

  function store(key, names) {
    try {
      if (names.length) localStorage.setItem(key, names.join(" "));
      else localStorage.removeItem(key);
    } catch (e) {}
  }

  function initial() {
    var expanded = load("expanded");
    return load("collapsed").concat(DEFAULT_COLLAPSED.filter(function (name) { return expanded.indexOf(name) === -1; }));
  }

  function save(names) {
    store("collapsed", names.filter(function (name) { return DEFAULT_COLLAPSED.indexOf(name) === -1; }));
    store("expanded", DEFAULT_COLLAPSED.filter(function (name) { return names.indexOf(name) === -1; }));
  }

  function apply(names) {
    if (names.length) root.dataset.collapsed = names.join(" ");
    else delete root.dataset.collapsed;
  }

  function current() {
    return (root.dataset.collapsed || "").split(" ").filter(Boolean);
  }

  // aria-expanded der Buttons an den Zustand anpassen (auch nach jedem HTMX-Tausch)
  function syncButtons(scope) {
    var names = current();
    (scope || document).querySelectorAll("[data-collapse]").forEach(function (button) {
      button.setAttribute("aria-expanded", names.indexOf(button.dataset.collapse) === -1 ? "true" : "false");
    });
  }

  apply(initial());

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-collapse]");
    if (!button) return;
    var name = button.dataset.collapse;
    var names = current();
    var i = names.indexOf(name);
    if (i === -1) names.push(name); else names.splice(i, 1);
    apply(names);
    save(names);
    syncButtons();
  });

  document.addEventListener("DOMContentLoaded", function () { syncButtons(); });
  document.addEventListener("htmx:load", function (event) { syncButtons(event.target); });
})();
