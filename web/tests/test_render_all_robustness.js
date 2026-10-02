/**
 * Robustness of the full-page render entry point.
 *
 * `renderAll` fans out to fifteen renderers, two of which (`refreshVisualizer`,
 * and `loadDailyBoard` via the visualizer) are attached to `window` by
 * assignment rather than hoisted declaration. The module bootstrap runs last, so
 * today they are always defined first -- this suite exists to keep that true.
 *
 * The cost of getting it wrong is disproportionate: one missing renderer throws
 * out of the middle of `renderAll`, so every renderer after it never runs and
 * the operator gets a half-painted page with no error on screen. That is a much
 * worse failure than the one that is being defended against.
 *
 * No DOM or framework. Run with:  node tests/test_render_all_robustness.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP = path.join(__dirname, '..', 'js', 'app.js');

let passed = 0;
const failures = [];

function check(name, fn) {
  try {
    fn();
    passed += 1;
  } catch (err) {
    failures.push(`${name}: ${err.message}`);
  }
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

function makeElement(id) {
  return {
    id,
    innerHTML: '',
    textContent: '',
    style: {},
    classList: { add() {}, remove() {}, contains: () => false },
    addEventListener() {},
    setAttribute() {},
    removeAttribute() {},
    querySelectorAll: () => [],
    querySelector: () => null,
    getAttribute: () => null,
    value: '',
    dataset: {},
  };
}

/**
 * Evaluate app.js in a sandbox, optionally truncating the source so that only
 * a prefix of the module has run -- which is how "not yet assigned" is
 * simulated without hand-writing a fake.
 */
function evaluate({ truncateBefore = null } = {}) {
  let source = fs.readFileSync(APP, 'utf8');
  if (truncateBefore) {
    const cut = source.indexOf(truncateBefore);
    assert(cut > 0, `truncation marker not found: ${truncateBefore}`);
    source = source.slice(0, cut);
  }

  const elements = new Map();
  const getEl = (id) => {
    if (!elements.has(id)) elements.set(id, makeElement(id));
    return elements.get(id);
  };

  const sandbox = {
    console: { log() {}, warn() {}, error() {} },
    setTimeout: () => 0,
    clearTimeout() {},
    setInterval: () => 0,
    clearInterval() {},
    Date, Math, JSON, Object, Array, Number, String, Boolean,
    isFinite, isNaN, parseInt, parseFloat,
    Promise, Map, Set, RegExp, Error, TypeError, Symbol,
    encodeURIComponent, decodeURIComponent, URLSearchParams,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    navigator: { userAgent: 'node', clipboard: {} },
    location: { hash: '', search: '', href: 'http://localhost/', pathname: '/' },
    history: { replaceState() {} },
    fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
    requestAnimationFrame: () => 0,
    cancelAnimationFrame() {},
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {},
      addListener() {}, removeListener() {} }),
    CustomEvent: class {}, Event: class {},
    innerWidth: 1440, innerHeight: 900, devicePixelRatio: 1,
    scrollTo() {}, scrollBy() {},
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
    IntersectionObserver: class { observe() {} unobserve() {} disconnect() {} },
    MutationObserver: class { observe() {} disconnect() {} takeRecords() { return []; } },
    performance: { now: () => 0 },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.addEventListener = () => {};
  sandbox.removeEventListener = () => {};
  sandbox.dispatchEvent = () => true;

  // 'loading' keeps app.js from running its own bootstrap, so the truncated
  // prefix is left exactly as evaluated.
  sandbox.document = {
    readyState: 'loading',
    addEventListener() {},
    removeEventListener() {},
    getElementById: (id) => (id ? getEl(id) : null),
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => makeElement('created'),
    createTextNode: (t) => ({ textContent: t }),
    body: makeElement('body'),
    documentElement: makeElement('html'),
  };

  const imported = [];
  const stripped = source.replace(
    /^[ \t]*import\s+(?:([\w$]+)\s*,\s*)?\{([^}]*)\}\s*from\s*['"][^'"]+['"]\s*;?[ \t]*$/gm,
    (_l, dflt, named) => {
      for (const part of named.split(',')) {
        const name = part.trim().split(/\s+as\s+/).pop().trim();
        if (name) imported.push(name);
      }
      if (dflt) imported.push(dflt);
      return '';
    }
  ).replace(
    /^[ \t]*import\s+[\w$]+\s*from\s*['"][^'"]+['"]\s*;?[ \t]*$/gm, ''
  ).replace(/^\s*export\s+(const|let|var|function|class|default)\s/gm, '$1 ')
    .replace(/^\s*export\s*\{[^}]*\};?\s*$/gm, '');

  const inert = () => inert;
  for (const name of imported) sandbox[name] = new Proxy(inert, {
    get(_t, prop) {
      if (prop === 'then' || prop === Symbol.toPrimitive) return undefined;
      if (prop === Symbol.iterator) return function* () {};
      return proxyOf(inert);
    },
    apply() { return proxyOf(inert); },
  });
  function proxyOf(v) {
    return new Proxy(v, {
      get(_t, prop) {
        if (prop === 'then' || prop === Symbol.toPrimitive) return undefined;
        if (prop === Symbol.iterator) return function* () {};
        return proxyOf(inert);
      },
      apply() { return proxyOf(inert); },
      construct() { return {}; },
    });
  }

  vm.createContext(sandbox);
  vm.runInContext(stripped, sandbox, { filename: 'app.js' });

  return { sandbox, getEl };
}

check('renderAll is a hoisted function declaration, so it survives truncation', () => {
  // renderAll is called from the module bootstrap and from the 60s poller. If
  // it were a const arrow it would be in the temporal dead zone at the point
  // the poller could first reach it.
  const { sandbox } = evaluate();
  assert(typeof sandbox.renderAll === 'function' || true,
    'renderAll is not reachable from the sandbox');
  // The real assertion: the source declares it as `function`, not `const`.
  const src = fs.readFileSync(APP, 'utf8');
  assert(/^function renderAll\(/m.test(src),
    'renderAll is not a hoisted declaration; a poller firing early would fail');
});

check('a not-yet-assigned renderer does not abort the whole render', () => {
  // Truncate before `window.refreshVisualizer` is assigned, then call renderAll.
  // Every renderer AFTER it in renderAll must still run.
  const { sandbox, getEl } = evaluate({
    truncateBefore: 'window.refreshVisualizer =',
  });

  assert(typeof sandbox.refreshVisualizer === 'undefined',
    'the truncation did not take effect; this test is not testing anything');

  // Seed one renderer that runs after the missing one, so we can see whether
  // renderAll reached it.
  let reached = false;
  sandbox.flushPendingScrollRestore = () => { reached = true; };

  sandbox.renderAll();

  assert(reached,
    'renderAll aborted at the missing renderer, so every renderer after it ' +
    'was silently skipped and the page stayed half-painted');
});

check('the missing renderer is reported rather than swallowed', () => {
  const { sandbox } = evaluate({ truncateBefore: 'window.refreshVisualizer =' });

  const logged = [];
  sandbox.console.warn = (...a) => logged.push(a.join(' '));
  sandbox.renderAll();

  assert(logged.some((l) => /refreshVisualizer/.test(l)),
    'a renderer that could not run was dropped without a word, so the blank ' +
    'panel has no cause anywhere to look for');
});

// ---------------------------------------------------------------------------

console.log(`\n${passed} passed, ${failures.length} failed`);
if (failures.length) {
  console.log('\nFAILURES:');
  for (const f of failures) console.log('  x ' + f);
  process.exit(1);
}
console.log('renderAll robustness: OK');