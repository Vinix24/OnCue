/**
 * Minimal DOM + browser-globals stub for testing dashboard/js/*.js.
 *
 * The dashboard is vanilla HTML/CSS/JS with no build step, and
 * scripts/check_architecture_boundaries.py enforces that: no package.json, no
 * node_modules, no framework. So this harness adds no dependency of any kind.
 * It runs each dashboard module inside node:vm with a hand-rolled DOM under
 * it, and asserts on the DOM the module actually produced.
 *
 * Every dashboard module is an IIFE that binds its DOM on load, so tests drive
 * them the way a browser does -- build the elements, load the script, push a
 * message into the stubbed WebSocket or click a stubbed button -- rather than
 * through a test-only export. Nothing in dashboard/js/ knows this file exists.
 *
 * Only the surface the dashboard modules genuinely use is implemented. When a
 * module starts using something this stub lacks, the test fails loudly instead
 * of silently passing on undefined.
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const HERE = dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = resolve(HERE, "..", "..");

/* ---------------------------------------------------------------- elements */

const SELECTOR_PART = /^([a-zA-Z][\w-]*)?((?:[.#][\w-]+|\[[\w-]+\])*)$/;

const camel = (value) => value.replace(/-([a-z])/g, (_match, char) => char.toUpperCase());
const dashed = (value) => value.replace(/[A-Z]/g, (char) => `-${char.toLowerCase()}`);

class ClassList {
  constructor(element) {
    this._element = element;
  }

  get _tokens() {
    return String(this._element.className || "").split(/\s+/).filter(Boolean);
  }

  _write(tokens) {
    this._element.className = tokens.join(" ");
  }

  add(...names) {
    const tokens = this._tokens;
    names.forEach((name) => {
      if (!tokens.includes(name)) {
        tokens.push(name);
      }
    });
    this._write(tokens);
  }

  remove(...names) {
    this._write(this._tokens.filter((token) => !names.includes(token)));
  }

  contains(name) {
    return this._tokens.includes(name);
  }

  toggle(name, force) {
    const shouldHave = force === undefined ? !this.contains(name) : !!force;
    if (shouldHave) {
      this.add(name);
    } else {
      this.remove(name);
    }
    return shouldHave;
  }
}

class Element {
  constructor(tagName) {
    this.tagName = String(tagName).toUpperCase();
    this.className = "";
    this.attributes = {};
    // In a browser, dataset and the data-* attributes are one store: writing
    // `el.dataset.role` makes `[data-role]` match. A plain object would let a
    // module set dataset and a querySelectorAll("[data-role]") silently miss it,
    // so writes mirror into attributes the way the DOM does.
    this.dataset = new Proxy(
      {},
      {
        set: (target, key, value) => {
          target[key] = String(value);
          this.attributes[`data-${dashed(String(key))}`] = String(value);
          return true;
        },
        deleteProperty: (target, key) => {
          delete target[key];
          delete this.attributes[`data-${dashed(String(key))}`];
          return true;
        },
      },
    );
    this.style = {};
    this.children = [];
    this.parentNode = null;
    this.listeners = new Map();
    this._text = "";
    this.checked = false;
    this.disabled = false;
    this.value = "";
    this.type = "";
    this.title = "";
    this.classList = new ClassList(this);
  }

  get id() {
    return this.attributes.id || "";
  }

  set id(value) {
    this.attributes.id = value;
  }

  get textContent() {
    if (this.children.length === 0) {
      return this._text;
    }
    return this.children.map((child) => child.textContent).join("");
  }

  set textContent(value) {
    this.children.forEach((child) => {
      child.parentNode = null;
    });
    this.children = [];
    this._text = value === null || value === undefined ? "" : String(value);
  }

  get innerHTML() {
    return this.children.length ? "<children>" : this._text;
  }

  set innerHTML(value) {
    // Dashboard modules only assign "" (clear) or one simple element string.
    // Anything richer would need a real parser, which this stub deliberately
    // does not grow: a module doing that should be caught in review.
    this.textContent = value ? String(value) : "";
  }

  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    this._text = "";
    return child;
  }

  prepend(child) {
    child.parentNode = this;
    this.children.unshift(child);
    this._text = "";
    return child;
  }

  get firstChild() {
    return this.children[0] || null;
  }

  insertBefore(child, reference) {
    child.parentNode = this;
    this._text = "";
    if (reference === null || reference === undefined) {
      this.children.push(child);
      return child;
    }
    const index = this.children.indexOf(reference);
    if (index === -1) {
      // The browser throws NotFoundError here. Throwing too keeps a module that
      // passes a stale reference failing loudly instead of appending quietly.
      throw new Error("dom_stub: insertBefore reference is not a child of this node");
    }
    this.children.splice(index, 0, child);
    return child;
  }

  removeChild(child) {
    this.children = this.children.filter((existing) => existing !== child);
    child.parentNode = null;
    return child;
  }

  // report.js removes its temporary download link this way after the click.
  remove() {
    if (this.parentNode) {
      this.parentNode.removeChild(this);
    }
  }

  replaceChildren(...nodes) {
    this.children.forEach((child) => {
      child.parentNode = null;
    });
    this.children = [];
    this._text = "";
    nodes.forEach((node) => this.appendChild(node));
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name.startsWith("data-")) {
      this.dataset[camel(name.slice(5))] = String(value);
    }
  }

  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) {
      this.listeners.set(type, []);
    }
    this.listeners.get(type).push(handler);
  }

  /** Fire every listener registered for `type` -- the tests' input verb. */
  fire(type, event = {}) {
    (this.listeners.get(type) || []).forEach((handler) => handler({ type, target: this, ...event }));
  }

  click() {
    this.fire("click");
  }

  querySelector(selector) {
    return queryAll(this, selector)[0] || null;
  }

  querySelectorAll(selector) {
    return queryAll(this, selector);
  }
}

const descendants = (node) => {
  const out = [];
  const walk = (current) => {
    current.children.forEach((child) => {
      out.push(child);
      walk(child);
    });
  };
  walk(node);
  return out;
};

const matchesPart = (element, part) => {
  const parsed = SELECTOR_PART.exec(part);
  if (!parsed) {
    throw new Error(`dom_stub: unsupported selector part "${part}"`);
  }
  const [, tag, rest] = parsed;
  if (tag && element.tagName !== tag.toUpperCase()) {
    return false;
  }
  const tokens = (rest || "").match(/[.#][\w-]+|\[[\w-]+\]/g) || [];
  return tokens.every((token) => {
    if (token.startsWith(".")) {
      return element.classList.contains(token.slice(1));
    }
    if (token.startsWith("#")) {
      return element.id === token.slice(1);
    }
    return element.getAttribute(token.slice(1, -1)) !== null;
  });
};

const queryAll = (root, selector) => {
  const parts = String(selector).trim().split(/\s+/);
  let current = [root];
  parts.forEach((part) => {
    const next = [];
    current.forEach((node) => {
      descendants(node).forEach((element) => {
        if (matchesPart(element, part) && !next.includes(element)) {
          next.push(element);
        }
      });
    });
    current = next;
  });
  return current;
};

/** Build an element with id/class/dataset/children in one call. */
export function el(tag, { id, className, dataset = {}, attributes = {}, children = [], ...props } = {}) {
  const element = new Element(tag);
  if (id) {
    element.id = id;
  }
  if (className) {
    element.className = className;
  }
  Object.assign(element.dataset, dataset);
  Object.entries(attributes).forEach(([name, value]) => element.setAttribute(name, value));
  Object.assign(element, props);
  children.forEach((child) => element.appendChild(child));
  return element;
}

/* ------------------------------------------------------------- environment */

/**
 * Build a browsing context: a document, a window (the vm global), storage,
 * controllable timers and clock, a fetch stub and a WebSocket stub.
 */
export function createEnvironment({ routes = {}, storage = {}, language = "nl" } = {}) {
  const root = new Element("html");
  const body = root.appendChild(new Element("body"));
  const timers = { timeouts: [], intervals: [] };
  const clock = { now: 1700000000000 };
  const sockets = [];
  const missingI18nKeys = [];

  const document = {
    readyState: "complete",
    body,
    documentElement: root,
    createElement: (tag) => new Element(tag),
    getElementById: (id) => descendants(root).find((element) => element.id === id) || null,
    querySelector: (selector) => queryAll(root, selector)[0] || null,
    querySelectorAll: (selector) => queryAll(root, selector),
    addEventListener: () => {},
  };

  const store = { ...storage };
  const localStorage = {
    getItem: (key) => (Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null),
    setItem: (key, value) => {
      store[key] = String(value);
    },
    removeItem: (key) => {
      delete store[key];
    },
  };

  const catalog = JSON.parse(
    readFileSync(resolve(REPO_ROOT, "dashboard", "i18n", `${language}.json`), "utf8"),
  );

  // Mirrors dashboard/js/i18n.js: dotted-path resolution with {name}
  // interpolation, against the real shipped catalog. So a string a test
  // asserts on is the string the dashboard actually renders, and a key a
  // module forgot to add to the catalog is recorded here instead of shipping
  // to a seller as a raw dotted key.
  const t = (key, params) => {
    let node = catalog;
    for (const segment of String(key).split(".")) {
      if (node === null || typeof node !== "object" || !(segment in node)) {
        missingI18nKeys.push(key);
        return key;
      }
      node = node[segment];
    }
    if (typeof node !== "string") {
      missingI18nKeys.push(key);
      return key;
    }
    if (!params) {
      return node;
    }
    return node.replace(/\{(\w+)\}/g, (match, name) => (
      Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match
    ));
  };

  class FakeWebSocket {
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      this.listeners = new Map();
      sockets.push(this);
    }

    addEventListener(type, handler) {
      if (!this.listeners.has(type)) {
        this.listeners.set(type, []);
      }
      this.listeners.get(type).push(handler);
    }

    /** Deliver one server -> client message, exactly as the hub would. */
    deliver(payload) {
      const data = JSON.stringify(payload);
      (this.listeners.get("message") || []).forEach((handler) => handler({ data }));
    }

    send() {}

    close() {
      this.readyState = 3;
    }
  }
  FakeWebSocket.OPEN = 1;

  // Controllable clock: the detector-status staleness rule is "no payload for
  // three publish intervals", which is only testable if the test owns time.
  class FakeDate extends Date {
    static now() {
      return clock.now;
    }
  }

  const context = {
    console,
    document,
    localStorage,
    sessionStorage: localStorage,
    location: { host: "localhost:8760", href: "http://localhost:8760/" },
    Date: FakeDate,
    WebSocket: FakeWebSocket,
    MutationObserver: class {
      observe() {}
    },
    setTimeout: (fn, delay) => {
      timers.timeouts.push({ fn, delay });
      return timers.timeouts.length;
    },
    clearTimeout: () => {},
    setInterval: (fn, delay) => {
      timers.intervals.push({ fn, delay });
      return timers.intervals.length;
    },
    clearInterval: () => {},
    fetch: async (url) => {
      const key = Object.keys(routes).find((route) => String(url).includes(route));
      if (key === undefined) {
        throw new Error(`fetch: no stubbed route for ${url}`);
      }
      return { ok: true, json: async () => routes[key] };
    },
    alert: () => {},
    t,
    copilotAuthReady: Promise.resolve(),
    copilotAuthHeaders: () => ({}),
    SalesCopilotI18n: { t, ready: Promise.resolve(), DEFAULT_LANGUAGE: "nl" },
  };
  context.window = context;
  vm.createContext(context);

  return {
    context,
    document,
    root,
    body,
    timers,
    sockets,
    storage: store,
    missingI18nKeys,
    /** Append `element` to <body>. */
    mount(element) {
      return body.appendChild(element);
    },
    /** Run a dashboard module in this context, the way a <script> tag would. */
    load(relativePath) {
      const file = resolve(REPO_ROOT, relativePath);
      vm.runInContext(readFileSync(file, "utf8"), context, { filename: file });
    },
    /** Move the clock forward without running timers. */
    advanceClock(ms) {
      clock.now += ms;
    },
    /** Fire every registered interval callback once (the staleness clock). */
    tickIntervals() {
      timers.intervals.forEach(({ fn }) => fn());
    },
  };
}

/** Let queued promise callbacks (fetch chains, .then ladders) run out. */
export async function settle(rounds = 8) {
  for (let index = 0; index < rounds; index += 1) {
    await new Promise((done) => setImmediate(done));
  }
}

export { Element };
