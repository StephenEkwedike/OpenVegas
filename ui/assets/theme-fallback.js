(function () {
  var THEME_KEY = "ov_theme";
  var TOGGLE_ID = "ov-theme-toggle";
  var NAV_ID = "siteNav";
  var NAV_LINKS = [
    ["/ui", "Home"],
    ["/ui/how-it-works", "How it works"],
    ["/ui/pricing", "Pricing"],
    ["/ui/balance", "Balance"],
    ["/ui/faq", "FAQ"],
    ["/ui/how-to-play", "HOW TO PLAY"],
    ["/ui/contact", "Contact"]
  ];

  function normalizeTheme(value) {
    return String(value || "").toLowerCase() === "dark" ? "dark" : "light";
  }

  function applyFaviconTheme(theme) {
  var normalized = normalizeTheme(theme);
  var isDark = normalized === "dark";
  var svgBase = isDark ? "/ui/assets/brand/favicon-dark.svg" : "/ui/assets/brand/favicon.svg";
  var jpgBase = isDark ? "/ui/assets/brand/favicon-dark.jpg" : "/ui/assets/brand/favicon.jpg";
  var appleBase = isDark ? "/ui/assets/brand/openvegas-logo-circle-dark.jpg" : "/ui/assets/brand/openvegas-logo-circle.jpg";
  var svgHref = svgBase + "?theme=" + normalized;
  var jpgHref = jpgBase + "?theme=" + normalized;
  var appleHref = appleBase + "?theme=" + normalized;

  function refreshLink(node, href) {
    if (!node) return;
    node.setAttribute("href", href);
    var clone = node.cloneNode(true);
    if (node.parentNode) node.parentNode.replaceChild(clone, node);
  }

  function ensureLink(rel, type, href) {
    var selector = 'link[rel="' + rel + '"]' + (type ? '[type="' + type + '"]' : "");
    var node = document.querySelector(selector);
    if (!node) {
      node = document.createElement("link");
      node.setAttribute("rel", rel);
      if (type) node.setAttribute("type", type);
      node.setAttribute("href", href);
      document.head.appendChild(node);
    }
    return node;
  }

  Array.prototype.forEach.call(
    document.querySelectorAll('link[rel="alternate icon"][type="image/jpeg"]'),
    function (node) {
      node.setAttribute("rel", "icon");
    }
  );

  var svg = ensureLink("icon", "image/svg+xml", svgHref);
  refreshLink(svg, svgHref);

  var jpg = ensureLink("icon", "image/jpeg", jpgHref);
  refreshLink(jpg, jpgHref);

  var shortcut = ensureLink("shortcut icon", "image/jpeg", jpgHref);
  refreshLink(shortcut, jpgHref);

  var apple = ensureLink("apple-touch-icon", null, appleHref);
  refreshLink(apple, appleHref);
}

function getTheme() {
    try {
      var stored = window.localStorage.getItem(THEME_KEY);
      return normalizeTheme(stored || "light");
    } catch (_) {
      return "light";
    }
  }

  function setTheme(theme) {
    var normalized = normalizeTheme(theme);
    document.documentElement.dataset.theme = normalized;
    document.documentElement.style.colorScheme = normalized;
  applyFaviconTheme(normalized);
  }

  function persistTheme(theme) {
    try {
      window.localStorage.setItem(THEME_KEY, normalizeTheme(theme));
    } catch (_) {
      // no-op
    }
  }

  function currentTheme() {
    return normalizeTheme(document.documentElement.dataset.theme || "light");
  }

  function setToggleState(toggle) {
    if (!toggle) return;
    var dark = currentTheme() === "dark";
    toggle.setAttribute("aria-checked", String(dark));
    toggle.setAttribute("title", dark ? "Switch to light mode" : "Switch to dark mode");
  }

  function bindToggle(toggle) {
    if (!toggle) return;
    if (toggle.dataset.ovThemeBound === "1") return;
    toggle.addEventListener("click", function () {
      var next = currentTheme() === "light" ? "dark" : "light";
      setTheme(next);
      persistTheme(next);
      setToggleState(toggle);
    });
    toggle.dataset.ovThemeBound = "1";
  }

  function ensureNavFallback() {
    var nav = document.getElementById(NAV_ID);
    if (!nav) return;
    var current = window.location.pathname;
    var anchors = nav.querySelectorAll("a[href]");
    if (anchors.length) {
      for (var i = 0; i < anchors.length; i += 1) {
        var link = anchors[i];
        var href = link.getAttribute("href") || "";
        if (href === current) link.classList.add("active");
        else link.classList.remove("active");
      }
      return;
    }
    nav.innerHTML = NAV_LINKS.map(function (pair) {
      var href = pair[0];
      var label = pair[1];
      var active = current === href ? "active" : "";
      return '<a href="' + href + '" class="' + active + '">' + label + "</a>";
    }).join("");
  }

  function buildToggle() {
    var button = document.createElement("button");
    button.type = "button";
    button.id = TOGGLE_ID;
    button.className = "theme-toggle";
    button.setAttribute("role", "switch");
    button.setAttribute("aria-label", "Toggle light and dark mode");
    button.innerHTML =
      '<span class="theme-toggle__icon" aria-hidden="true">☀</span>' +
      '<span class="theme-toggle__rail" aria-hidden="true"><span class="theme-toggle__thumb"></span></span>' +
      '<span class="theme-toggle__icon" aria-hidden="true">☾</span>';
    bindToggle(button);
    return button;
  }

  function ensureToggle() {
    var existing = document.getElementById(TOGGLE_ID);
    var topNav = document.querySelector(".top-nav");
    var preferredHost =
      (topNav && (topNav.querySelector(".founder-links") || topNav.querySelector(".nav-links")) && (topNav.querySelector(".founder-links") || topNav.querySelector(".nav-links")).parentElement) ||
      topNav ||
      document.body ||
      document.documentElement;
    if (!preferredHost) return;

    var toggle = existing || buildToggle();
    if (!existing || toggle.parentElement !== preferredHost) {
      preferredHost.appendChild(toggle);
    }

    if (preferredHost === document.body || preferredHost === document.documentElement) {
      toggle.classList.add("theme-toggle--floating");
    } else {
      toggle.classList.remove("theme-toggle--floating");
    }

    bindToggle(toggle);
    setToggleState(toggle);
  }

  setTheme(getTheme());

  document.addEventListener("DOMContentLoaded", function () {
    ensureNavFallback();
    ensureToggle();
  });

  window.addEventListener("storage", function (event) {
    if (event.key !== THEME_KEY) return;
    var next = normalizeTheme(event.newValue || "light");
    setTheme(next);
    setToggleState(document.getElementById(TOGGLE_ID));
  });
})();
