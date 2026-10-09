/**
 * Honesty tests for the Model Opportunity Board renderer.
 *
 * The invariant under test is the product's core rule: an absent value must
 * render as absent. This is not hypothetical -- the same file already shipped
 * `(p.best_ev * 100).toFixed(1)`, and in JavaScript `null * 100 === 0`, so every
 * unpriced pick rendered a confident "+0.0%" edge on a market that had never
 * been observed. A renderer is exactly where that bug class reappears.
 *
 * No DOM, no framework, no bundler: app.js is evaluated against a minimal stub
 * and the produced HTML is asserted on. Run with:  node tests/test_model_board_render.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP = path.join(__dirname, '..', 'js', 'app.js');

// ---------------------------------------------------------------------------
// Minimal DOM + browser stub
// ---------------------------------------------------------------------------

function makeElement(id) {
  return {
    id,
    innerHTML: '',
    textContent: '',
    style: {},
    classList: { add() {}, remove() {}, contains: () => false },
    addEventListener() {},
    setAttribute() {},
    querySelectorAll: () => [],
    querySelector: () => null,
    getAttribute: () => null,
    value: '',
  };
}

/** Build a sandbox in which app.js can be evaluated and driven. */
function loadApp() {
  const elements = new Map();
  const getEl = (id) => {
    if (!elements.has(id)) elements.set(id, makeElement(id));
    return elements.get(id);
  };

  const sandbox = {
    console,
    setTimeout,
    clearTimeout,
    setInterval: () => 0,
    clearInterval: () => {},
    Date,
    Math,
    JSON,
    Object,
    Array,
    Number,
    String,
    Boolean,
    isFinite,
    parseInt,
    parseFloat,
    encodeURIComponent,
    decodeURIComponent,
    URLSearchParams,
    localStorage: {
      getItem: () => null,
      setItem() {},
      removeItem() {},
    },
    sessionStorage: {
      getItem: () => null,
      setItem() {},
      removeItem() {},
    },
    navigator: { userAgent: 'node', clipboard: {} },
    location: { hash: '', search: '', href: 'http://localhost/', pathname: '/' },
    history: { replaceState() {} },
    fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
    requestAnimationFrame: () => 0,
    cancelAnimationFrame: () => {},
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }),
    CustomEvent: class {},
    Event: class {},
    innerWidth: 1440,
    innerHeight: 900,
    devicePixelRatio: 1,
    scrollTo() {},
    scrollBy() {},
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
    // 'loading' means app.js registers a DOMContentLoaded listener and never
    // calls initApp(). The bootstrap talks to the network and to auth; these
    // tests drive the renderer directly and must not depend on either.
    readyState: 'loading',
    addEventListener() {},
    removeEventListener() {},
    getElementById: (id) => (id ? getEl(id) : null),
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => makeElement('created'),
    createTextNode: () => ({}),
    body: makeElement('body'),
    documentElement: makeElement('html'),
  };

  // app.js is an ES module, so `vm.runInContext` (script mode, not module mode)
  // cannot evaluate it as-is. Rather than add a bundler, drop the `import`
  // lines and supply the imported bindings as sandbox globals. Anything the
  // Model Board renderer does not touch is inert here by construction: these
  // tests drive the renderer directly and assert on the HTML it writes.
  const source = fs.readFileSync(APP, 'utf8');
  const imported = [];
  const stripped = source.replace(
    /^[ \t]*import\s+(?:([\w$]+)\s*,\s*)?\{([^}]*)\}\s*from\s*['"][^'"]+['"]\s*;?[ \t]*$/gm,
    (_line, dflt, named) => {
      for (const part of named.split(',')) {
        const name = part.trim().split(/\s+as\s+/).pop().trim();
        if (name) imported.push(name);
      }
      if (dflt) imported.push(dflt);
      return '';
    }
  ).replace(
    /^[ \t]*import\s+[\w$]+\s*from\s*['"][^'"]+['"]\s*;?[ \t]*$/gm,
    ''
  ).replace(/^\s*export\s+(const|let|var|function|class|default)\s/gm, '$1 ')
    .replace(/^\s*export\s*\{[^}]*\};?\s*$/gm, '');

  for (const name of imported) sandbox[name] = moduleStub(name);

  // Top-level `const`/`function` declarations in a script are lexical bindings
  // of that script, not properties of the global object, so `sandbox.state` and
  // friends are unreachable from here. Expose exactly what the tests drive.
  const EXPOSED = ['state', 'renderModelBoard', 'renderModelBoardStatus',
    'renderModelBoardLadders', 'renderModelBoardAccumulators', 'refreshModelBoard',
    'fmtOdds', 'fmtPct', 'escapeHtml', 'switchTab', 'VALID_VIEWS'];

  vm.createContext(sandbox);
  vm.runInContext(
    `${stripped}\n;globalThis.__lisa = { ${EXPOSED.map((n) => `${n}: typeof ${n} !== 'undefined' ? ${n} : undefined`).join(', ')} };`,
    sandbox,
    { filename: 'app.js' }
  );

  return { sandbox, getEl, app: sandbox.__lisa };
}

/** A stand-in for an imported module binding: callable, chainable, inert. */
function moduleStub(name) {
  const inert = () => inert;
  const proxy = new Proxy(function () {}, {
    get(_t, prop) {
      if (prop === 'then' || prop === Symbol.toPrimitive) return undefined; // not a thenable
      if (prop === Symbol.iterator) return function* () {};
      return proxy;
    },
    apply() { return proxy; },
    construct() { return proxy; },
  });
  void inert;
  return proxy;
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

/** An unpriced opportunity: model probability present, market price absent. */
function unpricedOpportunity(overrides = {}) {
  return Object.assign({
    match_id: 'm1',
    sport_key: 'soccer_germany_bundesliga',
    kickoff: '2026-10-02T18:30:00+00:00',
    home: 'Bayern Munich',
    away: 'Dortmund',
    market: 'h2h',
    selection: 'Bayern Munich',
    line: null,
    p_model: 0.5543,
    fair_odds: 1.8033,
    best_odds: null,
    best_book: null,
    best_source: null,
    ev: null,
    stake_fraction: 0,
    priced: false,
    basis: 'model_only',
    reason: 'no market price observed',
  }, overrides);
}

function accumulator(overrides = {}) {
  return Object.assign({
    size: 2,
    legs: [unpricedOpportunity(), unpricedOpportunity({ home: 'Leverkusen' })],
    description: 'Bayern Munich v Dortmund + Leverkusen v Frankfurt',
    p_naive: 0.3,
    p_adjusted: 0.27,
    fair_odds: 3.7,
    best_odds: null,
    best_book: null,
    ev: null,
    stake_fraction: 0,
    priced: false,
    correlation_penalty: 0.9,
    warnings: ['legs may share a matchday'],
  }, overrides);
}

function boardPayload(overrides = {}) {
  const board = Object.assign({
    generated_at: '2026-09-29T12:00:00+00:00',
    window_hours: 48,
    unproven: false,
    model: { teams: 38, matches_used: 702 },
    winning: [unpricedOpportunity()],
    earning: [],
    micro_bets: [unpricedOpportunity({ market: 'totals', line: 2.5, selection: 'Over' })],
    accumulators: [accumulator()],
    coverage: {
      window_hours: 48,
      fixtures_seen: 5,
      fixtures_modelled: 5,
      fixtures_priced: 0,
      meets_volume_target: false,
      volume_target: 12,
      leagues: { soccer_germany_bundesliga: 5 },
      sources: { openligadb: 5 },
      notes: ['Bundesliga is between matchdays'],
    },
    notes: [],
  }, overrides);

  return Object.assign({
    success: true,
    health: 'degraded',
    generated_at: '2026-09-29T12:00:00+00:00',
    window_hours: 48,
    summary: 'health=degraded',
    model: { teams: 38, matches_used: 702 },
    providers: [],
    errors: [],
    board,
  }, overrides);
}

// ---------------------------------------------------------------------------
// Assertions
// ---------------------------------------------------------------------------

let passed = 0;
const failures = [];

function check(name, fn) {
  try {
    fn();
    passed += 1;
  } catch (err) {
    failures.push(`${name}: ${err.message}\n      ${(err.stack || '').split('\n').slice(1, 4).join('\n      ')}`);
  }
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

function render(payload, stateExtra = {}) {
  const { sandbox, getEl, app } = loadApp();
  Object.assign(app.state, {
    modelBoard: payload,
    modelBoardState: payload && payload.success ? 'ready' : 'failed',
    modelBoardError: payload && payload.success ? '' : (payload && payload.error) || 'cycle failed',
  }, stateExtra);
  app.renderModelBoard();
  return {
    status: getEl('mb-status').innerHTML,
    ladders: getEl('mb-ladders').innerHTML,
    accas: getEl('mb-accumulators').innerHTML,
    micro: getEl('micro-markets').innerHTML,
    alpha: getEl('alpha-poisson-tbody').innerHTML,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

check('curated, micro and premium research tables show the nearest dates first', () => {
  const early=unpricedOpportunity({home:'Earliest Fixture',kickoff:'2026-10-09T18:00:00Z',p_model:.6});
  const later=unpricedOpportunity({home:'Later Fixture',kickoff:'2026-10-10T12:00:00Z',p_model:.9});
  const rows=[later,early];
  const html=render(boardPayload({winning:rows,earning:rows,micro_bets:rows,research:rows,
    research_access:{available:true,candidate_count:2,evaluated_candidates:2}}));
  for (const name of ['ladders','micro','alpha']) {
    assert(html[name].includes('Earliest Fixture'), `${name} did not render the match`);
    assert(html[name].indexOf('Earliest Fixture')<html[name].indexOf('Later Fixture'), `${name} hid the earliest match`);
  }
});

check('accumulators and their legs follow kickoff before joint probability', () => {
  const leg=(home,kickoff)=>unpricedOpportunity({home,kickoff});
  const early=accumulator({description:'Earlier Combination',p_adjusted:.6,legs:[
    leg('Second Leg','2026-10-10T12:00:00Z'),leg('First Leg','2026-10-09T18:00:00Z')]});
  const later=accumulator({description:'Later Combination',p_adjusted:.8,legs:[
    leg('Last Leg','2026-10-11T12:00:00Z'),leg('Third Leg','2026-10-10T18:00:00Z')]});
  const {accas}=render(boardPayload({accumulators:[later,early]}));
  assert(accas.indexOf('Earlier Combination')<accas.indexOf('Later Combination'), 'stronger later combination hid an earlier one');
  assert(accas.indexOf('First Leg')<accas.indexOf('Second Leg'), 'earlier combination legs were out of order');
  assert(accas.indexOf('Third Leg')<accas.indexOf('Last Leg'), 'later combination legs were out of order');
});

check('an unpriced opportunity never renders an edge of zero', () => {
  const { ladders } = render(boardPayload());

  // The exact bug that shipped: `null * 100 === 0`.
  assert(!/\+0\.0%/.test(ladders),
    `a fabricated "+0.0%" edge was rendered:\n${ladders.match(/.{0,90}\+0\.0%.{0,40}/)}`);
  assert(!/Edge \(EV\)<\/div>\s*<div[^>]*>\s*\+?0\.0/.test(ladders),
    'Edge cell rendered a bare zero');
  assert(/n\/a/.test(ladders), 'missing EV should render as n/a');
});

check('missing odds render as n/a, never 0.00', () => {
  const { ladders } = render(boardPayload());
  assert(!/0\.00/.test(ladders),
    `a zero was fabricated where no price exists:\n${ladders.match(/.{0,90}0\.00.{0,40}/)}`);
  assert(/n\/a/.test(ladders), 'missing price should render as n/a');
});

check('model probability and fair odds DO render (they are real)', () => {
  const { ladders } = render(boardPayload());
  assert(/55\.4%/.test(ladders), 'model probability 0.5543 should render as 55.4%');
  assert(/1\.80/.test(ladders), 'fair odds 1.8033 should render as 1.80');
});

check('a real, positive edge renders with its sign', () => {
  const priced = unpricedOpportunity({
    best_odds: 2.10, best_book: 'sharp', ev: 0.164, priced: true,
    basis: 'model_vs_market',
  });
  const { ladders } = render(boardPayload({ winning: [priced] }));
  assert(/\+16\.4%/.test(ladders),
    `a genuine +16.4% edge was not rendered:\n${ladders.match(/.{0,120}EV.{0,60}/)}`);
});

check('a negative edge renders with its sign, not as positive', () => {
  const losing = unpricedOpportunity({ best_odds: 1.5, ev: -0.07, priced: true });
  const { ladders } = render(boardPayload({ earning: [losing] }));
  assert(/-7\.0%/.test(ladders), 'a negative EV must not lose its minus sign');
});

check('the empty earning ladder explains why it is empty', () => {
  const { ladders } = render(boardPayload());
  assert(/Empty by design/.test(ladders),
    'an empty earning ladder must state that it is empty by design');
  // And must NOT be backfilled with the unpriced winning row.
  const earningBlock = ladders.split('Earning ladder')[1] || '';
  const microBlock = ladders.split('Qualifying market alternatives')[1] || '';
  assert(!/Dortmund/.test(earningBlock.split('Qualifying market alternatives')[0] || ''),
    'an unpriced row leaked into the earning ladder');
  assert(/Dortmund/.test(microBlock), 'micro markets should still list the row');
});

check('prices read but unmatched is reported as a matching failure, not as no price', () => {
  // The three causes of an empty earning ladder look identical from the board
  // alone. "No prices exist" and "prices exist but could not be attached to a
  // fixture" call for opposite operator responses -- add a price source versus
  // fix the matcher -- so they must not share a message.
  const { ladders } = render(boardPayload({
    prices: { source: 'sharpapi', quotes: 36, rows: 174, pages: 1 },
    price_match: { matched_events: 1, unmatched_events: 1 },
  }));
  assert(/36 prices were read but none could be attached/.test(ladders),
    'an unmatched price feed must be reported as a matching failure');
  assert(!/Empty by design/.test(ladders),
    '"no price observed" is false when 36 prices were read');
});

check('expired published prices are not reported as failed event matching', () => {
  const { status, ladders } = render(boardPayload({
    prices: { quotes: 36, rows: 36 },
    price_match: { matched_fixtures: 2, matched_events: 2 },
    price_readiness: { state: 'expired', scope: 'published_selections', fixtures_priced: 0,
      fresh_earning_selections: 0, collection_state: 'ok' },
  }));
  assert(/Published prices have expired/.test(status));
  assert(/Published prices have expired/.test(ladders));
  assert(!/none could be attached/.test(ladders));
});

check('collection failure is distinguished from an expired publication', () => {
  const { status, ladders } = render(boardPayload({
    prices: { quotes: 36, rows: 36 },
    price_readiness: { state: 'unavailable', scope: 'published_selections', fixtures_priced: 0,
      fresh_earning_selections: 0, collection_state: 'unavailable' },
  }));
  assert(/Price collection needs attention/.test(status));
  assert(/Current offers could not be verified/.test(ladders));
  assert(!/none could be attached/.test(ladders));
});

check('prices read and priced but no edge is reported as no edge, not as no price', () => {
  const base = boardPayload();
  const { ladders } = render(boardPayload({
    board: Object.assign({}, base.board, {
      coverage: Object.assign({}, base.board.coverage, { fixtures_priced: 2 }),
    }),
    prices: { source: 'sharpapi', quotes: 174, rows: 174, pages: 1 },
    price_match: { matched_events: 2, matched_fixtures: 2, unmatched_events: 0 },
  }));
  assert(/No selection cleared the edge threshold/.test(ladders),
    'a priced-but-flat market must say so');
  assert(/2 fixtures were/.test(ladders), 'the priced count must be stated');
  assert(!/Empty by design/.test(ladders), '"no price observed" is false here');
  assert(!/none could be attached/.test(ladders), 'nothing failed to attach here');
});

check('the priced count is not singularised when it is one', () => {
  const base = boardPayload();
  const { ladders } = render(boardPayload({
    board: Object.assign({}, base.board, {
      coverage: Object.assign({}, base.board.coverage, { fixtures_priced: 1 }),
    }),
    prices: { source: 'sharpapi', quotes: 36, rows: 36, pages: 1 },
  }));
  assert(/1 fixture was/.test(ladders), `"1 fixtures was" should read as singular`);
});

check('a coverage shortfall is framed as data coverage, not selection', () => {
  const { status } = render(boardPayload());
  assert(/coverage limit/i.test(status),
    'volume shortfall must be labelled a coverage limit');
  // The phrase may appear only inside a negation. A blanket negative regex
  // would flag the honest "not the model declining to recommend" as dishonest.
  assert(/not the model\s*declining to recommend/i.test(status),
    'the shortfall must be explicitly denied as a selection decision');
  const bareSelection = status.replace(/not the model\s*declining to recommend/gi, '');
  assert(!/declining to recommend|too few opportunities/i.test(bareSelection),
    'must not imply anywhere that the model had opportunities and refused them');
});

check('the unproven stamp is surfaced when the fit is thin', () => {
  // board.py emits `unproven` inside the board, not on the envelope.
  const base = boardPayload();
  const { status } = render(boardPayload({
    board: Object.assign({}, base.board, { unproven: true }),
  }));
  assert(/Unproven/i.test(status), 'a thin fit must be stamped unproven');
});

check('a trained paper model is not reported as having too few games', () => {
  const base = boardPayload();
  const { status } = render(boardPayload({
    paper_mode: true,
    model: { sufficient: true, matches_used: 7272, mean_games_behind: 42.279 },
    board: Object.assign({}, base.board, { unproven: true })
  }));
  assert(/Paper research/.test(status), 'paper state must remain explicit');
  assert(/7,272/.test(status), 'actual training count must be rendered');
  assert(/42\.3/.test(status), 'actual mean history must be rendered');
  assert(!/below.*minimum|short of|two matches|scorer attached/.test(status),
    'paper mode must not fabricate a sample deficiency');
});

check('a genuinely thin fit retains its training warning in paper mode', () => {
  const base = boardPayload();
  const { status } = render(boardPayload({
    paper_mode: true,
    model: { sufficient: false, matches_used: 4, mean_games_behind: 2 },
    board: Object.assign({}, base.board, { unproven: true })
  }));
  assert(/below.*minimum sample threshold/.test(status), 'real sample deficiency must remain visible');
});

check('a current degraded publication is not called a failed or last-good cycle', () => {
  const error = 'the_odds_api: HTTP 401';
  const { status } = render(boardPayload({
    errors: [error], stale_reason: error,
    service: { state: 'stale', error, age_sec: 10, stale_after_sec: 900 }
  }));
  assert(/saved publication remains visible/i.test(status), 'saved data must remain visible');
  assert(!/last good board|this cycle failed|older than/.test(status), 'provider errors do not imply an old publication');
  assert(status.split(error).length - 1 === 1, 'provider error must render only once');
});

check('old duplicate diagnostics and notes render once', () => {
  const base = boardPayload();
  const note = 'Research forecasts require validation';
  const error = 'sharpapi: HTTP 401';
  const { status } = render(boardPayload({
    errors: [error, error], stale_reason: `${error}; ${error}`,
    service: { error: `${error}; ${error}` }, notes: [note],
    board: Object.assign({}, base.board, { notes: [note], coverage:
      Object.assign({}, base.board.coverage, { notes: [note] }) })
  }));
  assert(status.split(error).length - 1 === 1, 'persisted duplicate errors must be collapsed');
  assert(status.split(note).length - 1 === 1, 'the same coverage/board/report note must render once');
  assert(/No usable bookmaker prices matched/.test(status), 'zero price coverage must be explained');
});

check('age-based staleness and worker-only failures are still visible', () => {
  const { status } = render(boardPayload({
    errors: [], stale_reason: 'stale',
    service: { age_sec: 1200, stale_after_sec: 900, error: 'Settlement worker failed' }
  }));
  assert(/older than its refresh target/.test(status), 'real age staleness must not be hidden');
  assert(/Settlement worker failed/.test(status), 'worker failure absent from the board errors must remain visible');
});

check('a failed cycle shows the cause and invents no board', () => {
  const { status, ladders, accas } = render({
    success: false,
    error: 'board cycle failed: no finished results',
    board: null,
    errors: ['openligadb.de is unreachable: ENETUNREACH'],
  });

  assert(/Board cycle failed/i.test(status), 'failure must be visible');
  assert(/unreachable/.test(status), 'the real cause must be shown');
  assert(/does not substitute a cached or invented board/i.test(status),
    'must state that no board was substituted');
  assert(ladders === '', 'no ladders may render for a failed cycle');
  assert(accas === '', 'no accumulators may render for a failed cycle');
});

check('a 503 with board:null is treated as failed, not as an empty board', () => {
  // The shape the server actually returns on a failed cycle.
  const { status, ladders } = render({ success: false, error: 'x', board: null });
  assert(/Board cycle failed/i.test(status), 'board:null with success:false must be a failure');
  assert(ladders === '', 'a failed cycle must not render an empty-but-valid board');
});

check('the accumulator shows the correlation penalty beside the naive product', () => {
  const { accas } = render(boardPayload());
  assert(/correlation penalty/i.test(accas), 'penalty must be labelled');
  assert(/10\.0%/.test(accas), 'a 0.9 penalty is a 10% penalty and must read as one');
  assert(/Naive product/.test(accas), 'the naive product must be shown for comparison');
  assert(/27\.00%/.test(accas) && /30\.00%/.test(accas),
    'adjusted (27%) and naive (30%) joint probabilities must both render');
});

check('an unpriced accumulator renders no edge', () => {
  const { accas } = render(boardPayload());
  assert(!/\+0\.0%/.test(accas), 'unpriced accumulator fabricated a zero edge');
});

check('row values are HTML-escaped', () => {
  const nasty = unpricedOpportunity({ home: '<img src=x onerror=alert(1)>' });
  const { ladders } = render(boardPayload({ winning: [nasty] }));
  assert(!/<img src=x/.test(ladders), 'team name was not escaped');
  assert(/&lt;img/.test(ladders), 'team name should appear escaped');
});

check('dedicated micro-bets view renders selections and shares board status', () => {
  const { app, getEl } = loadApp();
  app.state.modelBoard = boardPayload({ micro_bets: [unpricedOpportunity({
    market: 'btts', selection: 'Yes',
  })] });
  app.state.modelBoardState = 'ready';
  app.renderModelBoard();
  const html = getEl('micro-markets').innerHTML;
  assert(/btts/.test(html) && /Yes/.test(html), 'micro selection must be visible');
  assert(/n\/a/.test(html), 'absent market prices must remain absent');
  assert(getEl('micro-count-badge').textContent === ' (1)', 'micro count must match');
  assert(getEl('micro-status').innerHTML === getEl('mb-status').innerHTML, 'status must match');
  assert(app.VALID_VIEWS.has('micro-bets'), 'micro-bets navigation must be enabled');
});

check('dedicated micro-bets view clears selections after a failed request', () => {
  const { app, getEl } = loadApp();
  app.state.modelBoard = boardPayload();
  app.state.modelBoardState = 'ready';
  app.renderModelBoard();
  app.state.modelBoard = null;
  app.state.modelBoardState = 'failed';
  app.state.modelBoardError = 'provider unavailable';
  app.renderModelBoard();
  assert(getEl('micro-markets').innerHTML === '', 'failed request must clear markets');
  assert(/provider unavailable/.test(getEl('micro-status').innerHTML));
  assert(getEl('micro-count-badge').style.display === 'none', 'failed request must hide count');
});

// ---------------------------------------------------------------------------

console.log(`\n${passed} passed, ${failures.length} failed`);
if (failures.length) {
  console.log('\nFAILURES:');
  for (const f of failures) console.log('  x ' + f);
  process.exit(1);
}
console.log('model board renderer honesty: OK');
