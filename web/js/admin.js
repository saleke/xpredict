/* ==========================================================================
   LISA Console
   Buildless, no framework, no build step. The whole console is one module:
   a small fetch wrapper that knows about the CSRF token and the cookie, a
   router over the sections, and one render function per section.

   Two rules the code obeys throughout, because breaking them is the difference
   between a console and a liability:
     1. Nothing is rendered from an API response without passing through esc().
     2. Any action that is irreversible asks for a typed confirmation first.
   ========================================================================== */
(function () {
  'use strict';

  var API = '/api/admin';
  var SESSION_KEY = 'lisa_admin_session';

  var state = {
    role: null,
    user: null,
    csrf: null,
    expiresAt: null,
    view: 'overview',
    busy: 0
  };

  // The controller for the view currently being fetched. Navigating again aborts
  // the previous one, so a slow response can never paint over the view the user
  // actually asked for. Kept outside `state` because it is transport plumbing,
  // not UI state, and must never be persisted or serialised.
  var viewCtl = null;

  // Monotonic navigation counter. A background session refresh that lands after
  // the operator has moved on must not repaint the old view, so it checks the
  // sequence number it captured before touching the DOM.
  var navSeq = 0;

  var el = {
    gate: document.getElementById('gate'),
    gateForm: document.getElementById('gateForm'),
    gateEmail: document.getElementById('gateEmail'),
    gatePassword: document.getElementById('gatePassword'),
    gateError: document.getElementById('gateError'),
    gateSubmit: document.getElementById('gateSubmit'),
    shell: document.getElementById('shell'),
    railRole: document.getElementById('railRole'),
    railUptime: document.getElementById('railUptime'),
    nav: document.getElementById('nav'),
    view: document.getElementById('view'),
    viewTitle: document.getElementById('viewTitle'),
    viewNote: document.getElementById('viewNote'),
    busy: document.getElementById('busy'),
    toasts: document.getElementById('toasts'),
    btnRefresh: document.getElementById('btnRefresh'),
    btnSignOut: document.getElementById('btnSignOut')
  };

  /* ==================================================================
     Utilities
     ================================================================== */

  // Every interpolation into innerHTML goes through here. The API is ours,
  // but a bookmaker name, a Telegram username or a search term typed by a
  // person is not, and a single stray < in any of them should not be able to
  // execute in an operator's session.
  function esc(value) {
    if (value === null || value === undefined) return '';
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function attr(value) { return esc(value); }

  function num(value, digits) {
    if (value === null || value === undefined || value === '' || isNaN(Number(value))) return '—';
    return Number(value).toFixed(digits === undefined ? 0 : digits);
  }

  function pct(value, digits) {
    if (value === null || value === undefined || isNaN(Number(value))) return '—';
    return (Number(value) * 100).toFixed(digits === undefined ? 1 : digits) + '%';
  }

  function signed(value, digits) {
    if (value === null || value === undefined || isNaN(Number(value))) return '—';
    var n = Number(value);
    return (n > 0 ? '+' : '') + n.toFixed(digits === undefined ? 2 : digits);
  }

  function bytes(n) {
    if (n === null || n === undefined) return '—';
    var v = Number(n), units = ['B', 'KB', 'MB', 'GB'], i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v.toFixed(i === 0 ? 0 : 1) + ' ' + units[i];
  }

  function when(epoch) {
    if (!epoch) return '—';
    // The API sends seconds for ledger timestamps and ISO strings for the
    // process start; handle both rather than showing NaN for one of them.
    var d = typeof epoch === 'number' ? new Date(epoch * 1000) : new Date(epoch);
    if (isNaN(d.getTime())) return '—';
    return d.toLocaleString(undefined, {
      month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit'
    });
  }

  function ago(epoch) {
    if (!epoch) return '—';
    var secs = Date.now() / 1000 - Number(epoch);
    if (isNaN(secs)) return '—';
    if (secs < 60) return Math.max(0, Math.round(secs)) + 's ago';
    if (secs < 3600) return Math.round(secs / 60) + 'm ago';
    if (secs < 86400) return Math.round(secs / 3600) + 'h ago';
    return Math.round(secs / 86400) + 'd ago';
  }

  function duration(secs) {
    if (secs === null || secs === undefined) return '—';
    var s = Math.floor(Number(secs));
    if (s < 60) return s + 's';
    var m = Math.floor(s / 60), h = Math.floor(m / 60), d = Math.floor(h / 24);
    if (d) return d + 'd ' + (h % 24) + 'h';
    if (h) return h + 'h ' + (m % 60) + 'm';
    return m + 'm';
  }

  function toast(kind, title, body) {
    var node = document.createElement('div');
    node.className = 'toast toast--' + kind;
    node.innerHTML = '<div class="toast__title">' + esc(title) + '</div>' +
      (body ? '<div class="toast__body">' + esc(body) + '</div>' : '');
    el.toasts.appendChild(node);
    setTimeout(function () {
      node.style.opacity = '0';
      node.style.transition = 'opacity .25s';
      setTimeout(function () { node.remove(); }, 260);
    }, kind === 'err' ? 7000 : 3800);
  }

  function setBusy(on) {
    state.busy += on ? 1 : -1;
    if (state.busy < 0) state.busy = 0;
    el.busy.hidden = state.busy === 0;
  }

  /* ==================================================================
     API client
     ================================================================== */

  function rememberSession(data) {
    try {
      sessionStorage.setItem(SESSION_KEY, JSON.stringify({
        role: data.role, user: data.user, expires_at: data.expires_at
      }));
    } catch (e) { /* private mode: the server cookie is still authoritative */ }
  }

  function recallSession() {
    try {
      var raw = sessionStorage.getItem(SESSION_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }

  function forgetSession() {
    try { sessionStorage.removeItem(SESSION_KEY); } catch (e) {}
  }

  function request(method, path, body, opts) {
    opts = opts || {};
    var headers = { 'Accept': 'application/json' };
    var init = { method: method, headers: headers, credentials: 'same-origin' };

    // Read-only requests issued by a renderer are tied to the active view, so a
    // newer navigation can cancel them. Writes are never auto-cancelled: the
    // browser may already have delivered them, and silently dropping the
    // response would leave the operator unsure whether the action took effect.
    if ((method === 'GET' || method === 'HEAD') && !opts.keepAlive) {
      init.signal = opts.signal || (viewCtl ? viewCtl.signal : undefined);
    }

    if (body !== undefined && body !== null) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    // The CSRF token is derived from the session id, so it has to ride along on
    // every state-changing request. A GET never carries it.
    if (method !== 'GET' && method !== 'HEAD' && state.csrf && !opts.anonymous) {
      headers['X-CSRF-Token'] = state.csrf;
    }

    setBusy(true);
    return fetch(API + path, init).then(function (res) {
      return res.text().then(function (text) {
        var data = {};
        try { data = text ? JSON.parse(text) : {}; } catch (e) { data = { error: text }; }
        // A 401 anywhere means the session is gone. Return it to the caller so
        // it can drop to the sign-in gate instead of rendering an empty shell
        // that looks like a healthy system with no data.
        if (res.status === 401 && !opts.anonymous) {
          state.csrf = null;
          showGate('Your session has expired. Sign in again.');
          throw new Error('unauthorized');
        }
        if (!res.ok) {
          var message = data.error || ('Request failed (' + res.status + ')');
          if (data.detail && opts.verbose) message += ' — ' + data.detail;
          var err = new Error(message);
          err.status = res.status;
          err.payload = data;
          throw err;
        }
        return data;
      });
    }).finally(function () { setBusy(false); });
  }

  var api = {
    get: function (p, o) { return request('GET', p, null, o); },
    post: function (p, b, o) { return request('POST', p, b || {}, o); },
    put: function (p, b, o) { return request('PUT', p, b || {}, o); },
    patch: function (p, b, o) { return request('PATCH', p, b || {}, o); },
    del: function (p, b, o) { return request('DELETE', p, b || {}, o); }
  };

  /* ==================================================================
     Confirmation for irreversible actions
     ================================================================== */

  function confirmHard(message) {
    // window.confirm is deliberately used: it is the one dialog an operator
    // cannot fat-finger past, and it cannot be styled into something that
    // looks like ordinary UI.
    return window.confirm(message + '\n\nThis cannot be undone.');
  }

  function reportError(err) {
    if (err && err.message === 'unauthorized') return;
    toast('err', 'Request failed', err && err.message ? err.message : String(err));
  }

  /* ==================================================================
     Sign-in gate
     ================================================================== */

  function showGate(message) {
    el.gate.hidden = false;
    el.shell.hidden = true;
    if (message) {
      el.gateError.textContent = message;
      el.gateError.hidden = false;
    }
    el.gateEmail.focus();
  }

  el.gateForm.addEventListener('submit', function (ev) {
    ev.preventDefault();
    el.gateError.hidden = true;
    el.gateSubmit.disabled = true;
    el.gateSubmit.textContent = 'Signing in…';

    api.post('/login', {
      email: el.gateEmail.value.trim(),
      password: el.gatePassword.value
    }, { anonymous: true }).then(function (data) {
      state.role = data.role;
      state.user = data.user;
      state.csrf = data.csrf_token;
      state.expiresAt = data.expires_at;
      rememberSession(data);
      el.gatePassword.value = '';
      el.gateError.hidden = true;
      enterConsole();
    }).catch(function (err) {
      el.gateError.textContent = err.message || 'Sign-in failed.';
      el.gateError.hidden = false;
      el.gatePassword.select();
    }).finally(function () {
      el.gateSubmit.disabled = false;
      el.gateSubmit.textContent = 'Sign in';
    });
  });

  function enterConsole() {
    el.gate.hidden = true;
    el.gateError.hidden = true;
    el.shell.hidden = false;
    paintRole();
    var remembered = recallSession();
    var initial = (remembered && remembered.view) || 'overview';
    go(initial);
    startClock();
  }

  el.btnSignOut.addEventListener('click', function () {
    api.post('/logout', {}).catch(function () {
      // Even if the call fails the local session should be dropped: leaving a
      // stale shell on screen after "sign out" is worse than an extra server
      // session that the CSRF check will reject.
    }).finally(function () {
      state.csrf = null;
      forgetSession();
      showGate('Signed out.');
    });
  });

  el.btnRefresh.addEventListener('click', function () { go(state.view, true); });

  /* ==================================================================
     Router
     ================================================================== */

  var VIEWS = [
    { group: 'Monitor' },
    { id: 'overview', label: 'Overview' },
    { id: 'health', label: 'Health' },
    { id: 'keys', label: 'API keys' },
    { group: 'Accuracy' },
    { id: 'performance', label: 'Performance' },
    { id: 'picks', label: 'Pick ledger' },
    { id: 'forecast', label: 'Forecast board' },
    { group: 'Operations' },
    { id: 'operations', label: 'Production testing' },
    { id: 'notifications', label: 'Notifications' },
    { id: 'cache', label: 'Live cache' },
    { id: 'database', label: 'Database' },
    { group: 'Configuration', ownerOnly: true },
    { id: 'settings', label: 'Settings', ownerOnly: true },
    { id: 'sports', label: 'Sports & cost', ownerOnly: true },
    { group: 'Access' },
    { id: 'users', label: 'Users' },
    { id: 'audit', label: 'Audit log' },
    { id: 'system', label: 'System controls', ownerOnly: true }
  ];

  function isOwner() { return state.role === 'owner'; }

  function visibleViews() {
    return VIEWS.filter(function (v) {
      return !v.ownerOnly || isOwner();
    });
  }

  function buildNav() {
    el.nav.innerHTML = visibleViews().map(function (v) {
      if (v.group) return '<div class="nav__group">' + esc(v.group) + '</div>';
      return '<button class="nav__item" data-view="' + attr(v.id) + '"' +
        ' aria-current="' + (state.view === v.id ? 'true' : 'false') + '">' +
        '<span class="nav__dot"></span>' + esc(v.label) + '</button>';
    }).join('');
  }

  el.nav.addEventListener('click', function (ev) {
    var btn = ev.target.closest('[data-view]');
    if (btn) go(btn.getAttribute('data-view'));
  });

  function go(viewId, force) {
    var def = null;
    visibleViews().forEach(function (v) { if (v.id === viewId) def = v; });
    if (!def) { viewId = 'overview'; def = { id: 'overview', label: 'Overview' }; }

    state.view = viewId;
    try { sessionStorage.setItem(SESSION_KEY + '_view', viewId); } catch (e) {}

    // The cookie is authoritative and the server re-resolves the role on every
    // request, so re-fetch it on each navigation. An operator whose rights
    // changed (allowlist edit, tier change, restart) sees the matching controls
    // without reloading the tab; boot() still paints the first view from the
    // session restoration, this only keeps a long-lived tab honest.
    navSeq += 1;
    refreshRole(navSeq);

    // Supersede whatever was still loading. The aborted render may already have
    // scheduled its own DOM writes, so the identity check below is what actually
    // keeps the newer view on screen.
    if (viewCtl) { try { viewCtl.abort(); } catch (e) {} }
    var mine = (typeof AbortController === 'function') ? new AbortController() : null;
    viewCtl = mine;
    var stale = function () { return mine !== viewCtl; };

    buildNav();
    el.viewTitle.textContent = def.label;
    el.viewNote.textContent = '';
    el.view.innerHTML = '<div class="grid grid--4">' +
      ['<div class="card"><div class="stat"><div class="skeleton" style="width:60%"></div>' +
       '<div class="skeleton" style="height:26px;margin:8px 0;width:45%"></div></div></div>'
      ].join('') + '</div>';

    var render = RENDER[viewId];
    if (!render) { el.view.innerHTML = empty('No view registered'); return; }

    Promise.resolve(render(force)).then(function (note) {
      if (stale()) return;
      if (note) el.viewNote.textContent = note;
    }).catch(function (err) {
      if (stale() || (err && err.name === 'AbortError')) return;
      el.view.innerHTML = empty(
        'Could not load this view',
        (err && err.message) || String(err)
      );
      reportError(err);
    });
  }

  function empty(title, body) {
    return '<div class="card"><div class="empty">' +
      '<div class="empty__title">' + esc(title) + '</div>' +
      (body ? '<div>' + esc(body) + '</div>' : '') + '</div></div>';
  }

  function statTile(label, value, foot, tone) {
    return '<div class="card"><div class="stat">' +
      '<div class="stat__label">' + esc(label) + '</div>' +
      '<div class="stat__value' + (tone ? ' ' + tone : '') + '">' + value + '</div>' +
      (foot ? '<div class="stat__foot">' + esc(foot) + '</div>' : '') +
      '</div></div>';
  }

  function paintRole() {
    el.railRole.textContent = (state.role || 'operator') +
      (state.user && state.user.email ? ' · ' + state.user.email : '');
  }

  // The server resolves the role from the live cookie + allowlist/tier on every
  // request, but a tab caches it at boot. Re-resolve it in the background so
  // owner-only controls track the account's *current* rights without a reload.
  // Re-painting the active view is guarded by the navigation sequence so a slow
  // response can never paint over a view the operator actually asked for.
  function refreshRole(seq) {
    if (!state.role) return;
    api.get('/session', { anonymous: true, keepAlive: true }).then(function (s) {
      if (seq !== navSeq) return;
      if (!s.authenticated) {
        state.role = null;
        state.user = null;
        state.csrf = null;
        showGate('Your session has expired. Sign in again.');
        return;
      }
      var changed = (s.role || null) !== (state.role || null);
      state.role = s.role;
      state.user = s.user;
      state.csrf = s.csrf_token;
      state.expiresAt = s.expires_at;
      paintRole();
      if (changed) {
        buildNav();
        if (seq !== navSeq) return;
        if (state.view && RENDER[state.view]) {
          var viewId = state.view;
          Promise.resolve(RENDER[viewId](true)).then(function (note) {
            if (seq !== navSeq) return;
            if (note) el.viewNote.textContent = note;
          }).catch(function () { /* a re-render failure is non-fatal */ });
        }
      }
    }).catch(function () { /* non-fatal: keep the current role */ });
  }

  function card(title, bodyHtml, headExtra) {
    return '<div class="card">' +
      '<div class="card__head"><div class="card__title">' + esc(title) + '</div>' +
      '<div class="card__spacer"></div>' + (headExtra || '') + '</div>' +
      '<div class="card__body">' + bodyHtml + '</div></div>';
  }

  function tableCard(title, columns, rows, opts) {
    opts = opts || {};
    var head = columns.map(function (c) {
      return '<th' + (c.num ? ' class="num"' : '') + '>' + esc(c.label) + '</th>';
    }).join('');
    var body = rows.length ? rows.map(function (r) {
      return '<tr>' + columns.map(function (c) {
        var v = r[c.key];
        return '<td' + (c.num ? ' class="num"' : '') + '>' + (v === undefined || v === null || v === '' ? '<span class="mute">—</span>' : v) + '</td>';
      }).join('') + '</tr>';
    }).join('') : '<tr><td colspan="' + columns.length + '">' +
      empty(opts.emptyTitle || 'Nothing here yet', opts.emptyBody) + '</td></tr>';

    return '<div class="card"><div class="card__head">' +
      '<div class="card__title">' + esc(title) + '</div><div class="card__spacer"></div>' +
      (opts.headExtra || '') + '</div>' +
      '<div class="card__body card__body--flush"><div class="table-wrap">' +
      '<table><thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table>' +
      '</div>' + (opts.footer || '') + '</div></div>';
  }

  function pager(page, pages, total) {
    return '<div class="pager">' +
      '<span>Page ' + esc(page) + ' of ' + esc(pages) +
      ' · ' + esc(total) + ' total</span>' +
      '<div class="pager__spacer"></div>' +
      '<button class="btn btn--ghost btn--sm" data-page="' + (page - 1) + '"' +
      (page <= 1 ? ' disabled' : '') + '>Previous</button>' +
      '<button class="btn btn--ghost btn--sm" data-page="' + (page + 1) + '"' +
      (page >= pages ? ' disabled' : '') + '>Next</button></div>';
  }

  function badge(text, tone) {
    return '<span class="badge' + (tone ? ' badge--' + tone : '') + '">' + esc(text) + '</span>';
  }

  var clockTimer = null;
  var operationsRefreshedAt = 0;
  function startClock() {
    if (clockTimer) clearInterval(clockTimer);
    var tick = function () {
      if (el.railUptime.dataset.started) {
        el.railUptime.textContent = 'up ' + duration(Date.now() / 1000 - Number(el.railUptime.dataset.started));
      }
      if (state.role && state.view === 'operations' && state.busy === 0 &&
          document.visibilityState !== 'hidden' && Date.now() - operationsRefreshedAt >= 20000) {
        operationsRefreshedAt = Date.now();
        go('operations', true);
      }
    };
    clockTimer = setInterval(tick, 1000);
  }

  /* ==================================================================
     Views
     ================================================================== */

  var RENDER = {};

  // ---- Overview -----------------------------------------------------
  RENDER.overview = function () {
    return Promise.all([api.get('/overview'), api.get('/health')]).then(function (both) {
      var o = both[0], h = both[1];
      if (o.started_at) el.railUptime.dataset.started = new Date(o.started_at).getTime() / 1000;

      var counts = o.ledger_counts || {};
      var st = o.settings || {};
      var pool = o.odds_pool || {};

      var tiles = [
        statTile('Hit rate', pct(o.stats && o.stats.hit_rate),
          (o.stats ? o.stats.wins + 'W / ' + o.stats.losses + 'L graded' : ''),
          o.stats && o.stats.hit_rate >= 0.5 ? 'pos' : 'warn'),
        statTile('Flat-stake ROI', pct(o.stats && o.stats.flat_stake_roi, 2),
          o.stats ? signed(o.stats.flat_stake_profit_units) + ' units' : '',
          (o.stats && o.stats.flat_stake_profit_units > 0) ? 'pos' : 'mute'),
        statTile('Live picks', esc(counts.pending || 0),
          esc(counts.settled || 0) + ' settled'),
        statTile('Credits left', pool.present ? esc(pool.remaining_credits) : '—',
          pool.present ? 'of ' + esc(pool.budget_daily) + '/day' : 'no pool attached'),
        statTile('Leagues', esc(st.sports_count || 0),
          'markets: ' + esc(st.markets || '—')),
        statTile('Status', h.healthy ? 'OK' : 'DEGRADED',
          h.checks ? h.checks.filter(function (c) { return !c.ok; }).length + ' checks failing' : '',
          h.healthy ? 'pos' : 'warn')
      ].join('');

      var flags = [
        ['enable_inplay', 'In-play polling'],
        ['enable_extra_markets', 'Extra markets'],
        ['enable_micro_predictions', 'Micro predictions'],
        ['require_positive_ev', 'Require positive EV']
      ].map(function (f) {
        var on = !!st[f[0]];
        return '<div class="check"><span class="dot ' + (on ? 'brand' : 'mute') + '"></span>' +
          '<div class="check__body"><div class="check__name">' + esc(f[1]) + '</div>' +
          '<div class="check__detail">' + (on ? 'enabled' : 'disabled') + '</div></div>' +
          (on ? badge('on', 'brand') : badge('off')) + '</div>';
      }).join('');

      el.view.innerHTML =
        '<div class="grid grid--4">' + tiles + '</div>' +
        '<div class="grid grid--2" style="margin-top:14px">' +
          card('Product flags', flags) +
          card('Runtime', '<dl class="kv">' +
            row('Version', o.version) +
            row('Role', o.role) +
            row('Process', o.pid + ' · up ' + duration(o.uptime_seconds)) +
            row('Scheduler', o.scheduler && o.scheduler.present ? 'attached' : 'not attached') +
            row('Odds pool', pool.present ? pool.size + ' keys' : 'not attached') +
            row('Polling', o.paused ? '<span class="neg">paused</span>' : '<span class="pos">running</span>') +
            '</dl>') +
        '</div>';

      return o.paused ? 'Ingestion is paused.' : '';
    });
  };

  function row(k, v) {
    return '<dt>' + esc(k) + '</dt><dd>' + (v === undefined || v === null ? '—' : v) + '</dd>';
  }

  // ---- Health --------------------------------------------------------
  RENDER.health = function () {
    return api.get('/health').then(function (h) {
      var checks = (h.checks || []).map(function (c) {
        return '<div class="check">' +
          '<span class="dot ' + (c.ok ? 'pos' : 'neg') + '"></span>' +
          '<div class="check__body"><div class="check__name">' + esc(c.name) + '</div>' +
          '<div class="check__detail">' + esc(JSON.stringify(c.detail)) + '</div></div>' +
          (c.ok ? badge('ok', 'pos') : badge('failing', 'bad')) + '</div>';
      }).join('');

      el.view.innerHTML =
        '<div class="grid grid--2">' +
          '<div class="card"><div class="card__head"><div class="card__title">Checks</div>' +
          '<div class="card__spacer"></div>' +
          (h.healthy ? badge('healthy', 'pos') : badge('degraded', 'warn')) + '</div>' +
          '<div class="card__body card__body--flush">' + (checks || empty('No checks ran')) + '</div></div>' +
          card('Process', '<dl class="kv">' +
            row('Uptime', duration(h.uptime_seconds)) +
            row('Checked at', when(h.checked_at)) +
            '</dl>') +
        '</div>';
      return '';
    });
  };

  // ---- API keys ------------------------------------------------------
  RENDER.keys = function () {
    return api.get('/keys').then(function (k) {
      if (!k.configured) {
        el.view.innerHTML = empty('No key pool attached',
          k.note || 'The poller is not using rotation.');
        return '';
      }
      var rows = (k.keys || []).map(function (key, i) {
        var cooldown = key.disabled_until && key.disabled_until > Date.now() / 1000;
        return {
          idx: i + 1,
          label: '<span class="mono">' + esc(key.label || 'key ' + (i + 1)) + '</span>' +
            (k.active_index === i ? ' ' + badge('active', 'brand') : ''),
          requests: esc(key.requests_today != null ? key.requests_today : (key.requests || 0)) + ' / ' + esc(key.budget_daily != null ? key.budget_daily : (key.budget_day || 0)),
          remaining: esc(Math.max(0, Number(key.remaining != null ? key.remaining : 0))),
          cooldown: cooldown
            ? '<span class="warn">' + esc(Math.ceil(key.disabled_until - Date.now() / 1000)) + 's</span>'
            : '<span class="mute">—</span>',
          state: cooldown ? badge('cooldown', 'warn') : badge('available', 'pos'),
          action: isOwner()
            ? '<button class="btn btn--ghost btn--sm" data-cooldown="' + attr(key.label || ('key ' + (i + 1))) + '">Cooldown</button>'
            : '<span class="mute">owner only</span>'
        };
      });

      el.view.innerHTML =
        '<div class="note note--brand" style="margin-bottom:14px">' +
        'Rotating a key or putting one on cooldown spends budget from that key\'s own daily allowance. ' +
        'Owner role required.</div>' +
        tableCard('Key pool', [
          { key: 'label', label: 'Key' },
          { key: 'requests', label: 'Requests today' },
          { key: 'remaining', label: 'Remaining', num: true },
          { key: 'cooldown', label: 'Cooldown', num: true },
          { key: 'state', label: 'State' },
          { key: 'action', label: '' }
        ], rows, { emptyTitle: 'No keys' });
      return k.note || '';
    });
  };

  document.addEventListener('click', function (ev) {
    var cd = ev.target.closest('[data-cooldown]');
    if (!cd) return;
    var label = cd.getAttribute('data-cooldown');
    var mins = window.prompt('Cooldown length in minutes (1-1440):', '30');
    if (mins === null) return;
    var n = Number(mins);
    if (!isFinite(n) || n < 1 || n > 1440) {
      toast('err', 'Invalid duration', 'Enter a number of minutes between 1 and 1440.');
      return;
    }
    api.post('/keys/cooldown', { label: label, cooldown_seconds: Math.round(n * 60) })
      .then(function (r) { toast('ok', 'Cooldown set', (r.label || label) + ' paused for ' + mins + ' minutes.'); go('keys', true); })
      .catch(reportError);
  });

  // ---- Performance ---------------------------------------------------
  RENDER.performance = function () {
    return api.get('/picks/stats').then(function (r) {
      var s = r.stats || {};
      var tiles = [
        statTile('Graded', esc(s.graded || 0),
          esc(s.pending || 0) + ' pending · ' + esc(s.awaiting_settlement || 0) + ' awaiting'),
        statTile('Hit rate', pct(s.hit_rate),
          s.decided !== undefined ? '' : (s.wins + s.losses) + ' decided', (s.hit_rate || 0) >= 0.5 ? 'pos' : 'warn'),
        statTile('ROI', pct(s.flat_stake_roi, 2), signed(s.flat_stake_profit_units) + ' units',
          (s.flat_stake_profit_units || 0) > 0 ? 'pos' : 'neg'),
        statTile('Void rate', pct(s.void_rate), 'voids push no stake'),
        statTile('Avg p_true', num(s.avg_p_true, 3), 'model probability'),
        statTile('Avg EV', num(s.avg_best_ev, 4), 'edge at quoted odds'),
        statTile('Avg odds', num(s.avg_odds, 2), 'across graded picks'),
        statTile('Avg books', num(s.avg_books, 1), 'books per line')
      ].join('');

      // Calibration: if the model claims 60% and the bucket wins 75%, it is
      // overconfident no matter what the headline hit rate says. This is the
      // chart that decides whether to trust the model at all.
      var calib = (s.calibration || []).map(function (b) {
        var predicted = Number(b.avg_p || 0);
        var realised = b.n ? b.wins / b.n : 0;
        var width = Math.max(2, Math.min(100, predicted * 100));
        var realWidth = Math.max(2, Math.min(100, realised * 100));
        return '<div class="calib__row">' +
          '<div class="calib__bucket">' + (Math.round(predicted * 100) / 10).toFixed(1) + '–' +
          ((Math.round(predicted * 100) / 10) + 10).toFixed(1) + '%</div>' +
          '<div class="calib__track">' +
          '<div class="calib__pred" style="width:' + width + '%"></div>' +
          '<div class="calib__real" style="width:' + realWidth + '%; top:11px"></div>' +
          '</div>' +
          '<div class="calib__nums">' + b.wins + '/' + b.n + ' = ' + pct(realised, 0) + '</div>' +
          '</div>';
      }).join('');

      var byMarket = (s.by_market || []).map(function (m) {
        return {
          market: '<span class="mono">' + esc(m.market) + '</span>',
          n: esc(m.n), wins: esc(m.wins),
          rate: pct(m.n ? m.wins / m.n : null, 1),
          ev: num(m.avg_ev, 4),
          p: num(m.avg_p, 3)
        };
      });

      var bySport = (s.by_sport || []).slice(0, 15).map(function (s2) {
        return {
          sport: '<span class="mono">' + esc(s2.sport_key) + '</span>',
          n: esc(s2.n), wins: esc(s2.wins),
          rate: pct(s2.n ? s2.wins / s2.n : null, 1)
        };
      });

      el.view.innerHTML =
        '<div class="grid grid--4">' + tiles + '</div>' +
        '<div class="grid grid--2" style="margin-top:14px">' +
          card('Calibration', calib
            ? '<div class="calib">' + calib + '</div>' +
              '<div class="stat__foot" style="margin-top:11px">Grey is the probability the model claimed; ' +
              'the bar underneath is how often that bucket actually won.</div>'
            : empty('Not enough graded picks',
                    'Calibration needs graded WIN/LOSS picks with a recorded probability.'),
            badge((s.calibration || []).length + ' buckets')) +
          card('Empty-state note', '<div class="note">' +
            'A hit rate over a handful of graded picks is noise. Treat the calibration ' +
            'chart and the by-market table as the decision inputs, and the headline ' +
            'hit rate as a summary of them.</div>') +
        '</div>' +
        '<div class="grid grid--2" style="margin-top:14px">' +
          tableCard('By market', [
            { key: 'market', label: 'Market' },
            { key: 'n', label: 'n', num: true },
            { key: 'wins', label: 'Wins', num: true },
            { key: 'rate', label: 'Rate', num: true },
            { key: 'ev', label: 'Avg EV', num: true },
            { key: 'p', label: 'Avg p', num: true }
          ], byMarket, { emptyTitle: 'No graded picks yet' }) +
          tableCard('By sport', [
            { key: 'sport', label: 'Sport' },
            { key: 'n', label: 'n', num: true },
            { key: 'wins', label: 'Wins', num: true },
            { key: 'rate', label: 'Rate', num: true }
          ], bySport, { emptyTitle: 'No graded picks yet' }) +
        '</div>';

      return s.graded ? s.graded + ' graded picks' : 'No graded picks yet';
    });
  };

  // ---- Pick ledger ---------------------------------------------------
  var pickQuery = { page: 1, page_size: 50, state: '', market: '', q: '' };

  RENDER.picks = function () {
    var qs = [];
    if (pickQuery.page > 1) qs.push('page=' + pickQuery.page);
    qs.push('page_size=' + pickQuery.page_size);
    if (pickQuery.state) qs.push('state=' + encodeURIComponent(pickQuery.state));
    if (pickQuery.market) qs.push('market=' + encodeURIComponent(pickQuery.market));
    if (pickQuery.q) qs.push('q=' + encodeURIComponent(pickQuery.q));

    return api.get('/picks?' + qs.join('&')).then(function (r) {
      var rows = (r.rows || []).map(function (p) {
        var resultTone = p.result === 'WIN' ? 'pos' : (p.result === 'LOSS' ? 'bad' : null);
        return {
          when: '<span class="nowrap">' + esc(when(p.commence_time)) + '</span>' +
            '<div class="stat__foot">' + esc(p.match_id || '') + '</div>',
          fixture: esc([p.home_team, p.away_team].filter(Boolean).join(' v ') || p.match_id || '—'),
          market: '<span class="mono">' + esc(p.market || '—') + '</span>' +
            '<div class="stat__foot">' + esc(p.outcome_name || '') + '</div>',
          odds: num(p.best_odds, 2),
          p: num(p.p_true, 3),
          ev: num(p.best_ev, 4),
          state: badge(String(p.state || '').replace(/_/g, ' '), stateTone(p.state)),
          result: p.result ? badge(p.result, resultTone) : '<span class="mute">—</span>',
          action: isOwner() && p.state !== 'SETTLED' && p.state !== 'VOID'
            ? '<button class="btn btn--ghost btn--sm" data-settle="' + attr(p.dedupe_key) + '">Settle</button>'
            : '<span class="mute">—</span>'
        };
      });

      el.view.innerHTML =
        '<div class="toolbar">' +
          '<input class="input" id="pq" placeholder="Search team, outcome or match id" value="' + attr(pickQuery.q) + '" style="min-width:230px">' +
          '<select class="select" id="pstate">' + options([
            ['', 'Any state'], ['CONFIRMED', 'Confirmed'], ['PENDING_SETTLEMENT', 'Pending settlement'],
            ['SETTLED', 'Settled'], ['VOID', 'Void']
          ], pickQuery.state) + '</select>' +
          '<select class="select" id="pmarket">' + options([
            ['', 'Any market'], ['h2h', 'h2h'], ['spreads', 'Spreads'], ['totals', 'Totals']
          ], pickQuery.market) + '</select>' +
          '<button class="btn" id="papply">Apply</button>' +
          '<div class="toolbar__spacer"></div>' +
          '<span class="stat__foot">Showing ' + (r.rows || []).length + ' of ' + (r.total || 0) + '</span>' +
        '</div>' +
        tableCard('Ledger', [
          { key: 'when', label: 'Kickoff' },
          { key: 'fixture', label: 'Fixture' },
          { key: 'market', label: 'Market' },
          { key: 'odds', label: 'Odds', num: true },
          { key: 'p', label: 'p', num: true },
          { key: 'ev', label: 'EV', num: true },
          { key: 'state', label: 'State' },
          { key: 'result', label: 'Result' },
          { key: 'action', label: '' }
        ], rows, {
          emptyTitle: pickQuery.q || pickQuery.state ? 'No picks match those filters' : 'No picks yet',
          emptyBody: pickQuery.q || pickQuery.state ? 'Try a wider search.' : 'Nothing has been published to the ledger.',
          footer: pager(r.page, r.pages, r.total)
        });
      return '';
    });
  };

  function stateTone(state_) {
    if (state_ === 'SETTLED') return 'pos';
    if (state_ === 'VOID') return null;
    if (state_ === 'PENDING_SETTLEMENT' || state_ === 'CONFIRMED') return 'info';
    return null;
  }

  function options(pairs, current) {
    return pairs.map(function (p) {
      return '<option value="' + attr(p[0]) + '"' + (p[0] === current ? ' selected' : '') + '>' +
        esc(p[1]) + '</option>';
    }).join('');
  }

  // Set-tier dropdown for the Users view. Collapsed it shows the account's
  // current tier; opening it reveals every available tier (the current one
  // marked), so the dropdown always makes the full role set visible.
  var TIER_OPTS = [
    ['free', 'Free'],
    ['tier1', 'Tier 1'],
    ['tier2', 'Tier 2'],
    ['tier3', 'Tier 3'],
    ['admin', 'Admin']
  ];

  function tierDropdown(u) {
    var current = TIER_OPTS.some(function (t) { return t[0] === u.tier; }) ? u.tier : 'free';
    return '<span class="tier-dd" data-dduser="' + attr(u.id) + '">' +
      '<button type="button" class="tier-dd__btn" data-ddbtn="1">' +
      '<span class="tier-dd__val">' + esc(current) + '</span>' +
      '<span class="tier-dd__caret">&#9662;</span></button>' +
      '<ul class="tier-dd__menu" hidden>' +
      TIER_OPTS.map(function (t) {
        var isCur = t[0] === current;
        return '<li><button type="button" class="tier-dd__opt' + (isCur ? ' is-current' : '') + '"' +
          ' data-ddtier="' + attr(t[0]) + '"' + (isCur ? ' disabled' : '') + '>' +
          esc(t[1]) + '</button></li>';
      }).join('') +
      '</ul></span>';
  }

  // Any click outside an open tier dropdown closes it. The dropdown's own
  // button stops propagation, so the menu stays put until an option is
  // picked or the operator clicks somewhere else.
  document.addEventListener('click', function () {
    el.view.querySelectorAll('.tier-dd.is-open').forEach(function (o) {
      o.classList.remove('is-open');
      o.querySelector('.tier-dd__menu').hidden = true;
    });
  });

  el.view.addEventListener('click', function (ev) {
    var pg = ev.target.closest('[data-page]');
    if (pg && !pg.disabled) {
      var q = null;
      if (state.view === 'picks') q = pickQuery;
      else if (state.view === 'notifications') q = notifQuery;
      else if (state.view === 'users') q = userQuery;
      else if (state.view === 'audit') q = auditQuery;
      if (q) {
        q.page = Number(pg.getAttribute('data-page'));
        go(state.view, true);
      }
    }
    var apply = ev.target.closest('#papply');
    if (apply) {
      pickQuery.q = el.view.querySelector('#pq').value.trim();
      pickQuery.state = el.view.querySelector('#pstate').value;
      pickQuery.market = el.view.querySelector('#pmarket').value;
      pickQuery.page = 1;
      go('picks', true);
    }
    var settle = ev.target.closest('[data-settle]');
    if (settle) {
      var key = settle.getAttribute('data-settle');
      var outcome = window.prompt(
        'Settle ' + key + '\n\nEnter WIN, LOSS or VOID.\n' +
        'This rewrites graded ledger history and cannot be undone:', '');
      if (outcome === null) return;
      outcome = outcome.trim().toUpperCase();
      if (['WIN', 'LOSS', 'VOID'].indexOf(outcome) === -1) {
        toast('err', 'Invalid result', 'Use WIN, LOSS or VOID.');
        return;
      }
      api.post('/picks/' + encodeURIComponent(key) + '/settle',
               { result: outcome, confirm: true })
        .then(function () { toast('ok', 'Settled', key + ' recorded as ' + outcome + '.'); go(state.view, true); })
        .catch(reportError);
    }
  });

  el.view.addEventListener('keydown', function (ev) {
    if (ev.target.id === 'pq' && ev.key === 'Enter') {
      ev.preventDefault();
      var b = el.view.querySelector('#papply');
      if (b) b.click();
    }
  });

  // ---- Forecast board ------------------------------------------------
  RENDER.forecast = function () {
    return api.get('/forecast').then(function (r) {
      if (!r.board) {
        el.view.innerHTML = empty('No live board', r.reason || 'The poller has not cached a snapshot yet.');
        return '';
      }
      var b = r.board;
      // The board is a bulletin: each row is a fixture whose `market` is the
      // headline quote, with any extra lines in `micro_markets`.
      var fixtures = b.matches || b.fixtures || b.rows || [];
      var rows = fixtures.map(function (f) {
        var marketsText = [];
        if (f.market && f.market.market) {
          marketsText.push('<div class="stat__foot">' + esc(f.market.market) + ': ' +
            esc(f.market.top_outcome || '') +
            (f.market.best_odds ? ' @ ' + esc(f.market.best_odds) : '') + '</div>');
        }
        (f.micro_markets || []).forEach(function (mm) {
          marketsText.push('<div class="stat__foot">' + esc(mm.market || mm.side || '') + ': ' +
            esc(mm.outcome || mm.side || '') +
            ' <span class="mute">' + (mm.priced ? esc(mm.best_odds) : 'unpriced') + '</span></div>');
        });
        return {
          league: '<span class="mono">' + esc(f.league || f.sport_key || '—') + '</span>',
          when: '<span class="nowrap">' + esc(when(f.commence_at)) + '</span>',
          fixture: esc([f.home, f.away].filter(Boolean).join(' v ') || f.match_id || '—'),
          markets: marketsText.join('') || '<span class="mute">—</span>',
          p: num(f.market && f.market.p_top, 3),
          ev: num(f.market && f.market.ev, 4)
        };
      });

      var shortfall = Number(b.shortfall || 0);
      el.view.innerHTML =
        (shortfall > 0
          ? '<div class="note note--warn" style="margin-bottom:14px">' +
            'Shortfall: ' + esc(shortfall) + ' fixture(s) short of this horizon\'s target.' +
            '</div>'
          : '') +
        tableCard('Fixtures in horizon', [
          { key: 'league', label: 'League' },
          { key: 'when', label: 'Kickoff' },
          { key: 'fixture', label: 'Fixture' },
          { key: 'markets', label: 'Markets' },
          { key: 'p', label: 'p', num: true },
          { key: 'ev', label: 'EV', num: true }
        ], rows, { emptyTitle: 'No fixtures in the horizon',
                   emptyBody: (b.mode === 'no_live_data')
                     ? (b.disclaimer || 'The poller has not cached a snapshot.')
                     : 'The poller has not cached a snapshot.' });
      return b.generated_at ? 'generated ' + ago(b.generated_at) : '';
    });
  };

  // ---- Notifications -------------------------------------------------
  var notifQuery = { page: 1, status: '' };

  RENDER.notifications = function () {
    var qs = ['page=' + notifQuery.page, 'page_size=50'];
    if (notifQuery.status) qs.push('status=' + encodeURIComponent(notifQuery.status));

    return api.get('/notifications?' + qs.join('&')).then(function (r) {
      var rows = (r.rows || []).map(function (n) {
        return {
          key: '<span class="mono">' + esc(n.dedupe_key) + '</span>',
          text: '<span class="mute">' + esc((n.text || '').slice(0, 160)) + '</span>',
          status: badge(n.status, n.status === 'SENT' ? 'pos' : (n.status === 'FAILED' ? 'bad' : 'warn')),
          attempts: esc(n.attempts || 0),
          error: n.last_error ? '<span class="neg">' + esc(n.last_error) + '</span>' : '<span class="mute">—</span>',
          when: '<span class="nowrap">' + esc(when(n.created_at)) + '</span>'
        };
      });

      var counts = r.counts || {};
      el.view.innerHTML =
        '<div class="toolbar">' +
          '<select class="select" id="nstatus">' + options([
            ['', 'Any status'], ['PENDING', 'Pending'], ['SENT', 'Sent']
          ], notifQuery.status) + '</select>' +
          '<button class="btn" id="napply">Apply</button>' +
          '<div class="toolbar__spacer"></div>' +
          (isOwner()
            ? '<button class="btn" id="nretry" ' +
              (counts.FAILED || (counts.PENDING || 0) ? '' : 'disabled') +
              '>Retry failed</button>'
            : '') +
        '</div>' +
        tableCard('Outbox', [
          { key: 'key', label: 'Key' },
          { key: 'text', label: 'Message' },
          { key: 'status', label: 'Status' },
          { key: 'attempts', label: 'Attempts', num: true },
          { key: 'error', label: 'Last error' },
          { key: 'when', label: 'Queued' }
        ], rows, {
          emptyTitle: 'Outbox is empty',
          emptyBody: 'Messages appear here when an alert is queued for Telegram.',
          footer: pager(r.page, r.pages, r.total)
        });

      var apply = el.view.querySelector('#napply');
      if (apply) apply.addEventListener('click', function () {
        notifQuery.status = el.view.querySelector('#nstatus').value;
        notifQuery.page = 1;
        go('notifications', true);
      });
      var retry = el.view.querySelector('#nretry');
      if (retry) retry.addEventListener('click', function () {
        if (!confirmHard('Reset the failed messages so the next cycle retries them?')) return;
        api.post('/notifications/retry', { limit: 200 }).then(function (res) {
          toast('ok', 'Retry queued', res.note || (res.requeued + ' message(s) re-queued.'));
          go('notifications', true);
        }).catch(reportError);
      });
      return '';
    });
  };

  // ---- Cache ---------------------------------------------------------
  RENDER.cache = function () {
    return api.get('/cache').then(function (r) {
      var c = r.cache || {};
      var prefixes = Object.keys(c.by_prefix || {}).map(function (k) {
        var v = c.by_prefix[k];
        var n = (v && typeof v === 'object') ? (v.count != null ? v.count : v.entries) : v;
        return { prefix: '<span class="mono">' + esc(k) + '</span>', n: esc(n == null ? 0 : n) };
      });
      el.view.innerHTML =
        '<div class="grid grid--4">' +
          statTile('Entries', esc(c.entries || 0)) +
          statTile('Live', esc(c.live || 0)) +
          statTile('Stale', esc(c.stale || 0), '', c.stale ? 'warn' : '') +
          statTile('Size', bytes(c.bytes)) +
        '</div>' +
        '<div class="grid grid--2" style="margin-top:14px">' +
          tableCard('By prefix', [
            { key: 'prefix', label: 'Prefix' }, { key: 'n', label: 'Entries', num: true }
          ], prefixes, { emptyTitle: 'Cache is empty' }) +
          card('Maintenance', '<div class="note">Purging drops every cached snapshot. ' +
            'The next poll refills it, so this costs one cycle of freshness and no API budget.</div>' +
            (isOwner() ? '<div style="margin-top:12px"><button class="btn btn--danger btn--sm" id="purge">Purge cache</button></div>'
              : '<div class="stat__foot" style="margin-top:10px">Owner role required to purge.</div>')) +
        '</div>';

      var purge = el.view.querySelector('#purge');
      if (purge) purge.addEventListener('click', function () {
        if (!confirmHard('Purge every cached snapshot?')) return;
        api.post('/cache/purge', { confirm: true }).then(function (res) {
          toast('ok', 'Cache purged', (res.pruned || 0) + ' entries removed.');
          go('cache', true);
        }).catch(reportError);
      });
      return c.pruned ? c.pruned + ' entries pruned on last read' : '';
    });
  };

  // ---- Database ------------------------------------------------------
  RENDER.database = function () {
    return api.get('/database').then(function (r) {
      var d = r.database || {};
      var rows = Object.keys(d.row_counts || {}).map(function (k) {
        return { table: '<span class="mono">' + esc(k) + '</span>', n: esc(d.row_counts[k]),
                 cols: esc((d.tables || {})[k]) };
      }).sort(function (a, b) { return Number(b.n) - Number(a.n); });

      el.view.innerHTML =
        '<div class="grid grid--4">' +
          statTile('File size', bytes(d.bytes), bytes(d.bytes_on_disk) + ' on disk') +
          statTile('Integrity', d.integrity === 'ok'
            ? '<span class="pos">OK</span>' : '<span class="neg">' + esc(d.integrity) + '</span>') +
          statTile('Journal', esc(d.journal_mode || '—')) +
          statTile('Pages', esc(d.page_count || 0) + ' × ' + esc(d.page_size || 0) + 'B') +
        '</div>' +
        '<div class="grid grid--2" style="margin-top:14px">' +
          tableCard('Tables', [
            { key: 'table', label: 'Table' },
            { key: 'n', label: 'Rows', num: true },
            { key: 'cols', label: 'Columns', num: true }
          ], rows, { emptyTitle: 'No tables' }) +
          card('Path', '<dl class="kv">' +
            row('File', '<span class="mono">' + esc(d.path || '—') + '</span>') +
            row('Exists', d.exists ? 'yes' : 'no') +
            (d.sidecars ? row('Sidecars', Object.keys(d.sidecars).map(function (s) {
              return esc(s) + ' ' + bytes(d.sidecars[s]);
            }).join('<br>')) : '') +
            '</dl>') +
        '</div>';
      return '';
    });
  };

  // ---- Settings ------------------------------------------------------
  RENDER.operations = function () {
    operationsRefreshedAt = Date.now();
    return api.get('/operations').then(function (r) {
      var evidence = r.pilot || {};
      var fixtureRows = [].concat((r.generation_fixtures || {}).rows || [],
        (r.settlement_fixtures || {}).rows || []).map(function (match) {
        return {
          source: esc(match.source), league: esc(match.league),
          match: esc(match.home) + ' — ' + esc(match.away),
          score: match.score ? esc(match.score.join('–')) : '—',
          status: esc(match.status), kickoff: esc(when(match.kickoff))
        };
      });
      var buttons = r.scheduler === 'serverless' && isOwner()
        ? '<div style="display:flex;gap:8px;flex-wrap:wrap">' +
          ['history', 'generation', 'settlement'].map(function (job) {
            return '<button class="btn" data-paper-job="' + job + '">Run ' + job + '</button>';
          }).join('') + '</div><p id="paper-job-result"></p>' : '';
      el.view.innerHTML = card('Production testing',
        '<p>Saved provider observations and recorded paper runs. Source caches and quotas determine score freshness.</p>' +
        '<p>State: ' + esc(evidence.operational_state || 'unknown') + ' · scheduler: ' + esc(r.scheduler) + '</p>' +
        '<p>' + esc((evidence.blockers || []).join(' · ')) + '</p>' + buttons +
        '<p>Worker observation: ' + esc(when((r.generation_fixtures || {}).worker_observed_at)) +
        ' · settlement observation: ' + esc(when((r.settlement_fixtures || {}).worker_observed_at)) + '</p>' +
        '<p>Provider retrieval, model fitting, pricing and board duration (milliseconds)</p>' +
        '<pre>' + esc(JSON.stringify((r.performance || {}).timings_ms || {}, null, 2)) + '</pre>') +
        tableCard('Recent fixture observations', [
          { key: 'source', label: 'Source' }, { key: 'league', label: 'League' },
          { key: 'match', label: 'Match' }, { key: 'score', label: 'Score' },
          { key: 'status', label: 'Status' }, { key: 'kickoff', label: 'Kickoff' }
        ], fixtureRows, { emptyTitle: 'No fixture observations recorded yet' }) +
        card('Daily publications, jobs and settlements',
          '<pre>' + esc(JSON.stringify(evidence, null, 2)) + '</pre>');
      el.view.querySelectorAll('[data-paper-job]').forEach(function (button) {
        button.addEventListener('click', function () {
          var job = button.getAttribute('data-paper-job');
          button.disabled = true;
          var output = el.view.querySelector('#paper-job-result');
          output.textContent = 'Running ' + job + '; this may take several minutes.';
          api.post('/jobs/' + job, {}).then(function (result) {
            output.textContent = JSON.stringify(result);
          }).catch(function (error) {
            output.textContent = error.message;
          }).finally(function () { button.disabled = false; });
        });
      });
    });
  };

  RENDER.settings = function () {
    return Promise.all([api.get('/settings'), api.get('/providers')]).then(function (responses) {
      var r = responses[0], providers = responses[1];
      var fields = r.fields || [];
      var drafts = {};

      function fieldHtml(f) {
        var id = 'f_' + f.name;
        var hint = [];
        if (f.overridden) hint.push('overridden');
        if (f.min !== null && f.min !== undefined) {
          hint.push(f.max !== null && f.max !== undefined ? f.min + '–' + f.max : 'minimum ' + f.min);
        }
        if (f.name === 'pick_feed_limit') hint.push('0 = all qualifying matches; positive = optional cap');
        if (f.choices && f.choices.length) hint.push(f.choices.join(' | '));

        var control;
        if (f.type === 'bool') {
          control = '<label class="switch"><input type="checkbox" id="' + attr(id) + '"' +
            (f.value ? ' checked' : '') + '><span class="switch__track"></span>' +
            '<span class="switch__text">' + (f.value ? 'Enabled' : 'Disabled') + '</span></label>';
        } else if (f.choices && f.choices.length) {
          control = '<select class="select" id="' + attr(id) + '">' +
            options(f.choices.map(function (c) { return [String(c), String(c)]; }), String(f.value)) +
            '</select>';
        } else if (f.type === 'list') {
          control = '<input class="input input--mono" id="' + attr(id) + '" value="' +
            attr((f.value || []).join(', ')) + '">';
        } else {
          control = '<input class="input input--mono" id="' + attr(id) + '" type="' +
            (f.type === 'int' ? 'number' : 'text') + '" value="' + attr(f.value) + '"' +
            (f.min !== null && f.min !== undefined ? ' min="' + attr(f.min) + '"' : '') +
            (f.max !== null && f.max !== undefined ? ' max="' + attr(f.max) + '"' : '') + '>';
        }
        return '<div class="card" data-field="' + attr(f.name) + '"><div class="stat">' +
          '<div class="stat__label">' + esc(f.name) +
          (f.overridden ? ' <span class="badge badge--brand">edited</span>' : '') + '</div>' +
          '<div style="margin:9px 0">' + control + '</div>' +
          '<div class="stat__foot">' + esc(f.type) +
          (hint.length ? ' · ' + esc(hint.join(' · ')) : '') +
          ' · default ' + esc(Array.isArray(f.default) ? f.default.join(', ') : f.default) +
          '</div></div></div>';
      }

      el.view.innerHTML =
        '<div class="note note--brand" style="margin-bottom:14px">' +
        'Changes apply to the running process on the next cycle — no restart. ' +
        'Provider credentials use the separate controls below and are never displayed.</div>' +
        '<div class="card"><div class="card__body"><h3>Data providers</h3>' +
        '<p>Replace a key, disable a provider, or restore its environment credential. Account quotas remain in force.</p>' +
        '<p id="provider-status">' + (providers.providers || []).map(function (p) {
          return esc(p.provider) + ': ' + (p.configured ? 'configured' : 'missing') + ' (' + esc(p.configuration_source) + ')';
        }).join(' · ') + '</p>' +
        (providers.credential_updates_allowed ?
          '<select class="select" id="provider-name">' + (providers.providers || []).map(function (p) {
            return '<option value="' + attr(p.provider) + '">' + esc(p.provider) + '</option>';
          }).join('') + '</select>' +
          '<input class="input" id="provider-credential" type="password" autocomplete="new-password" placeholder="New API key">' +
          '<button class="btn" data-provider-operation="replace">Save replacement</button>' +
          '<button class="btn" data-provider-operation="disable">Disable provider</button>' +
          '<button class="btn" data-provider-operation="inherit">Use environment key</button>' : '') +
        '<p>OddsPapi quota status: ' + esc(JSON.stringify(providers.oddspapi_status || {})) + '</p>' +
        '<p>The Odds API credits: ' + esc(JSON.stringify(providers.the_odds_api_status || {})) + '</p>' +
        '<details><summary>Coverage and request budget</summary><pre>' +
          esc(JSON.stringify(providers.coverage_plan || {}, null, 2)) + '</pre>' +
          '<p>Settlement budget</p><pre>' + esc(JSON.stringify(providers.settlement_coverage || {}, null, 2)) +
          '</pre></details></div></div>' +
        '<div class="toolbar">' +
          '<button class="btn btn--primary" id="sapply">Apply edited settings</button>' +
          '<button class="btn btn--ghost" id="sreset">Reset all overrides</button>' +
          '<div class="toolbar__spacer"></div>' +
          '<span class="stat__foot">' + Object.keys(r.overrides || {}).length + ' override(s) · revision ' + esc(r.revision) + '</span>' +
        '</div>' +
        '<div class="grid grid--3" id="sgrid">' + fields.map(fieldHtml).join('') + '</div>';

      el.view.querySelectorAll('[data-provider-operation]').forEach(function (button) {
        button.addEventListener('click', function () {
          var name = el.view.querySelector('#provider-name').value;
          var input = el.view.querySelector('#provider-credential');
          var operation = button.getAttribute('data-provider-operation');
          var body = { operation: operation };
          if (operation === 'replace') {
            if (!input.value.trim()) { toast('err', 'Missing credential', 'Enter a replacement credential.'); return; }
            body.credential = input.value;
          }
          input.value = '';
          button.disabled = true;
          api.put('/providers/' + encodeURIComponent(name), body).then(function () {
            body.credential = '';
            toast('ok', 'Provider saved', 'Applies on the next worker cycle.');
            go('settings', true);
          }).catch(function () {
            body.credential = '';
            toast('err', 'Provider update failed', 'Provider configuration could not be saved.');
          }).finally(function () { button.disabled = false; });
        });
      });

      // Keep the switch label honest as it is toggled.
      el.view.querySelectorAll('.switch input').forEach(function (input) {
        input.addEventListener('change', function () {
          input.parentNode.querySelector('.switch__text').textContent =
            input.checked ? 'Enabled' : 'Disabled';
        });
      });

      el.view.querySelector('#sapply').addEventListener('click', function () {
        var updates = {};
        fields.forEach(function (f) {
          var input = el.view.querySelector('#f_' + f.name);
          if (!input) return;
          var current = f.value, next;
          if (f.type === 'bool') next = input.checked;
          else if (f.type === 'int') next = parseInt(input.value, 10);
          else if (f.type === 'float') next = parseFloat(input.value);
          else if (f.type === 'list') next = input.value.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
          else next = input.value;

          var changed = Array.isArray(current)
            ? JSON.stringify(current) !== JSON.stringify(next)
            : String(current) !== String(next);
          if (changed) updates[f.name] = next;
        });

        if (!Object.keys(updates).length) { toast('warn', 'Nothing to apply', 'No field was changed.'); return; }
        api.patch('/settings', { settings: updates }).then(function (res) {
          toast('ok', 'Applied', Object.keys(updates).join(', '));
          go('settings', true);
        }).catch(reportError);
      });

      el.view.querySelector('#sreset').addEventListener('click', function () {
        if (!confirmHard('Reset every setting override back to its environment value?')) return;
        api.post('/settings/reset', {}).then(function () {
          toast('ok', 'Reset', 'All overrides cleared.');
          go('settings', true);
        }).catch(reportError);
      });
      return '';
    });
  };

  // ---- Sports --------------------------------------------------------
  RENDER.sports = function () {
    return Promise.all([api.get('/sports'), api.get('/settings')]).then(function (both) {
      var sp = both[0], st = both[1];
      // The endpoint returns `leagues` (not `catalogue`), and each entry is
      // {key, configured, events_cached} — there is no display name, so the
      // key is what the operator sees.
      var leagues = sp.leagues || [];
      var configured = sp.configured || [];
      var effective = (st && st.effective) || {};

      el.view.innerHTML =
        '<div class="grid grid--2">' +
          card('Cost model',
            '<dl class="kv">' + row('Markets', esc(sp.markets || '—')) +
            row('Regions', esc(sp.regions || '—')) + '</dl>' +
            '<div class="note" style="margin-top:12px">' + esc(sp.cost_note || '') + '</div>') +
          card('Horizon', '<dl class="kv">' +
            row('Board window', esc((effective.forecast_horizon_hours !== undefined
              ? effective.forecast_horizon_hours + 'h' : '—'))) +
            row('Target matches', esc(effective.forecast_min_matches)) +
            row('Max matches', esc(effective.forecast_max_matches)) +
            '</dl>') +
        '</div>' +
        '<div class="toolbar" style="margin-top:14px">' +
          '<button class="btn btn--primary" id="sportsave">Save league selection</button>' +
          '<div class="toolbar__spacer"></div>' +
          '<span class="stat__foot"><span id="sportcount">' + configured.length + '</span> of ' +
            leagues.length + ' selected</span>' +
        '</div>' +
        '<div class="grid grid--3" id="sportgrid">' +
          leagues.map(function (s) {
            return '<label class="card" style="cursor:pointer"><div class="stat">' +
              '<label class="switch"><input type="checkbox" data-sport="' + attr(s.key) + '"' +
              (s.configured ? ' checked' : '') + '><span class="switch__track"></span></label>' +
              '<div class="stat__label" style="margin-top:9px">' + esc(s.key) + '</div>' +
              '<div class="stat__foot">' + (s.events_cached
                ? esc(s.events_cached) + ' cached event(s)' : 'no cached events') + '</div>' +
              '</div></label>';
          }).join('') +
        '</div>';

      el.view.querySelectorAll('[data-sport]').forEach(function (box) {
        box.addEventListener('change', function () {
          var n = el.view.querySelectorAll('[data-sport]:checked').length;
          el.view.querySelector('#sportcount').textContent = n;
        });
      });

      el.view.querySelector('#sportsave').addEventListener('click', function () {
        var sports = [];
        el.view.querySelectorAll('[data-sport]:checked').forEach(function (b) {
          sports.push(b.getAttribute('data-sport'));
        });
        if (!sports.length) {
          toast('err', 'Nothing selected', 'At least one league must be configured.');
          return;
        }
        api.put('/sports', { sports: sports }).then(function (res) {
          toast('ok', 'Saved', sports.length + ' leagues configured.');
          go('sports', true);
        }).catch(reportError);
      });
      return sp.cost_note || '';
    });
  };

  // ---- Users ---------------------------------------------------------
  var userQuery = { page: 1, q: '' };

  RENDER.users = function () {
    var qs = ['page=' + userQuery.page, 'page_size=50'];
    if (userQuery.q) qs.push('q=' + encodeURIComponent(userQuery.q));

    return api.get('/users?' + qs.join('&')).then(function (r) {
      var rows = (r.rows || []).map(function (u) {
        return {
          email: '<span class="mono">' + esc(u.email) + '</span>',
          name: esc(u.display_name || '—'),
          tier: u.tier === 'admin' ? badge('admin', 'brand') : badge(u.tier || 'free'),
          operator: u.is_operator ? badge('operator', 'info') : '<span class="mute">—</span>',
          telegram: u.telegram_id
            ? '<span class="mono">' + esc(u.telegram_id) + '</span>' +
              (u.telegram_verified ? ' ' + badge('verified', 'pos') : '')
            : '<span class="mute">—</span>',
          last: '<span class="nowrap">' + esc(ago(u.last_login_at)) + '</span>',
          action: isOwner()
            ? tierDropdown(u)
            : '<span class="mute">owner only</span>'
        };
      });

      el.view.innerHTML =
        '<div class="toolbar">' +
          '<input class="input" id="uq" placeholder="Search email, name or telegram id" value="' + attr(userQuery.q) + '">' +
          '<button class="btn" id="uapply">Search</button>' +
          '<div class="toolbar__spacer"></div>' +
          '<span class="stat__foot">' + (r.total || 0) + ' account(s)</span>' +
        '</div>' +
        tableCard('Accounts', [
          { key: 'email', label: 'Email' },
          { key: 'name', label: 'Name' },
          { key: 'tier', label: 'Tier' },
          { key: 'operator', label: 'Access' },
          { key: 'telegram', label: 'Telegram' },
          { key: 'last', label: 'Last seen' },
          { key: 'action', label: 'Set tier' }
        ], rows, {
          emptyTitle: 'No accounts match',
          footer: pager(r.page, r.pages, r.total)
        });

      el.view.querySelector('#uapply').addEventListener('click', function () {
        userQuery.q = el.view.querySelector('#uq').value.trim();
        userQuery.page = 1;
        go('users', true);
      });
      el.view.querySelectorAll('.tier-dd').forEach(function (dd) {
        var btn = dd.querySelector('.tier-dd__btn');
        var menu = dd.querySelector('.tier-dd__menu');
        btn.addEventListener('click', function (ev) {
          ev.stopPropagation();
          var willOpen = !dd.classList.contains('is-open');
          el.view.querySelectorAll('.tier-dd.is-open').forEach(function (o) {
            o.classList.remove('is-open');
            o.querySelector('.tier-dd__menu').hidden = true;
          });
          if (!willOpen) return;
          var rect = btn.getBoundingClientRect();
          menu.style.top = (rect.bottom + 4) + 'px';
          menu.style.left = rect.left + 'px';
          menu.hidden = false;
          dd.classList.add('is-open');
        });
        dd.querySelectorAll('.tier-dd__opt').forEach(function (opt) {
          opt.addEventListener('click', function (ev) {
            ev.stopPropagation();
            menu.hidden = true;
            dd.classList.remove('is-open');
            if (opt.disabled) return;
            var id = dd.getAttribute('data-dduser');
            var tier = opt.getAttribute('data-ddtier');
            api.patch('/users/' + encodeURIComponent(id), { tier: tier }).then(function () {
              toast('ok', 'Tier updated', 'Set to ' + tier + '.');
              go('users', true);
            }).catch(function (err) {
              reportError(err);
              go('users', true);
            });
          });
        });
      });
      return '';
    });
  };

  // ---- Audit ---------------------------------------------------------
  var auditQuery = { page: 1 };

  RENDER.audit = function () {
    return api.get('/audit?page=' + auditQuery.page + '&page_size=100').then(function (r) {
      var rows = (r.rows || []).map(function (a) {
        var detail = a.details;
        try { detail = JSON.stringify(JSON.parse(a.details)); } catch (e) { /* already a string */ }
        return {
          when: '<span class="nowrap">' + esc(when(a.timestamp)) + '</span>',
          who: '<span class="mono">' + esc(a.admin_id || '—') + '</span>',
          action: '<span class="mono">' + esc(a.action) + '</span>',
          target: esc(a.target || '—'),
          detail: '<span class="mute">' + esc((detail || '').slice(0, 120)) + '</span>'
        };
      });
      el.view.innerHTML = tableCard('Audit log', [
        { key: 'when', label: 'When' },
        { key: 'who', label: 'Admin' },
        { key: 'action', label: 'Action' },
        { key: 'target', label: 'Target' },
        { key: 'detail', label: 'Detail' }
      ], rows, {
        emptyTitle: 'No console activity yet',
        emptyBody: 'Every sign-in and every write is recorded here.',
        footer: pager(r.page, r.pages, r.total)
      });
      return '';
    });
  };

  // ---- System controls -----------------------------------------------
  RENDER.system = function () {
    if (!isOwner()) {
      el.view.innerHTML = empty('Owner role required', 'These controls change how the running process behaves.');
      return '';
    }
    return Promise.all([api.get('/overview'), api.get('/telemetry')]).then(function (both) {
      var o = both[0];
      var paused = !!o.paused;

      var tele = (both[1].telemetry || []).map(function (t) {
        var v = typeof t.value === 'object' ? JSON.stringify(t.value) : String(t.value);
        return {
          key: '<span class="mono">' + esc(t.key) + '</span>',
          value: t.redacted
            ? '<span class="mute">' + esc(v) + '</span>'
            : '<span class="mono">' + esc((v || '').slice(0, 200)) + '</span>',
          when: '<span class="nowrap">' + esc(when(t.updated_at)) + '</span>'
        };
      });

      el.view.innerHTML =
        '<div class="grid grid--2">' +
          card('Ingestion',
            '<div class="note ' + (paused ? 'note--warn' : '') + '">Polling is currently ' +
            (paused ? '<strong>paused</strong>.' : 'running.') + '</div>' +
            '<div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">' +
              '<button class="btn" id="pollyCycle">Run poll cycle</button>' +
              '<button class="btn" id="pollySettle">Run settlement</button>' +
              '<button class="btn ' + (paused ? 'btn--danger' : 'btn--ghost') + '" id="togPause">' +
                (paused ? 'Resume ingestion' : 'Pause ingestion') + '</button>' +
            '</div>' +
            '<div class="stat__foot" style="margin-top:12px">' +
              'A poll cycle spends API budget for every configured league. ' +
              'Settlement only grades what has already finished and costs nothing.</div>') +
          card('Runtime', '<dl class="kv">' +
            row('Version', esc(o.version)) +
            row('Process', esc(o.pid) + ' · up ' + duration(o.uptime_seconds)) +
            row('Scheduler', o.scheduler && o.scheduler.present ? 'attached' : 'not attached') +
            row('Odds pool', o.odds_pool && o.odds_pool.present ? esc(o.odds_pool.size) + ' keys' : 'not attached') +
            '</dl>' +
            (o.scheduler && o.scheduler.next_cycle_at
              ? '<div class="stat__foot" style="margin-top:10px">Next cycle ' +
                esc(ago(o.scheduler.next_cycle_at)) + ' · next settlement ' +
                esc(ago(o.scheduler.next_settle_at)) + '</div>' : '')) +
        '</div>' +
        '<div style="margin-top:14px">' +
          tableCard('Stored telemetry', [
            { key: 'key', label: 'Key' }, { key: 'value', label: 'Value' }, { key: 'when', label: 'Updated' }
          ], tele, { emptyTitle: 'No telemetry stored' }) +
        '</div>';

      el.view.querySelector('#pollyCycle').addEventListener('click', function () {
        if (!confirmHard('Run a full poll cycle now across every configured league?')) return;
        api.post('/system/poll', { what: 'cycle', mode: 'sync', confirm: true }).then(function (r) {
          var s = r.summary || {};
          toast('ok', 'Cycle finished',
            'ran_cycle=' + s.ran_cycle + ' · ' + (s.errors || []).length + ' error(s)' +
            (s.skipped_reason ? ' · ' + s.skipped_reason : ''));
          go(state.view, true);
        }).catch(reportError);
      });

      el.view.querySelector('#pollySettle').addEventListener('click', function () {
        api.post('/system/poll', { what: 'settlement', mode: 'sync', confirm: true }).then(function (r) {
          var s = r.summary || {};
          toast('ok', 'Settlement finished', 'graded ' + ((s.settlement || {}).settled || 0) + ' pick(s)');
          go(state.view, true);
        }).catch(reportError);
      });

      el.view.querySelector('#togPause').addEventListener('click', function () {
        if (!paused && !confirmHard('Pause all ingestion?')) return;
        api.post('/system/pause', { paused: !paused, confirm: true }).then(function () {
          toast('ok', paused ? 'Resumed' : 'Paused', paused ? 'Ingestion is running again.' : 'Ingestion is paused.');
          go(state.view, true);
        }).catch(reportError);
      });
      return '';
    });
  };

  /* ==================================================================
     Boot
     ================================================================== */

  function boot() {
    // The cookie is the authority; sessionStorage only lets the console paint
    // the right shell before the first round trip resolves.
    api.get('/session', { anonymous: true }).then(function (s) {
      if (s.authenticated) {
        state.role = s.role;
        state.user = s.user;
        state.csrf = s.csrf_token;
        state.expiresAt = s.expires_at;
        var remembered = recallSession();
        var wanted = null;
        try { wanted = sessionStorage.getItem(SESSION_KEY + '_view'); } catch (e) {}
        if (remembered && remembered.view) wanted = remembered.view;
        enterConsole();
        if (wanted) go(wanted);
      } else {
        showGate();
      }
    }).catch(function () {
      showGate('Could not reach the console API.');
    });
  }

  boot();
})();
