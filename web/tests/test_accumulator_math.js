/**
 * Honesty tests for the client-side accumulator math.
 *
 * The ranked Model Board already gets its accumulators from board.py, which
 * haircuts the joint probability for shared uncertainty across legs
 * (CORRELATION_PENALTY_SAME_DAY). Two older surfaces in app.js still computed
 * parlays independently, in the browser, and both got it wrong in ways that
 * flatter the bettor:
 *
 *   * `Math.max(0, ...)` on the combined EV, so a parlay with negative
 *     expected value rendered as a confident "+0.0%" rather than a loss;
 *   * the naive product, presented as the joint probability, which overstates
 *     it whenever two legs kick off on the same matchday.
 *
 * These assertions pin the fixed behaviour. They call the renderers and read
 * the HTML, because the bug lived in the arithmetic inside a template string.
 *
 * Run with:  node tests/test_accumulator_math.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP = path.join(__dirname, '..', 'js', 'app.js');

function makeElement(id) {
  return {
    id, innerHTML: '', textContent: '', style: {},
    classList: { add() {}, remove() {}, contains: () => false },
    addEventListener() {}, setAttribute() {},
    querySelectorAll: () => [], querySelector: () => null,
    getAttribute: () => null, value: '',
  };
}

function loadApp() {
  const elements = new Map();
  const getEl = (id) => {
    if (!elements.has(id)) elements.set(id, makeElement(id));
    return elements.get(id);
  };

  const sandbox = {
    console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
    Date, Math, JSON, Object, Array, Number, String, Boolean, isFinite,
    parseInt, parseFloat, encodeURIComponent, decodeURIComponent, URLSearchParams,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    navigator: { userAgent: 'node', clipboard: {} },
    location: { hash: '', search: '', href: 'http://localhost/', pathname: '/' },
    history: { replaceState() {} },
    fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
    requestAnimationFrame: () => 0, cancelAnimationFrame: () => {},
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

  sandbox.document = {
    // 'loading' so initApp() never runs: these tests drive the two renderers
    // directly and must not depend on the auth/network bootstrap.
    readyState: 'loading',
    addEventListener() {}, removeEventListener() {},
    getElementById: (id) => (id ? getEl(id) : null),
    querySelector: () => null, querySelectorAll: () => [],
    createElement: () => makeElement('created'), createTextNode: () => ({}),
    body: makeElement('body'), documentElement: makeElement('html'),
  };

  const source = fs.readFileSync(APP, 'utf8');
  const imported = [];
  const stripped = source
    .replace(/^[ \t]*import\s+(?:([\w$]+)\s*,\s*)?\{([^}]*)\}\s*from\s*['"][^'"]+['"]\s*;?[ \t]*$/gm,
      (_line, dflt, named) => {
        for (const part of named.split(',')) {
          const name = part.trim().split(/\s+as\s+/).pop().trim();
          if (name) imported.push(name);
        }
        if (dflt) imported.push(dflt);
        return '';
      })
    .replace(/^[ \t]*import\s+[\w$]+\s*from\s*['"][^'"]+['"]\s*;?[ \t]*$/gm, '')
    .replace(/^\s*export\s+(const|let|var|function|class|default)\s/gm, '$1 ')
    .replace(/^\s*export\s*\{[^}]*\};?\s*$/gm, '');

  const inertProxy = new Proxy(function () {}, {
    get(_t, prop) {
      if (prop === 'then' || prop === Symbol.toPrimitive) return undefined;
      if (prop === Symbol.iterator) return function* () {};
      return inertProxy;
    },
    apply() { return inertProxy; },
    construct() { return inertProxy; },
  });
  for (const name of imported) sandbox[name] = inertProxy;

  const EXPOSED = ['state', 'renderAccumulatorBanner', 'renderTier3Alpha',
    'renderTicker', 'renderBacktest'];
  vm.createContext(sandbox);
  vm.runInContext(
    `${stripped}\n;globalThis.__lisa = { ${EXPOSED.map((n) => `${n}: typeof ${n} !== 'undefined' ? ${n} : undefined`).join(', ')} };`,
    sandbox, { filename: 'app.js' }
  );

  return { sandbox, getEl, app: sandbox.__lisa };
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

/** A priced, high-conviction pick, as the ledger produces. */
function pick(overrides = {}) {
  return Object.assign({
    id: 'p1',
    is_pass_advisory: false,
    grade: 'GRADE_A',
    conviction_score: 30,
    p_true: 0.55,
    best_odds: 2.10,
    best_book: 'book-a',
    outcome_name: 'Bayern Munich',
    home_team: 'Bayern Munich',
    away_team: 'Dortmund',
    commence_time: '2026-10-02T18:30:00+00:00',
  }, overrides);
}

/** Two legs on the SAME matchday: correlated, so the naive product lies. */
function sameDayPair() {
  return [
    pick({ id: 'p1', outcome_name: 'Bayern Munich', home_team: 'Bayern Munich', away_team: 'Dortmund' }),
    pick({ id: 'p2', outcome_name: 'Leverkusen', home_team: 'Leverkusen', away_team: 'Frankfurt' }),
  ];
}

/** Two legs on DIFFERENT days: genuinely close to independent. */
function differentDayPair() {
  return [
    pick({ id: 'p1', outcome_name: 'Bayern Munich', commence_time: '2026-10-02T18:30:00+00:00' }),
    pick({ id: 'p2', outcome_name: 'Leverkusen', home_team: 'Leverkusen', away_team: 'Frankfurt',
           commence_time: '2026-10-05T18:30:00+00:00' }),
  ];
}

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

let passed = 0;
const failures = [];

function check(name, fn) {
  try { fn(); passed += 1; }
  catch (err) {
    failures.push(`${name}: ${err.message}\n      ${(err.stack || '').split('\n').slice(1, 3).join('\n      ')}`);
  }
}

function assert(cond, msg) { if (!cond) throw new Error(msg); }

/** Drive renderAccumulatorBanner over a set of picks; return the banner HTML. */
function banner(picks) {
  const { getEl, app } = loadApp();
  app.state.data = { active_picks: picks };
  app.state.currentTier = 'all';
  app.state.activeAccuBook = 'sportybet';
  app.renderAccumulatorBanner();
  return getEl('accumulator-banner-container').innerHTML;
}

/** Drive renderTier3Alpha, which owns #parlays-list; return the parlay HTML. */
function parlayList(picks) {
  const { getEl, app } = loadApp();
  app.state.data = { active_picks: picks, settled_ledger: [], summary: {}, clv: null };
  try { app.renderTier3Alpha(); } catch (e) { /* unrelated sections need a fuller payload */ }
  return getEl('parlays-list').innerHTML;
}

// ---------------------------------------------------------------------------
// Tests: the banner
// ---------------------------------------------------------------------------

check('a losing parlay renders a negative EV, not a clamped +0.0%', () => {
  // p = 0.5 * 0.5 = 0.25, odds = 1.30 * 1.30 = 1.69, so EV = 1.69*0.25 - 1
  // = -57.75%: badly losing. Math.max(0, ...) rendered "+0.0%".
  const picks = [
    pick({ id: 'p1', p_true: 0.5, best_odds: 1.30, commence_time: '2026-10-02T18:30:00+00:00' }),
    pick({ id: 'p2', p_true: 0.5, best_odds: 1.30, outcome_name: 'Leverkusen',
           home_team: 'Leverkusen', away_team: 'Frankfurt',
           commence_time: '2026-10-05T18:30:00+00:00' }),
  ];
  const html = banner(picks);
  assert(!/\+0\.0%/.test(html), `a losing parlay was clamped to +0.0%:\n${html}`);
  assert(/-57\.7%/.test(html), `expected -57.7% EV, got:\n${html.match(/.{0,60}EV.{0,40}/)}`);
});

check('the banner applies a correlation haircut for same-day legs', () => {
  const html = banner(sameDayPair());
  assert(/correlation haircut/i.test(html), 'same-day legs must show the haircut');
  // 0.55 * 0.55 = 0.3025 naive; 6% haircut => 0.2844 => 28.4%.
  assert(/30\.3%/.test(html), `the naive product 30.25% should be shown for comparison:\n${html}`);
  assert(/28\.4%/.test(html), `the adjusted 28.44% should be the headline figure:\n${html}`);
});

check('legs on different days take no haircut', () => {
  const html = banner(differentDayPair());
  assert(!/correlation haircut/i.test(html),
    'independent legs must not be penalised');
  assert(/30\.3%/.test(html), 'the naive product stands when legs are independent');
});

check('the banner renders nothing when fewer than two legs are priced', () => {
  assert(banner([pick()]) === '', 'one leg is not an accumulator');
  assert(banner([]) === '', 'no picks means no banner');
});

// ---------------------------------------------------------------------------
// Tests: the parlay list
// ---------------------------------------------------------------------------

check('the parlay list reports a negative EV with its sign', () => {
  const picks = [
    pick({ id: 'p1', p_true: 0.5, best_odds: 1.30, commence_time: '2026-10-02T18:30:00+00:00' }),
    pick({ id: 'p2', p_true: 0.5, best_odds: 1.30, outcome_name: 'Leverkusen',
           home_team: 'Leverkusen', away_team: 'Frankfurt',
           commence_time: '2026-10-05T18:30:00+00:00' }),
  ];
  const html = parlayList(picks);
  assert(html !== '', 'the parlay list rendered nothing at all');
  assert(!/EV \+0\.0%/.test(html), `a losing slip rendered as break-even:\n${html}`);
  assert(/EV -57\.7%/.test(html), `expected -57.7% EV, got:\n${html.match(/.{0,60}EV.{0,40}/)}`);
});

check('the parlay list shows the penalty beside the naive product', () => {
  const html = parlayList(sameDayPair());
  assert(/correlation penalty/i.test(html), 'the haircut must be labelled');
  assert(/naive product 30\.3%/.test(html), 'the naive product must be shown');
  assert(/28\.4%/.test(html), 'the adjusted probability must be shown');
});

check('the parlay list refuses to build from unpriced legs', () => {
  const picks = [
    pick({ id: 'p1', best_odds: null }),
    pick({ id: 'p2', best_odds: null, outcome_name: 'Leverkusen',
           home_team: 'Leverkusen', away_team: 'Frankfurt' }),
  ];
  const html = parlayList(picks);
  assert(/not enough priced live selections/i.test(html),
    `an unpriced parlay must not be presented as a slip:\n${html}`);
});

// ---------------------------------------------------------------------------

console.log(`\n${passed} passed, ${failures.length} failed`);
if (failures.length) {
  console.log('\nFAILURES:');
  for (const f of failures) console.log('  x ' + f);
  process.exit(1);
}
console.log('accumulator math honesty: OK');
