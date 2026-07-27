// Vanilla-JS i18n mechanism mirroring sales_copilot.core.i18n (see
// claudedocs/2026-07-24-dashboard-i18n-design.md for the full design):
// dotted-key catalog resolution with {name} interpolation, falling back to
// the default language (nl) when a key or catalog is missing. No build step:
// catalogs are plain JSON files under dashboard/i18n/, served by the same
// static mount as the rest of the dashboard.
//
// Active language comes from the shared /api/config fetch (window.__configPromise,
// set up in index.html) so the dashboard always mirrors the backend LANGUAGE
// env var instead of tracking a second config source. nl remains a fully
// supported first-class toggle -- only the default/fallback direction is nl.
(() => {
const DEFAULT_LANGUAGE = "nl";
const catalogPath = (lang) => `i18n/${lang}.json`;

const catalogs = {};
let activeLang = DEFAULT_LANGUAGE;

const fetchCatalog = async (lang) => {
  try {
    const response = await fetch(catalogPath(lang));
    if (!response.ok) {
      return {};
    }
    const data = await response.json();
    return data && typeof data === "object" ? data : {};
  } catch (error) {
    console.warn(`i18n: could not load catalog for "${lang}"`, error);
    return {};
  }
};

const resolvePath = (catalog, key) => {
  let node = catalog;
  for (const part of key.split(".")) {
    if (node == null || typeof node !== "object" || !(part in node)) {
      return undefined;
    }
    node = node[part];
  }
  return typeof node === "string" ? node : undefined;
};

const interpolate = (template, params) => {
  if (!params) {
    return template;
  }
  return template.replace(/\{(\w+)\}/g, (match, name) => (
    Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match
  ));
};

/**
 * Resolve `key` (dotted path, e.g. "coaching.suggestion_empty_state") for the
 * active language, falling back to the default language, then to the key
 * itself. Mirrors sales_copilot.core.i18n.t()'s resolution order.
 */
const t = (key, params) => {
  const active = catalogs[activeLang] || {};
  const fallback = catalogs[DEFAULT_LANGUAGE] || {};
  const value = resolvePath(active, key) ?? resolvePath(fallback, key);
  if (value === undefined) {
    console.warn(`i18n: missing message key "${key}" (lang=${activeLang})`);
    return key;
  }
  return interpolate(value, params);
};

const applyDataI18n = () => {
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const key = el.getAttribute("data-i18n");
    if (key) {
      el.textContent = t(key);
    }
  });

  document.querySelectorAll("[data-i18n-attr]").forEach((el) => {
    const spec = el.getAttribute("data-i18n-attr") || "";
    spec.split(",").forEach((pair) => {
      const separatorIndex = pair.indexOf(":");
      if (separatorIndex === -1) {
        return;
      }
      const attr = pair.slice(0, separatorIndex).trim();
      const key = pair.slice(separatorIndex + 1).trim();
      if (attr && key) {
        el.setAttribute(attr, t(key));
      }
    });
  });
};

const resolveActiveLanguage = async () => {
  try {
    const config = await (window.__configPromise || Promise.resolve({}));
    const lang = config && typeof config.language === "string" ? config.language.trim().toLowerCase() : "";
    return lang || DEFAULT_LANGUAGE;
  } catch (error) {
    return DEFAULT_LANGUAGE;
  }
};

const whenDomReady = () => new Promise((resolve) => {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", resolve, { once: true });
  } else {
    resolve();
  }
});

// Resolved once the active language + catalogs are loaded and data-i18n /
// data-i18n-attr elements have been translated. Future callers that need
// t() before the DOM is hydrated should `await window.SalesCopilotI18n.ready`.
const ready = (async () => {
  activeLang = await resolveActiveLanguage();
  catalogs[DEFAULT_LANGUAGE] = await fetchCatalog(DEFAULT_LANGUAGE);
  catalogs[activeLang] = activeLang === DEFAULT_LANGUAGE
    ? catalogs[DEFAULT_LANGUAGE]
    : await fetchCatalog(activeLang);
  await whenDomReady();
  applyDataI18n();
})();

window.SalesCopilotI18n = { t, applyDataI18n, ready, DEFAULT_LANGUAGE };
window.t = t;
})();
