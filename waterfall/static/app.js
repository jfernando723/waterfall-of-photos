/* Waterfall's front end. One file, no framework, no build step.

   Four ideas carry most of it:

   1. The wall and the viewer are two views of ONE list. Tapping a tile opens
      the viewer at that item and swiping continues in the same order.
   2. The viewer's deck never holds more than the current item, the one before
      and the two after. A swipe physically cannot travel further than that,
      which is what makes three quick swipes move three items and not three
      hundred. After each move the deck is quietly re-centred.
   3. The next few items are fetched before they are needed. Over a 275 ms
      link that is the difference between a swipe that shows a picture and a
      swipe that shows a spinner.
   4. There are two walls, Visible and Hidden, and they are never mixed. Which
      one an item is on is decided by the folder it was put in, not here.
   5. An item has one home, its folder. A tag lets it appear in another
      category as well, without a second copy of the file.
   6. A Live Photo is a photo that also has a short clip. It is shown as the
      photo; holding a finger on it, or tapping LIVE, plays the clip over it.
   7. The admin can send files from this page, a few megabytes at a time, so a
      phone can add to the library without anything else installed.
   8. Being the admin lasts only while the page is being used. Left untouched
      for half an hour, it goes back to the wall everyone sees.
*/
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

  // ------------------------------------------------------------ settings --
  const DEFAULTS = { order: 'shuffle', videoMin: 0, photoHigh: false, ahead: 3, motion: true };
  const prefs = Object.assign({}, DEFAULTS, readStore());
  function readStore() {
    try { return JSON.parse(localStorage.getItem('waterfall.prefs') || '{}'); } catch { return {}; }
  }
  function savePrefs() {
    try { localStorage.setItem('waterfall.prefs', JSON.stringify(prefs)); } catch { /* private tab */ }
  }

  const state = {
    me: null, pipeline: 1,
    realm: 'visible',
    sections: [], sectionById: new Map(), totals: { photos: 0, videos: 0 }, tags: [],
    section: null, tag: null, kind: 'all', seed: newSeed(),
    items: [], more: true, loading: false, pending: null, generation: 0,
    cols: [], heights: [],
    viewing: false, current: -1, wantSound: false,
  };
  function newSeed() { return 1 + Math.floor(Math.random() * 2147483000); }
  const isAdmin = () => !!(state.me && state.me.admin);
  // The auto-scroll (see "auto-scroll", below). Kept up here because the
  // admin's clock, next, has to tell its moves from a person's.
  const auto = { running: false, quietUntil: 0, last: 0, shown: 0, plays: 0, done: false, doneAt: 0,
                 moving: false, userPaused: false, progressAt: 0, warmed: null };

  // -------------------------------------------------------- link measure --
  // The phone already downloads images constantly, so it can time them
  // instead of running a speed test. Cached files report zero bytes on the
  // wire and are skipped. Several downloads at once each see a share of the
  // link, so this errs low, which is the safe direction for picking a size.
  const link = { bps: 0, rtt: 0, samples: 0 };
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        if (!e.name.includes('/m/') || !e.transferSize || e.transferSize < 30000) continue;
        const ms = e.responseEnd - e.responseStart;
        if (ms < 15) continue;
        const bps = e.transferSize * 8000 / ms;
        link.bps = link.samples ? link.bps * 0.7 + bps * 0.3 : bps;
        const rtt = e.responseStart - e.requestStart;
        if (rtt > 0) link.rtt = link.rtt ? link.rtt * 0.7 + rtt * 0.3 : rtt;
        link.samples += 1;
      }
    }).observe({ type: 'resource', buffered: true });
  } catch { /* older browser: sizes fall back to the middle of the ladder */ }

  // ----------------------------------------------------------------- api --
  async function api(path, body) {
    const options = { credentials: 'same-origin' };
    if (body !== undefined) {
      options.method = 'POST';
      options.headers = { 'Content-Type': 'application/json', 'X-Waterfall': '1' };
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({}));
    if (response.status === 401 && !path.startsWith('/api/login')) {
      await refreshMe();
      showGate();
      throw new Error('Signed out.');
    }
    if (response.status === 403 && isAdmin() && path.startsWith('/api/admin/')) {
      // The admin sign-in has lapsed on the server. Go where that leaves us.
      await refreshMe().catch(() => {});
      if (!isAdmin()) setRealm('visible');
    }
    if (!response.ok) throw new Error(data.error || `Something went wrong (${response.status}).`);
    return data;
  }
  // A page left open while Waterfall is updated goes on running the code it
  // was given, against a server that has moved on, and the Version line in its
  // menu (which the server supplies) says the new number all the same. So the
  // page knows which build it is, and loads itself again when the server has
  // another, at a moment when nobody is in the middle of anything.
  const BUILD = document.body.dataset.build || '';
  function reloadIfStale() {
    if (!BUILD || !state.me || !state.me.build || state.me.build === BUILD) return;
    if (up.running || state.viewing || !$('sheet').hidden) return;
    location.reload();
  }
  async function refreshMe() {
    const was = isAdmin();
    state.me = await (await fetch('/api/me', { credentials: 'same-origin' })).json();
    state.pipeline = state.me.pipeline;
    if (state.me.admin && !was) {
      // Newly the admin (or the page reloaded as one): the clock starts from
      // whatever the sign-in itself has left.
      lastActive = lastRenewed = Date.now() - Math.max(0, (state.me.admin_idle - state.me.admin_left) * 1000);
    }
    // The Hidden wall's place is kept only while the sign-in lasts. Taking the
    // wall itself off the screen is setRealm's: whoever asked does that.
    if (!state.me.admin) forgetHiddenPlace();
  }
  const murl = (item, name) => `/m/${state.pipeline}/${item.key}/${name}`;
  // A photo at one of its three sizes, 't', 'm' or 'l'. A Live Photo shown by
  // a moment of its clip has that moment's three sizes, in a folder named for
  // what is in them (`kf`), so a new choice is never hidden by an old cache.
  const photoUrl = (item, size) => murl(item, (item.kf ? item.kf + '/' : '') + size + '.webp');

  // ------------------------------------------------------------- helpers --
  const fmtBytes = (n) => n >= 1e9 ? (n / 1e9).toFixed(1) + ' GB'
    : n >= 1e6 ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + ' MB' : Math.max(1, Math.round(n / 1e3)) + ' KB';
  const fmtClock = (s) => {
    s = Math.max(0, Math.round(s || 0));
    const m = Math.floor(s / 60), h = Math.floor(m / 60);
    return h ? `${h}:${String(m % 60).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
      : `${m}:${String(s % 60).padStart(2, '0')}`;
  };
  const fmtSpan = (s) => s >= 172800 ? Math.round(s / 86400) + ' days'
    : s >= 7200 ? Math.round(s / 3600) + ' hours' : s >= 120 ? Math.round(s / 60) + ' minutes' : 'a minute or two';
  function fmtTaken(local) {
    if (!local) return '';
    const [d, t] = local.split(' ');
    const [y, mo, day] = d.split('-').map(Number);
    const [h, mi] = (t || '0:0').split(':').map(Number);
    const date = new Date(y, mo - 1, day, h, mi);
    return date.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })
      + ' at ' + date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  }
  const sectionName = (id) => {
    const s = state.sectionById.get(id);
    return s ? s.name : '';
  };
  const plural = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? '' : 's'}`;

  // ---------------------------------------------------------- admin idle --
  // The admin stays the admin only while somebody is using the page. Touching
  // it renews the sign-in; left alone for the time the server names (half an
  // hour), the page signs out and shows the Visible wall, so a phone put down
  // unlocked does not go on showing the Hidden one. Only a person counts:
  // a video left playing or the status ticking over does not.
  let lastActive = Date.now();
  let lastRenewed = Date.now();
  const idleLimit = () => ((state.me && state.me.admin_idle) || 1800) * 1000;
  function noteActivity(event) {
    // The auto-scroll moving on scrolls the viewer, and that is nobody's touch.
    if (event && event.type === 'scroll' && performance.now() < auto.quietUntil) return;
    lastActive = Date.now();
    if (isAdmin() && lastActive - lastRenewed > 120000) {
      lastRenewed = lastActive;
      api('/api/admin/touch', {}).catch(() => {});
    }
  }
  for (const type of ['pointerdown', 'keydown', 'wheel', 'touchstart', 'scroll']) {
    window.addEventListener(type, noteActivity, { passive: true, capture: true });
  }
  let leaving = false;
  async function leaveAdminIfIdle() {
    if (leaving || !isAdmin() || Date.now() - lastActive < idleLimit()) return;
    leaving = true;
    try {
      await api('/api/logout', {}).catch(() => {});
      await refreshMe().catch(() => {});
      if (!state.me || !state.me.role) showGate();
      // An auto-scroll on the Visible wall carries on (the owner's choice): that
      // wall is anyone's to see. Anywhere else the wall is put back as ever,
      // which on the Hidden one closes the viewer and so stops it.
      else if (auto.running && state.realm === 'visible' && $('sheet').hidden) return;
      else await setRealm('visible');
    } finally { leaving = false; }
  }
  setInterval(leaveAdminIfIdle, 15000);
  // A phone's timers stop while it is asleep or the page is in the background,
  // so the moment the page is looked at again is when this has to be asked.
  document.addEventListener('visibilitychange', () => { if (!document.hidden) leaveAdminIfIdle(); });
  window.addEventListener('pageshow', leaveAdminIfIdle);

  // ---------------------------------------------------------------- gate --
  // Normally nobody sees this: opening the address shows the wall. It appears
  // once, to choose the admin password, and thereafter only if a viewing
  // password has been set.
  function showGate() {
    unwindHistory();
    closeViewer(true);
    closeSheet();
    $('wall').hidden = true;
    $('gate').hidden = false;
    const setup = state.me && !state.me.configured;
    $('gate-lead').textContent = setup
      ? 'First visit. Choose the admin password: it unlocks the Hidden wall and the import door that photos and videos come in by.'
      : 'Enter the password.';
    $('gate-label').textContent = setup ? 'Admin password' : 'Password';
    $('gate-submit').textContent = setup ? 'Set up' : 'Continue';
    $('gate-password').autocomplete = setup ? 'new-password' : 'current-password';
    $('gate-password').value = '';
    $('gate-error').textContent = '';
  }

  $('gate-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = $('gate-submit');
    button.disabled = true;
    $('gate-error').textContent = '';
    try {
      if (!state.me.configured) await api('/api/setup', { admin: $('gate-password').value });
      else await api('/api/login', { password: $('gate-password').value });
      await start();
    } catch (error) {
      $('gate-error').textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });

  async function start() {
    await refreshMe();
    if (!state.me.configured || !state.me.role) return showGate();
    $('gate').hidden = true;
    $('wall').hidden = false;
    setPressed('order', 'order', prefs.order);
    await loadSections();
    resetWall();
  }

  // ---------------------------------------------------------------- wall --
  function setPressed(groupId, attr, value) {
    for (const b of $(groupId).querySelectorAll('button')) {
      b.setAttribute('aria-pressed', String(b.dataset[attr] === String(value)));
    }
  }

  async function loadSections() {
    const realm = state.realm;
    const data = await api('/api/sections?realm=' + realm);
    if (realm !== state.realm) return;      // the wall was switched meanwhile: these are the other's
    state.sections = data.sections;
    state.totals = data.totals;
    state.sectionById = new Map(data.sections.map((s) => [s.id, s]));
    state.tags = data.tags || [];
    if (state.section !== null && !state.sectionById.has(state.section)) state.section = null;
    if (state.tag !== null && !state.tags.some((t) => t.tag.toLowerCase() === state.tag.toLowerCase())) state.tag = null;
  }

  // Switching wall is the one place the two are told apart. The screen is
  // emptied at once and filled again from the other wall's own items, so no
  // item from one can linger on screen in the other. Each wall comes back as
  // it was left (see "places", below); the same wall asked for again starts
  // afresh, as it always has.
  async function setRealm(realm) {
    const target = realm === 'hidden' && isAdmin() ? 'hidden' : 'visible';
    const switching = target !== state.realm;
    if (switching) keepPlace();
    unwindHistory();
    closeViewer(true);
    closeSheet();
    const place = !switching ? null : target === 'hidden' ? places.hidden || readHiddenPlace() : places.visible;
    state.realm = target;
    if (place) return showPlace(place);
    state.section = null;
    state.tag = null;
    clearWall();
    try { await loadSections(); } catch { /* the wall will say what is wrong */ }
    if (state.realm === target) resetWall();
  }

  // ------------------------------------------------------------- places --
  // Each wall keeps its place while the other one is shown: what had been
  // loaded, in what order, how far down, and which section, tag or kind.
  //
  // Leaving Waterfall while the Hidden wall is up puts the Visible wall back at
  // once, whatever the device: another app, the Home Screen, the phone locked,
  // another tab, or Waterfall closed. So whoever opens it next sees the Visible
  // wall, and the Wall switch brings the Hidden one back as it was for as long
  // as the admin is still signed in. That place is also written down on this
  // device, so that it outlives Waterfall being closed: which section, which
  // order, how far down, and nothing else, no picture and no file's name. It
  // is forgotten the moment the sign-in ends.
  //
  // One thing no page can do: a phone keeps a picture of the screen as it was
  // left, for its list of open apps, and takes it itself.
  const places = { visible: null, hidden: null };
  const HIDDEN_PLACE = 'waterfall.hidden-place';

  function tileNearTop() {
    const below = $('bar').getBoundingClientRect().bottom;
    let best = -1, top = Infinity;
    for (const tile of $('grid').querySelectorAll('.tile')) {
      const box = tile.getBoundingClientRect();
      if (box.bottom > below && box.top < top) { best = Number(tile.dataset.index); top = box.top; }
    }
    return best;
  }

  function keepPlace() {
    if (state.realm === 'hidden' && !isAdmin()) return forgetHiddenPlace();
    places[state.realm] = {
      section: state.section, tag: state.tag, kind: state.kind, order: prefs.order, seed: state.seed,
      items: state.items, more: state.more, sections: state.sections, totals: state.totals, tags: state.tags,
      // Where it was: the item open in the viewer, or else the scroll position,
      // which is exact while the window is as wide (the tiles lie as they did).
      y: Math.round(window.scrollY), width: window.innerWidth,
      viewing: state.viewing, at: state.viewing ? state.current : tileNearTop() };
    if (state.realm === 'hidden') writeHiddenPlace();
  }

  function writeHiddenPlace() {
    const place = places.hidden;
    if (!place) return;
    const { section, tag, kind, order, seed, more, y, width, viewing, at } = place;
    try {
      localStorage.setItem(HIDDEN_PLACE, JSON.stringify({
        section, tag, kind, order, seed, more, y, width, viewing, at,
        count: place.items ? place.items.length : place.count,
        until: lastActive + idleLimit() }));     // never past the end of the sign-in it was kept under
    } catch { /* storage refused (a private window): it is kept while the page lives */ }
  }

  function readHiddenPlace() {
    try {
      const place = JSON.parse(localStorage.getItem(HIDDEN_PLACE) || 'null');
      if (place && place.until > Date.now()) return place;
    } catch { /* unreadable: the wall starts afresh */ }
    forgetHiddenPlace();
    return null;
  }

  function forgetHiddenPlace() {
    places.hidden = null;
    try { localStorage.removeItem(HIDDEN_PLACE); } catch { /* nothing was kept */ }
  }

  async function showPlace(place) {
    Object.assign(state, { section: place.section, tag: place.tag, kind: place.kind, seed: place.seed });
    prefs.order = place.order;
    savePrefs();
    setPressed('order', 'order', prefs.order);
    setPressed('kind', 'kind', state.kind);
    if (place.items) {
      // Kept while the page lived: shown at once, with nothing to ask the server.
      Object.assign(state, { sections: place.sections, totals: place.totals, tags: place.tags,
                             sectionById: new Map(place.sections.map((s) => [s.id, s])) });
      resetWall(place);
      return goBackTo(place);
    }
    // Written down before Waterfall was closed: the same pages are asked for
    // again with the same order and the same shuffle, and come back the same.
    const realm = state.realm;
    clearWall();
    try { await loadSections(); } catch { /* the wall will say what is wrong */ }
    if (state.realm !== realm) return;
    resetWall();
    const generation = state.generation;
    await loadUpTo(place.count);
    if (generation === state.generation) goBackTo(place);
  }

  function goBackTo(place) {
    if (!place.viewing && place.width === window.innerWidth) return window.scrollTo(0, place.y);
    const tile = $('grid').querySelector(`.tile[data-index="${place.at}"]`);
    if (tile) tile.scrollIntoView({ block: 'center' });
  }

  // Leaving while the Hidden wall is up. Everything that takes it off the
  // screen happens before the first wait for the server, so it is done before
  // the page is put to sleep; the Visible wall is drawn from what was kept.
  function leftWaterfall() {
    if (state.realm === 'hidden') setRealm('visible');
    else writeHiddenPlace();            // a place kept from before: good for as long as the sign-in
  }
  document.addEventListener('visibilitychange', () => { if (document.hidden) leftWaterfall(); });
  window.addEventListener('pagehide', leftWaterfall);

  function columnCount() {
    const w = window.innerWidth;
    return w < 560 ? 2 : w < 900 ? 3 : w < 1300 ? 4 : 5;
  }

  function buildColumns() {
    const grid = $('grid');
    grid.textContent = '';
    state.cols = [];
    state.heights = [];
    for (let i = 0; i < columnCount(); i++) {
      state.cols.push(grid.appendChild(el('div', 'col')));
      state.heights.push(0);
    }
  }

  // Nothing on the wall, at once: what is drawn next belongs to state.realm.
  function clearWall() {
    stopAllPreviews();
    inView.clear();
    previewWatcher.disconnect();
    state.generation += 1;
    state.items = [];
    state.more = true;
    state.loading = false;
    state.pending = null;
    buildColumns();
    const hidden = state.realm === 'hidden';
    $('bar').classList.toggle('is-hidden', hidden);
    $('realm-tag').hidden = !hidden;
    $('title-text').textContent = 'Everything';
    $('wall-count').textContent = '';
    $('wall-status').textContent = '';
    window.scrollTo(0, 0);
  }

  // The wall afresh, or, given a place kept with its items, as it was left.
  function resetWall(kept) {
    clearWall();
    const current = state.section === null ? null : state.sectionById.get(state.section);
    const tagged = state.tag === null ? null
      : state.tags.find((t) => t.tag.toLowerCase() === state.tag.toLowerCase());
    $('title-text').textContent = state.tag !== null ? '#' + state.tag : current ? current.name : 'Everything';
    const totals = tagged ? [tagged.photos, tagged.videos]
      : current ? [current.total_photos, current.total_videos]
      : [state.totals.photos, state.totals.videos];
    const shown = state.kind === 'photo' ? totals[0] : state.kind === 'video' ? totals[1] : totals[0] + totals[1];
    $('wall-count').textContent = shown ? plural(shown, 'item') : '';
    if (!kept || !kept.items) return loadMore();
    state.items = kept.items.slice();
    state.more = kept.more;
    state.items.forEach(addTile);
    sayWhatIsLeft();
  }

  function sayWhatIsLeft() {
    if (!state.items.length) {
      $('wall-status').textContent = isAdmin()
        ? 'Nothing here yet. Add photos and videos from the ··· menu.'
        : 'Nothing here yet.';
    } else if (!state.more) {
      $('wall-status').textContent = 'That is everything.';
    }
  }

  // The next page of the feed. Asked again while one is on its way, it hands
  // back the same one rather than asking twice.
  function loadMore(limit = 40) {
    if (state.loading) return state.pending;
    if (!state.more) return Promise.resolve();
    state.loading = true;
    const generation = state.generation;
    const query = new URLSearchParams({
      realm: state.realm, order: prefs.order, seed: state.seed,
      offset: state.items.length, limit, kind: state.kind,
    });
    if (state.section !== null) query.set('section', state.section);
    if (state.tag !== null) query.set('tag', state.tag);
    state.pending = (async () => {
      try {
        const data = await api('/api/feed?' + query);
        if (generation !== state.generation) return;      // the wall was reset meanwhile
        const base = state.items.length;
        state.items.push(...data.items);
        state.more = data.more;
        data.items.forEach((item, i) => addTile(item, base + i));
        sayWhatIsLeft();
      } catch (error) {
        if (generation === state.generation && error.message !== 'Signed out.') {
          $('wall-status').textContent = error.message;
        }
      } finally {
        if (generation === state.generation) { state.loading = false; state.pending = null; }
      }
    })();
    return state.pending;
  }

  // At least `count` items, as many pages as that takes (to bring back a place).
  async function loadUpTo(count) {
    const generation = state.generation;
    while (generation === state.generation && state.more && state.items.length < count) {
      const had = state.items.length;
      await loadMore(100);
      if (state.items.length === had) break;      // nothing came: the wall says why
    }
  }

  function addTile(item, index) {
    const ratio = Math.min(2.2, Math.max(0.45, item.h / item.w));   // tame panoramas
    const tile = el('button', 'tile');
    tile.style.aspectRatio = `1 / ${ratio}`;
    if (item.ph) tile.style.backgroundImage = `url(data:image/webp;base64,${item.ph})`;
    tile.dataset.index = index;
    const img = el('img');
    img.loading = 'lazy';
    img.decoding = 'async';
    img.alt = '';
    img.addEventListener('load', () => img.classList.add('in'), { once: true });
    img.src = photoUrl(item, 't');
    tile.appendChild(img);
    if (item.k === 'v') {
      tile.appendChild(el('span', 'badge', fmtClock(item.d)));
      if (item.p) previewWatcher.observe(tile);
    } else if (item.lv) {
      tile.appendChild(el('span', 'badge live', 'LIVE'));
    }
    // Always add to the shortest column, so the wall grows evenly and nothing
    // already on screen ever moves when more arrives.
    let shortest = 0;
    for (let i = 1; i < state.heights.length; i++) {
      if (state.heights[i] < state.heights[shortest]) shortest = i;
    }
    state.cols[shortest].appendChild(tile);
    state.heights[shortest] += ratio;
  }

  // Moving previews. A video tile that has settled on screen plays a few silent
  // seconds, so the wall moves. Three rules keep it cheap: only
  // tiles that are properly in view, only after the scrolling pauses, and only
  // a few at once (a phone has a limited number of video decoders, and the
  // viewer needs them more).
  const MAX_PREVIEWS = 3;
  const previewing = new Map();        // tile -> its <video>
  const inView = new Set();
  let previewTimer = 0;
  const stillPreferred = window.matchMedia('(prefers-reduced-motion: reduce)');

  function stopPreview(tile) {
    const video = previewing.get(tile);
    if (!video) return;
    video.pause();
    video.removeAttribute('src');
    video.load();                      // gives the decoder back straight away
    video.remove();
    previewing.delete(tile);
  }
  function stopAllPreviews() {
    clearTimeout(previewTimer);
    for (const tile of [...previewing.keys()]) stopPreview(tile);
  }
  function startPreview(tile) {
    const item = state.items[Number(tile.dataset.index)];
    if (!item) return;
    const video = el('video');
    video.muted = true;
    video.loop = true;
    video.playsInline = true;
    video.setAttribute('muted', '');
    video.setAttribute('playsinline', '');
    video.disableRemotePlayback = true;
    video.addEventListener('playing', () => video.classList.add('in'), { once: true });
    video.src = murl(item, 'p.mp4');
    tile.insertBefore(video, tile.querySelector('.badge'));
    previewing.set(tile, video);
    // If the phone refuses (Low Power Mode does), the still picture simply stays.
    video.play().catch(() => stopPreview(tile));
  }
  function schedulePreviews() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(() => {
      if (state.viewing || !prefs.motion || stillPreferred.matches || document.hidden) return;
      for (const tile of inView) {
        if (previewing.size >= MAX_PREVIEWS) break;
        if (!previewing.has(tile) && tile.isConnected) startPreview(tile);
      }
    }, 280);
  }
  const previewWatcher = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (entry.intersectionRatio >= 0.6) inView.add(entry.target);
      else { inView.delete(entry.target); stopPreview(entry.target); }
    }
    schedulePreviews();
  }, { threshold: [0, 0.6] });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopAllPreviews(); else schedulePreviews();
  });

  $('grid').addEventListener('click', (event) => {
    const tile = event.target.closest('.tile');
    if (tile) openViewer(Number(tile.dataset.index));
  });

  new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) loadMore();
  }, { rootMargin: '1400px' }).observe($('sentinel'));

  function setSection(id) {
    unwindHistory();
    closeViewer(true);
    closeSheet();
    state.section = id;
    state.tag = null;
    resetWall();
  }
  // A tag is a category too: everything carrying it, wherever it lives.
  async function setTag(tag) {
    unwindHistory();
    closeViewer(true);
    closeSheet();
    state.section = null;
    state.tag = tag;
    try { await loadSections(); } catch { /* the wall will say what is wrong */ }
    resetWall();
  }

  $('order').addEventListener('click', (event) => {
    const button = event.target.closest('button');
    if (!button) return;
    // Pressing Shuffle again deals a new hand.
    if (button.dataset.order === 'shuffle') state.seed = newSeed();
    prefs.order = button.dataset.order;
    savePrefs();
    setPressed('order', 'order', prefs.order);
    resetWall();
  });
  $('kind').addEventListener('click', (event) => {
    const button = event.target.closest('button');
    if (!button) return;
    state.kind = button.dataset.kind;
    setPressed('kind', 'kind', state.kind);
    resetWall();
  });

  window.addEventListener('resize', () => {
    if (state.viewing) layoutDeck();
    if (columnCount() !== state.cols.length) {
      stopAllPreviews();
      inView.clear();
      previewWatcher.disconnect();
      buildColumns();
      state.items.forEach(addTile);
    }
  });

  // -------------------------------------------------------------- viewer --
  const deck = $('deck');
  const slides = new Map();           // item index -> element
  const BEHIND = 1, AHEAD = 2;        // how much of the list the deck ever holds
  let slideHeight = 0;
  let deckBase = 0;                   // item index of the deck's first slide
  let settle = 0;                     // timer: the scroll has come to rest
  let relayoutUntil = 0;
  const HLS_NATIVE = !!document.createElement('video').canPlayType('application/vnd.apple.mpegurl');

  // Which video size to ask for. On an iPhone "auto" is a playlist and the
  // phone's own player keeps adapting; the server puts the best starting size
  // first using the speed measured here. A chosen size is that size and
  // nothing else: the player waits for it rather than dropping lower.
  function videoSource(item, choice) {
    const rungs = (item.r || ['360']).map(Number);
    if (choice && choice !== 'auto') {
      return murl(item, HLS_NATIVE ? `v/${choice}.m3u8` : `v/${choice}.m4s`);
    }
    const bw = Math.round(link.bps || 2500000);
    if (HLS_NATIVE) return murl(item, `v/master.m3u8?min=${prefs.videoMin}&bw=${bw}`);
    const afford = bw > 13e6 ? 1440 : bw > 7e6 ? 1080 : bw > 3.4e6 ? 720 : bw > 1.7e6 ? 540 : 360;
    const want = Math.max(afford, prefs.videoMin);
    return murl(item, `v/${rungs.filter((r) => r <= want).pop() || rungs[0]}.m4s`);
  }

  function buildSlide(index) {
    const item = state.items[index];
    const slide = el('div', 'slide');
    slide.dataset.index = index;
    if (item.ph) slide.style.backgroundImage = `url(data:image/webp;base64,${item.ph})`;
    if (item.k === 'v') {
      const video = el('video');
      video.playsInline = true;
      video.setAttribute('playsinline', '');
      video.muted = true;
      video.loop = true;
      video.poster = murl(item, 'm.webp');
      // The auto-scroll waiting for full quality shows the best copy that
      // arrives in time, and fetches nothing of a video ahead but the one it
      // is fetching whole (see autoSize and wholeInto); otherwise, and for
      // the video it starts on, which there was no time to fetch, the size
      // follows the link.
      const size = autoFull() && (index !== state.current || whole.item === item) ? autoSize(item) : 'auto';
      video.preload = autoFull() && index !== state.current ? 'none' : 'metadata';
      if (!wholeInto(video, item)) video.src = videoSource(item, size);
      slide.dataset.size = size;
      const spinner = el('div', 'spinner');
      spinner.hidden = true;
      video.addEventListener('waiting', () => { spinner.hidden = false; });
      video.addEventListener('playing', () => { spinner.hidden = true; slide.classList.remove('is-paused'); });
      video.addEventListener('pause', () => {
        // A play ending under the auto-scroll is played again, or moved on
        // from, at once: no play button for that moment.
        if (index === state.current && !(auto.running && video.ended)) slide.classList.add('is-paused');
      });
      video.addEventListener('timeupdate', () => {
        if (index !== state.current) return;
        showTime(video);
        auto.progressAt = performance.now();
      });
      video.addEventListener('loadedmetadata', () => { if (index === state.current) showTime(video); });
      video.addEventListener('ended', () => autoEnded(index, video));     // only while the auto-scroll runs: loop is off then
      const paused = el('div', 'paused');
      paused.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>';
      slide.append(video, spinner, paused);
    } else {
      // The thumbnail is almost certainly cached from the wall, so there is
      // something real on screen at once; the sharp copy fades in over it.
      const low = el('img');
      low.alt = '';
      low.draggable = false;
      low.src = photoUrl(item, 't');
      const sharp = el('img', 'sharp');
      sharp.alt = '';
      sharp.draggable = false;
      sharp.decoding = 'async';
      sharp.addEventListener('load', () => sharp.classList.add('in'), { once: true });
      const size = prefs.photoHigh || autoFull() ? 'l' : 'm';
      sharp.src = photoUrl(item, size);
      slide.dataset.size = size;
      slide.classList.add('photo');
      const layer = el('div', 'zoom');       // what pinching scales and dragging moves
      layer.append(low, sharp);
      slide.appendChild(layer);
    }
    return slide;
  }

  // A video slide's own video. A photo slide can hold one too, the clip of a
  // Live Photo, and nothing that plays, pauses or sizes "the video" means that.
  const mainVideo = (slide) => (slide ? slide.querySelector('video:not(.live)') : null);

  function dropSlide(index) {
    const slide = slides.get(index);
    if (!slide) return;
    // A dropped video has its source cleared first, or iOS keeps its decoder
    // (it allows few).
    for (const video of slide.querySelectorAll('video')) {
      video.pause(); video.removeAttribute('src'); video.load();
      letGoWhole(video);                // a whole video held in memory goes with it
    }
    slide.remove();
    slides.delete(index);
  }

  // Rebuild the deck around the current item and put the scroll position back
  // on it. Everything moves by the same amount in the same instant, so what is
  // on screen does not change; only how far the deck extends does.
  function renderWindow() {
    const first = Math.max(0, state.current - BEHIND);
    const last = Math.min(state.items.length - 1, state.current + AHEAD);
    for (const index of [...slides.keys()]) {
      if (index < first || index > last) dropSlide(index);
    }
    for (let index = first; index <= last; index++) {
      if (!slides.has(index)) {
        const slide = buildSlide(index);
        slides.set(index, slide);
        deck.appendChild(slide);
      }
    }
    deckBase = first;
    for (const [index, slide] of slides) {
      slide.style.top = (index - deckBase) * slideHeight + 'px';
      slide.style.height = slideHeight + 'px';
    }
    $('deck-size').style.height = (last - first + 1) * slideHeight + 'px';
    relayoutUntil = performance.now() + 60;     // the jump below is not a swipe
    deck.scrollTop = (state.current - deckBase) * slideHeight;
  }

  // Called when the viewer opens and whenever the screen changes size (the
  // phone is turned, or Safari's toolbar slides away).
  function layoutDeck() {
    clearTimeout(settle);
    slideHeight = deck.clientHeight;
    if (state.current >= 0) renderWindow();
    relayoutUntil = performance.now() + 300;
  }

  const warmed = new Set();
  function preloadAhead() {
    for (let step = 1; step <= prefs.ahead; step++) {
      const item = state.items[state.current + step];
      if (!item || warmed.has(item.id)) continue;
      warmed.add(item.id);
      // For a photo this is the whole picture. For a video it is the poster:
      // the swipe lands on a sharp still while the first seconds arrive.
      new Image().src = photoUrl(item, item.k === 'v' || !(prefs.photoHigh || autoFull()) ? 'm' : 'l');
    }
    // The very next video also starts buffering, so it plays the moment it
    // is reached. Only one, so a quick flick past it wastes little. Not while
    // the auto-scroll waits for full quality: it fetches that one whole.
    const video = mainVideo(slides.get(state.current + 1));
    if (video && prefs.ahead > 0 && !autoFull() && video.preload !== 'auto') {
      video.preload = 'auto';
      video.load();
    }
  }

  function currentVideo() {
    return mainVideo(slides.get(state.current));
  }

  function setCurrent(index) {
    if (index === state.current || index < 0 || index >= state.items.length) return;
    resetZoom(false);
    state.current = index;
    renderWindow();
    for (const [i, slide] of slides) {
      stopLive(slide);
      const video = mainVideo(slide);
      if (!video) continue;
      if (i === index) playCurrent(video, slide);
      else { video.pause(); slide.classList.remove('is-paused'); }
    }
    const item = state.items[index];
    $('viewer-section').textContent = sectionName(item.s);
    $('viewer-live').hidden = !item.lv;
    $('viewer-sound').hidden = item.k !== 'v' && !item.lv;
    updateSoundIcon();
    $('scrub').hidden = item.k !== 'v';
    $('viewer-time').hidden = item.k !== 'v';
    $('viewer-time').textContent = item.k === 'v' ? `0:00 / ${fmtClock(item.d)}` : '';
    $('scrub-fill').style.width = '0';
    preloadAhead();
    if (index > state.items.length - 8) loadMore();
    if (auto.running) autoBegin(index);     // whoever moved, the auto-scroll or a swipe: the time starts again
  }

  // How far into the video, and how long it is, in a few characters.
  function showTime(video) {
    const item = state.items[state.current];
    const total = video.duration && isFinite(video.duration) ? video.duration : (item ? item.d : 0);
    $('viewer-time').textContent = `${fmtClock(video.currentTime)} / ${fmtClock(total)}`;
    if (total) $('scrub-fill').style.width = (video.currentTime / total * 100) + '%';
  }

  function playCurrent(video, slide) {
    slide.classList.remove('is-paused');
    video.muted = !state.wantSound;
    const attempt = video.play();
    if (attempt && attempt.catch) {
      attempt.catch(() => {
        // The browser refused to start with sound without a fresh tap. Start
        // silent rather than not at all; the sound button still works.
        video.muted = true;
        updateSoundIcon();
        video.play().catch(() => {
          // Still refused, usually because nothing has loaded yet. Try once
          // more when it can play; if that fails too, show the play button.
          slide.classList.add('is-paused');
          video.addEventListener('canplay', () => {
            if (Number(slide.dataset.index) === state.current && video.paused) {
              video.play().catch(() => {});
            }
          }, { once: true });
        });
      });
    }
    updateSoundIcon();
  }

  // Change the size of the video on screen, keeping its place.
  function setVideoSize(choice) {
    const slide = slides.get(state.current);
    const video = currentVideo();
    const item = state.items[state.current];
    if (!slide || !video || !item) return;
    const at = video.currentTime;
    slide.dataset.size = choice;
    letGoWhole(video);
    video.src = videoSource(item, choice);
    video.addEventListener('loadedmetadata', () => {
      try { video.currentTime = at; } catch { /* starts from the beginning instead */ }
    }, { once: true });
    playCurrent(video, slide);
  }

  const SOUND_ON = 'M4 9v6h4l5 4V5L8 9H4zM16 9a4 4 0 0 1 0 6M18.5 6.5a8 8 0 0 1 0 11';
  const SOUND_OFF = 'M4 9v6h4l5 4V5L8 9H4zM16.5 9.5l5 5M21.5 9.5l-5 5';
  function updateSoundIcon() {
    const video = currentVideo();
    const item = state.items[state.current];
    // A Live Photo has nothing playing most of the time: its button shows
    // what its clip will do when it is played.
    const audible = video ? !video.muted : !!(item && item.lv && state.wantSound);
    $('sound-icon').setAttribute('d', audible ? SOUND_ON : SOUND_OFF);
    $('viewer-sound').classList.toggle('on', audible);
  }

  // ----------------------------------------------------------- live photos --
  // The clip is one small file, fetched the first time it is asked for and
  // kept while the slide lives. It is laid exactly over the photo, inside the
  // layer that zooming moves, and fades in once it is really playing, so a
  // slow link shows the still for a moment longer rather than a black frame.
  let liveHold = 0;                   // timer: a finger resting on a Live Photo
  let liveHeld = false;               // the clip is playing because a finger is down

  function liveClip(slide, item) {
    let clip = slide.querySelector('video.live');
    if (!clip) {
      clip = el('video', 'live');
      clip.playsInline = true;
      clip.setAttribute('playsinline', '');
      clip.disableRemotePlayback = true;
      clip.preload = 'auto';
      clip.addEventListener('playing', () => { if (slide.classList.contains('live-on')) clip.classList.add('in'); });
      clip.addEventListener('ended', () => stopLive(slide));
      clip.addEventListener('error', () => stopLive(slide));
      clip.src = murl(item, 'live.mp4');
      slide.querySelector('.zoom').appendChild(clip);
    }
    return clip;
  }
  function playLive(slide) {
    const item = state.items[Number(slide.dataset.index)];
    if (!item || !item.lv || keyer.slide) return;
    const clip = liveClip(slide, item);
    slide.classList.add('live-on');
    $('viewer-live').classList.add('on');
    clip.muted = !state.wantSound;
    try { clip.currentTime = 0; } catch { /* not loaded yet: it starts at the start anyway */ }
    const attempt = clip.play();
    if (attempt && attempt.catch) {
      attempt.catch(() => {
        clip.muted = true;              // refused with sound: play it silent rather than not at all
        clip.play().catch(() => stopLive(slide));
      });
    }
  }
  function stopLive(slide) {
    if (!slide || !slide.classList.contains('live-on')) return;
    slide.classList.remove('live-on');
    $('viewer-live').classList.remove('on');
    const clip = slide.querySelector('video.live');
    if (clip) { clip.classList.remove('in'); clip.pause(); }
  }
  $('viewer-live').addEventListener('click', () => {
    const slide = slides.get(state.current);
    if (!slide) return;
    if (slide.classList.contains('live-on')) stopLive(slide); else playLive(slide);
  });

  // ----------------------------------------------------------- key photo --
  // A Live Photo can be shown, on the wall and here, by any moment of its clip
  // instead of its still, as Photos on an iPhone allows (Edit, Live, Make Key
  // Photo). The clip lies paused over the photo with a slider under it, one
  // step for each frame, and nothing changes until Make Key Photo is pressed.
  // Original puts the still back exactly as it was. Only the admin can do it,
  // as with tags.
  const keyer = { slide: null, item: null, frames: [], box: null, range: null, say: null, from: null, size: null };

  function keyerSeek(index) {
    const clip = keyer.slide && keyer.slide.querySelector('video.live');
    if (!clip || !keyer.frames.length) return;
    const i = clamp(index, 0, keyer.frames.length - 1);
    keyer.range.value = String(i);
    // A little after the frame's own start, so that the frame shown is that
    // one and not the one before it.
    const at = keyer.frames[i];
    const next = i + 1 < keyer.frames.length ? keyer.frames[i + 1] : at + 0.04;
    const go = () => { try { clip.currentTime = at + Math.min(0.005, (next - at) / 4); } catch { /* not ready */ } };
    if (clip.readyState >= 1) go(); else clip.addEventListener('loadedmetadata', go, { once: true });
    keyer.say.textContent = `Moment ${i + 1} of ${keyer.frames.length} · `
      + (keyer.from === 'clip' ? "from the camera's clip" : 'from the small copy of the clip')
      + (keyer.size ? `, ${keyer.size[0]} × ${keyer.size[1]}` : '');
  }

  async function openKeyer(index) {
    const slide = slides.get(index);
    const item = state.items[index];
    if (!slide || !item || !item.lv || !isAdmin() || keyer.slide || index !== state.current) return;
    resetZoom(false);
    stopLive(slide);
    Object.assign(keyer, { slide, item, frames: [] });
    $('viewer').classList.add('keying');
    deck.classList.add('held');             // no swiping on to another item meanwhile
    const box = keyer.box = el('div', 'keyer');
    const strip = el('div', 'keyer-strip');
    const back = el('button', '', '‹');
    back.setAttribute('aria-label', 'One frame back');
    const range = keyer.range = el('input');
    range.type = 'range';
    range.min = '0';
    range.step = '1';
    range.setAttribute('aria-label', 'Moment');
    const on = el('button', '', '›');
    on.setAttribute('aria-label', 'One frame on');
    strip.append(back, range, on);
    keyer.say = el('p', 'muted small', 'Loading the clip…');
    const row = el('div', 'keyer-row');
    const cancel = el('button', '', 'Cancel');
    const original = el('button', '', 'Original');
    original.hidden = !item.kf;
    const make = el('button', 'make', 'Make Key Photo');
    row.append(cancel, original, make);
    box.append(strip, keyer.say, row);
    $('viewer').appendChild(box);
    const buttons = [back, range, on, original, make];
    for (const b of buttons) b.disabled = true;
    cancel.addEventListener('click', closeKeyer);
    back.addEventListener('click', () => keyerSeek(Number(range.value) - 1));
    on.addEventListener('click', () => keyerSeek(Number(range.value) + 1));
    range.addEventListener('input', () => keyerSeek(Number(range.value)));

    const choose = async (frame) => {
      for (const b of buttons) b.disabled = true;
      keyer.say.textContent = frame === null ? 'Putting the photo back…' : 'Making the key photo…';
      try {
        const now = await api('/api/admin/key-photo', { id: item.id, frame });
        closeKeyer();
        showKeyPhoto(item, now);
      } catch (error) {
        if (keyer.box !== box) return;
        keyer.say.textContent = error.message;
        for (const b of buttons) b.disabled = false;
      }
    };
    make.addEventListener('click', () => choose(Number(range.value)));
    original.addEventListener('click', () => choose(null));

    // The clip is asked to play now, silently, while the tap that opened this
    // still counts as one: a phone may fetch nothing of a clip until it has
    // been asked to play it, and in Low Power Mode plays nothing a tap did not
    // ask for. It is stopped on a moment as soon as the moments are known.
    const clip = liveClip(slide, item);
    clip.muted = true;
    const started = clip.play();
    if (started && started.catch) started.catch(() => {});
    let found;
    try { found = await api('/api/admin/key-photo/' + item.id); } catch (error) {
      if (keyer.box === box) keyer.say.textContent = error.message;
      return;
    }
    if (keyer.box !== box) return;          // closed while the moments were on their way
    Object.assign(keyer, { frames: found.frames, from: found.from, size: found.size });
    range.max = String(found.frames.length - 1);
    clip.pause();
    clip.classList.add('in');
    keyerSeek(found.chosen === null ? Math.floor((found.frames.length - 1) / 2) : found.chosen);
    for (const b of buttons) b.disabled = false;
  }

  function closeKeyer() {
    if (!keyer.slide) return;
    const clip = keyer.slide.querySelector('video.live');
    if (clip) { clip.pause(); clip.classList.remove('in'); }
    if (keyer.box) keyer.box.remove();
    $('viewer').classList.remove('keying');
    deck.classList.remove('held');
    Object.assign(keyer, { slide: null, item: null, frames: [], box: null, range: null, say: null,
                           from: null, size: null });
  }

  // The new picture, everywhere this page shows the item: its tile on the
  // wall and its slide in the viewer, which is made again around it.
  function showKeyPhoto(item, now) {
    if (now.kf) item.kf = now.kf; else delete item.kf;
    Object.assign(item, { w: now.w, h: now.h, ph: now.ph });
    const index = state.items.indexOf(item);
    const tile = $('grid').querySelector(`.tile[data-index="${index}"]`);
    if (tile) {
      tile.style.aspectRatio = `1 / ${Math.min(2.2, Math.max(0.45, item.h / item.w))}`;
      if (item.ph) tile.style.backgroundImage = `url(data:image/webp;base64,${item.ph})`;
      const img = tile.querySelector('img');
      img.classList.remove('in');
      img.addEventListener('load', () => img.classList.add('in'), { once: true });
      img.src = photoUrl(item, 't');
    }
    if (state.viewing && slides.has(index)) {
      dropSlide(index);
      renderWindow();
    }
  }

  function openViewer(index) {
    stopAllPreviews();
    state.viewing = true;
    state.current = -1;
    warmed.clear();
    $('viewer').hidden = false;
    document.body.classList.add('locked');
    slideHeight = deck.clientHeight;
    setCurrent(index);
    relayoutUntil = performance.now() + 300;
    pushHistory();
  }

  function closeViewer(silent) {
    if (!state.viewing) return;
    stopAuto();
    closeKeyer();
    resetZoom(false);
    state.viewing = false;
    clearTimeout(settle);
    for (const index of [...slides.keys()]) dropSlide(index);
    $('viewer').hidden = true;
    document.body.classList.remove('locked');
    const index = state.current;
    state.current = -1;
    schedulePreviews();
    if (!silent) {
      // Put the wall where the viewer ended up, so closing never loses your place.
      const tile = $('grid').querySelector(`.tile[data-index="${index}"]`);
      if (tile) tile.scrollIntoView({ block: 'center' });
    }
  }

  // The viewer and the sheets each add a history entry, so the phone's back
  // gesture closes them. Jumping straight to another section has to remove
  // however many of those are stacked up, without reacting to its own unwinding.
  //
  // The page puts the wall where it belongs itself (closing the viewer, or
  // bringing a wall back to its place), so the browser is asked not to move it
  // as it goes back through these steps: it would put back where the wall was
  // when each one was taken, after the page had already moved it.
  if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
  let pushed = 0;
  let unwinding = false;
  function pushHistory() { pushed += 1; history.pushState({ waterfall: pushed }, ''); }
  function unwindHistory() {
    if (!pushed) return;
    unwinding = true;
    history.go(-pushed);
    pushed = 0;
  }
  window.addEventListener('popstate', () => {
    if (unwinding) { unwinding = false; return; }
    pushed = Math.max(0, pushed - 1);
    if (!$('sheet').hidden) return closeSheet();
    closeViewer();
  });
  $('viewer-close').addEventListener('click', () => history.back());

  // ---------------------------------------------------------------- zoom --
  // Pinch to zoom a photo, drag to move around it, double-tap to jump in and
  // out. While a photo is zoomed the deck is held still, so a drag moves the
  // picture instead of changing item; pinching back out releases it.
  //
  // The position is three numbers: scale s, and where the layer's top-left
  // corner sits (x, y). Zooming about a point p keeps whatever is under p
  // fixed:  x' = p - (p - x) * (s' / s).
  const ZOOM_MAX = 6;
  const zoom = { slide: null, layer: null, s: 1, x: 0, y: 0,
                 touches: new Map(), pinch: null, drag: null, moved: false,
                 tapAt: 0, tapX: 0, tapY: 0 };

  function paintZoom(animate) {
    if (!zoom.layer) return;
    const width = deck.clientWidth;
    zoom.s = clamp(zoom.s, 1, ZOOM_MAX);
    zoom.x = clamp(zoom.x, width - width * zoom.s, 0);          // never drag the picture
    zoom.y = clamp(zoom.y, slideHeight - slideHeight * zoom.s, 0); // off the screen
    zoom.layer.style.transition = animate ? 'transform 0.18s ease-out' : 'none';
    zoom.layer.style.transform = zoom.s === 1 ? '' : `translate(${zoom.x}px, ${zoom.y}px) scale(${zoom.s})`;
  }
  function holdDeck(held) {
    if (held && zoom.slide) stopDrift(zoom.slide, true);     // the fingers take over from the slow pan
    deck.classList.toggle('held', held);
    $('viewer').classList.toggle('zooming', held);
    if (zoom.slide) zoom.slide.classList.toggle('zoomed', held);
  }
  function resetZoom(animate) {
    if (!zoom.slide) return;
    zoom.s = 1; zoom.x = 0; zoom.y = 0;
    paintZoom(animate);
    holdDeck(false);
    zoom.touches.clear();
    zoom.pinch = zoom.drag = null;
    clearTimeout(liveHold);
    if (liveHeld) { liveHeld = false; stopLive(zoom.slide); }
    zoom.slide = zoom.layer = null;
  }
  function sharpenForZoom() {
    // Zoomed in, the screen-sized copy runs out of detail: fetch the large one.
    const item = state.items[state.current];
    if (!zoom.slide || !item || zoom.slide.dataset.size === 'l' || zoom.s < 1.5) return;
    zoom.slide.dataset.size = 'l';
    zoom.slide.querySelector('img.sharp').src = photoUrl(item, 'l');
  }
  function pointIn(event) {
    const box = deck.getBoundingClientRect();
    return { x: event.clientX - box.left, y: event.clientY - box.top };
  }
  function startPinch() {
    clearTimeout(liveHold);
    const [a, b] = [...zoom.touches.values()];
    zoom.pinch = { gap: Math.hypot(a.x - b.x, a.y - b.y) || 1, s: zoom.s, x: zoom.x, y: zoom.y,
                   cx: (a.x + b.x) / 2, cy: (a.y + b.y) / 2 };
    zoom.drag = null;
    holdDeck(true);                    // two fingers down: this is not a swipe
  }

  deck.addEventListener('pointerdown', (event) => {
    if (keyer.slide) return;           // choosing a key photo: the picture stays as it is
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    const slide = event.target.closest('.slide.photo');
    if (!slide || Number(slide.dataset.index) !== state.current) return;
    if (zoom.slide !== slide) {
      resetZoom(false);
      zoom.slide = slide;
      zoom.layer = slide.querySelector('.zoom');
    }
    zoom.touches.set(event.pointerId, pointIn(event));
    zoom.moved = false;
    // A finger resting on a Live Photo plays its clip for as long as it rests.
    clearTimeout(liveHold);
    if (zoom.touches.size === 1 && zoom.s === 1 && slide.querySelector('.zoom') && state.items[state.current].lv) {
      const at = pointIn(event);
      zoom.rest = at;
      liveHold = setTimeout(() => {
        if (zoom.touches.size !== 1 || zoom.s !== 1 || zoom.slide !== slide) return;
        liveHeld = true;
        zoom.moved = true;               // lifting the finger is then not a tap
        playLive(slide);
      }, 320);
    }
    if (zoom.touches.size === 2) startPinch();
    else if (zoom.touches.size === 1 && zoom.s > 1) {
      const p = pointIn(event);
      zoom.drag = { px: p.x, py: p.y, x: zoom.x, y: zoom.y };
    }
  });

  deck.addEventListener('pointermove', (event) => {
    if (!zoom.touches.has(event.pointerId)) return;
    const p = pointIn(event);
    zoom.touches.set(event.pointerId, p);
    if (zoom.rest && Math.hypot(p.x - zoom.rest.x, p.y - zoom.rest.y) > 8) clearTimeout(liveHold);
    if (zoom.pinch && zoom.touches.size >= 2) {
      const [a, b] = [...zoom.touches.values()];
      const start = zoom.pinch;
      const cx = (a.x + b.x) / 2, cy = (a.y + b.y) / 2;
      zoom.s = clamp(start.s * Math.hypot(a.x - b.x, a.y - b.y) / start.gap, 1, ZOOM_MAX);
      zoom.x = cx - (start.cx - start.x) * (zoom.s / start.s);
      zoom.y = cy - (start.cy - start.y) * (zoom.s / start.s);
      zoom.moved = true;
      paintZoom(false);
    } else if (zoom.drag && zoom.s > 1) {
      if (Math.hypot(p.x - zoom.drag.px, p.y - zoom.drag.py) > 6) zoom.moved = true;
      zoom.x = zoom.drag.x + p.x - zoom.drag.px;
      zoom.y = zoom.drag.y + p.y - zoom.drag.py;
      paintZoom(false);
    }
  });

  function liftPointer(event) {
    if (!zoom.touches.has(event.pointerId)) return;
    const p = zoom.touches.get(event.pointerId);
    zoom.touches.delete(event.pointerId);
    clearTimeout(liveHold);
    if (liveHeld) { liveHeld = false; stopLive(zoom.slide); }
    if (zoom.pinch && zoom.touches.size < 2) {
      zoom.pinch = null;
      if (zoom.s < 1.05) return resetZoom(true);      // pinched back out: let go
      sharpenForZoom();
      const [rest] = [...zoom.touches.values()];
      if (rest) zoom.drag = { px: rest.x, py: rest.y, x: zoom.x, y: zoom.y };
      return;
    }
    if (zoom.touches.size) return;
    zoom.drag = null;
    if (event.type === 'pointercancel' || zoom.moved) { zoom.tapAt = 0; return; }
    // A second quick tap in the same place: jump in to that point, or back out.
    const now = performance.now();
    if (now - zoom.tapAt < 320 && Math.hypot(p.x - zoom.tapX, p.y - zoom.tapY) < 32) {
      zoom.tapAt = 0;
      if (zoom.s > 1) return resetZoom(true);
      zoom.s = 2.5;
      zoom.x = p.x - p.x * zoom.s;
      zoom.y = p.y - p.y * zoom.s;
      holdDeck(true);
      paintZoom(true);
      sharpenForZoom();
    } else {
      zoom.tapAt = now; zoom.tapX = p.x; zoom.tapY = p.y;
    }
  }
  deck.addEventListener('pointerup', liftPointer);
  deck.addEventListener('pointercancel', liftPointer);
  // Safari's own page zoom must not answer the same pinch.
  $('viewer').addEventListener('gesturestart', (event) => event.preventDefault());

  // --------------------------------------------------------- deck scroll --
  // One swipe, one item. The moment a swipe lands on the next slide the deck
  // is re-centred on it, so the following swipe has somewhere to go.
  deck.addEventListener('scroll', () => {
    if (!state.viewing || !slideHeight || performance.now() < relayoutUntil) return;
    const position = deck.scrollTop / slideHeight;
    const nearest = Math.round(position);
    clearTimeout(settle);
    if (Math.abs(position - nearest) < 0.01) {
      if (deckBase + nearest !== state.current) setCurrent(deckBase + nearest);
      return;
    }
    settle = setTimeout(() => {
      const index = deckBase + Math.round(deck.scrollTop / slideHeight);
      if (index !== state.current) setCurrent(index);
    }, 140);
  }, { passive: true });

  deck.addEventListener('click', (event) => {
    const video = mainVideo(event.target.closest('.slide'));
    const slide = video && video.closest('.slide');
    if (!video) return;
    // A video paused by a tap holds the auto-scroll until it is tapped again.
    auto.userPaused = !video.paused;
    if (video.paused) playCurrent(video, slide);
    else video.pause();
  });

  $('viewer-sound').addEventListener('click', () => {
    state.wantSound = !state.wantSound;
    const video = currentVideo();
    if (video) {
      video.muted = !state.wantSound;
      if (video.paused) video.play().catch(() => {});
    }
    const slide = slides.get(state.current);
    const clip = slide && slide.querySelector('video.live');
    if (clip) clip.muted = !state.wantSound;
    updateSoundIcon();
  });

  $('viewer-section').addEventListener('click', () => {
    const item = state.items[state.current];
    if (item) setSection(item.s);
  });

  // Drag along the bottom edge to move through a video.
  const scrub = $('scrub');
  function scrubTo(event) {
    const video = currentVideo();
    if (!video || !video.duration) return;
    const box = scrub.getBoundingClientRect();
    const fraction = clamp((event.clientX - box.left) / box.width, 0, 1);
    video.currentTime = fraction * video.duration;
    showTime(video);
  }
  scrub.addEventListener('pointerdown', (event) => { scrub.setPointerCapture(event.pointerId); scrubTo(event); });
  scrub.addEventListener('pointermove', (event) => { if (event.buttons || event.pointerType === 'touch') scrubTo(event); });

  function step(by) {
    const target = state.current + by;
    if (target < 0 || target >= state.items.length) return;
    deck.scrollTo({ top: (target - deckBase) * slideHeight, behavior: 'smooth' });
  }
  window.addEventListener('keydown', (event) => {
    if (!state.viewing || !$('sheet').hidden) {
      if (event.key === 'Escape' && !$('sheet').hidden) history.back();
      return;
    }
    if (keyer.slide) {
      // Choosing a key photo: the arrows move a frame, Escape leaves it as it was.
      const by = { ArrowLeft: -1, ArrowRight: 1 }[event.key];
      if (event.key === 'Escape') closeKeyer();
      else if (by && event.target !== keyer.range && keyer.range && !keyer.range.disabled) {
        event.preventDefault();
        keyerSeek(Number(keyer.range.value) + by);
      }
      return;
    }
    const by = { ArrowDown: 1, ArrowRight: 1, ' ': 1, PageDown: 1, ArrowUp: -1, ArrowLeft: -1, PageUp: -1 }[event.key];
    if (by) { event.preventDefault(); step(by); }
    else if (event.key === 'Escape') history.back();
    else if (event.key === 'i') $('viewer-info').click();
    else if (event.key === 'm') $('viewer-sound').click();
  });

  // ----------------------------------------------------------- auto-scroll --
  // The viewer moving on by itself, so many whole minutes on each item. Only
  // the admin can start it (asked for outright), from the admin menu; anyone
  // can stop it, with its pill or by leaving the viewer.
  //
  // A video shorter than the time plays again until the time is up, and the
  // play under way when it runs out is finished: five minutes and a two-minute
  // video is three plays. A video longer than the time plays once, whole.
  //
  // "Wait for full quality" shows every photo at its large copy and every
  // video at the best size that arrives in time (the top one when the link
  // allows: see "fetched whole", below), and stays on the item before until
  // the next has arrived. Never for longer than one more turn of the time: a
  // phone that never says a video is ready must not stop the whole thing.
  // Unticked, it moves on when the time is up, at whatever has arrived.
  //
  // Anything a person does holds it: a sheet open, a photo zoomed, a video
  // paused by a tap, choosing a key photo, or the page out of sight. A swipe
  // moves on and starts the time again. Its own moves are not a touch, so the
  // admin's half hour runs out as it would on a phone left alone: then it
  // carries on on the Visible wall and stops on the Hidden one, which goes.
  let autoTimer = 0;
  const drifts = new WeakMap();        // slide -> its slow pan and zoom
  // Its settings are kept apart from everyone's and written down only when
  // the admin changes one, so nothing of it is stored on anybody else's device.
  const AUTO_STORE = 'waterfall.auto';
  const autoPrefs = Object.assign({ minutes: 1, wait: true, pan: false }, (() => {
    try { return JSON.parse(localStorage.getItem(AUTO_STORE) || '{}'); } catch { return {}; }
  })());
  const saveAuto = () => {
    try { localStorage.setItem(AUTO_STORE, JSON.stringify(autoPrefs)); } catch { /* private tab */ }
  };
  const autoFull = () => auto.running && autoPrefs.wait;
  const autoMinutes = () => { const m = Number(autoPrefs.minutes); return m > 0 ? m : 1; };
  const autoMs = () => autoMinutes() * 60000;
  const topRung = (item) => String(Math.max(...(item.r || ['360']).map(Number)));
  const autoHeld = () => document.hidden || !sheet.hidden || !!keyer.slide
    || (zoom.slide !== null && zoom.s > 1) || auto.userPaused;

  function startAuto(index) {
    if (!isAdmin() || index < 0 || index >= state.items.length) return;
    if (state.viewing) {
      // Started from the item on screen: the viewer stays open, with its one
      // step in the history, and the deck is made afresh around that item at
      // the quality asked for.
      stopAuto();
      auto.running = true;
      keepAwake(true);
      for (const i of [...slides.keys()]) dropSlide(i);
      state.current = -1;
      setCurrent(index);
    } else {
      auto.running = true;
      keepAwake(true);
      openViewer(index);                // the deck is made afresh, at the quality asked for
    }
    clearInterval(autoTimer);
    autoTimer = setInterval(autoTick, 250);
    drawAuto();
  }

  function stopAuto() {
    if (!auto.running) return;
    auto.running = false;
    clearInterval(autoTimer);
    keepAwake(false);
    stopWhole();
    for (const slide of slides.values()) {
      stopDrift(slide, false);          // a photo stays as it was at that moment
      const video = mainVideo(slide);
      if (video) video.loop = true;
    }
    drawAuto();
  }

  // A new item on screen, by the auto-scroll or by a swipe.
  function autoBegin(index) {
    const now = performance.now();
    Object.assign(auto, { last: now, shown: 0, plays: 0, done: false, doneAt: 0, moving: false,
                          userPaused: false, progressAt: now, warmed: null });
    for (const [i, slide] of slides) if (i !== index) stopDrift(slide, true);
    const slide = slides.get(index);
    const video = mainVideo(slide);
    if (video) video.loop = false;      // it is played again here, so that the plays can be counted
    else if (slide && autoPrefs.pan) startDrift(slide);
    // At most two whole videos held in memory: the one on screen and the next.
    // And while waiting for full quality, no other video of the deck fetches
    // anything: on a thin link every one of them takes a share.
    const target = wholeTarget();
    for (const [i, other] of slides) {
      const held = mainVideo(other);
      if (i === index || i === target || !held) continue;
      if (!held.dataset.whole && !(autoFull() && held.preload !== 'none')) continue;
      letGoWhole(held);
      held.preload = autoFull() ? 'none' : 'metadata';     // nothing more of it fetched unless it is swiped back to
      held.src = videoSource(state.items[i], other.dataset.size);
    }
    fetchWhole();
    drawAuto();
  }

  // How many times the video on screen plays in the time.
  function neededPlays(video) {
    const item = state.items[state.current];
    const length = video && isFinite(video.duration) && video.duration > 0 ? video.duration : (item && item.d) || 0;
    return playsFor(length);
  }
  function playsFor(length) {
    // A twentieth of a play's slack: a "one-minute" video of 60.03 seconds
    // still plays five times in five minutes, not six.
    return length ? Math.max(1, Math.ceil(autoMs() / 1000 / length - 0.05)) : 1;
  }

  function autoEnded(index, video) {
    if (!auto.running || index !== state.current) return;
    auto.plays += 1;
    auto.progressAt = performance.now();
    if (!auto.done && auto.plays >= neededPlays(video)) { auto.done = true; auto.doneAt = auto.shown; }
    if (auto.done && !autoHeld() && autoMayMove()) return autoNext();
    // Due to play again, or waiting for the next item: from the start.
    try { video.currentTime = 0; } catch { /* not seekable yet */ }
    video.play().catch(() => {});
  }

  function autoTick() {
    const now = performance.now();
    const elapsed = Math.min(now - auto.last, 1000);  // a phone's timers sleep with it
    auto.last = now;
    if (!auto.running) return;
    const held = autoHeld();
    const drift = drifts.get(slides.get(state.current));
    if (drift && held && drift.playState === 'running') drift.pause();
    else if (drift && !held && drift.playState === 'paused') drift.play();
    if (held) return drawAuto();
    auto.shown += elapsed;
    if (!auto.done) {
      const video = currentVideo();
      // A photo: the time. A video: its plays (autoEnded), or, once the time is
      // up, half a minute without moving (refused, or stuck on the way).
      if (!video ? auto.shown >= autoMs() : auto.shown >= autoMs() && now - auto.progressAt > 30000) {
        auto.done = true;
        auto.doneAt = auto.shown;
      }
    }
    if (auto.done && autoMayMove()) autoNext();
    drawAuto();
  }

  // The item after this one; at the end, the first again. -1: still loading.
  function autoNextIndex() {
    if (state.current + 1 < state.items.length) return state.current + 1;
    if (state.more) { loadMore(); return -1; }
    return 0;
  }

  function autoMayMove() {
    if (auto.moving || autoNextIndex() < 0) return false;
    if (!autoPrefs.wait || auto.shown - auto.doneAt >= autoMs()) {
      // Moving on without all of it: what is still on its way plays as it arrives.
      if (whole.state === 'loading' && whole.item === state.items[autoNextIndex()]) stopWhole();
      return true;
    }
    const slide = slides.get(autoNextIndex());
    if (!slide) return true;            // round again to the start: not in the deck yet
    const video = mainVideo(slide);
    if (!video) {
      const sharp = slide.querySelector('img.sharp');
      return !sharp || sharp.complete;  // arrived, or failed: either way nothing more is coming
    }
    if (video.dataset.whole) return true;                    // all of it is here, in memory
    if (whole.state === 'loading' && whole.item === state.items[Number(slide.dataset.index)]) return false;
    if (video.error || video.readyState >= 4) return true;
    // A browser fetches only so far ahead of a video that is not playing
    // (Chrome: twelve seconds, then it stops) and never calls it ready to play
    // through while it waits. So a video is ready once its first ten seconds,
    // or all of it if it is shorter, are here at the size asked for.
    const length = isFinite(video.duration) && video.duration > 0 ? video.duration : 10;
    const have = video.buffered.length && video.buffered.start(0) <= 0.5 ? video.buffered.end(0) : 0;
    if (video.readyState >= 3 && have >= Math.min(10, length) - 0.25) return true;
    if (auto.warmed !== video) {
      // A phone may fetch nothing of a video until it is asked to play it: a
      // moment's silent play, out of sight, starts it coming.
      auto.warmed = video;
      video.preload = 'auto';
      video.muted = true;
      const attempt = video.play();
      if (attempt && attempt.then) {
        attempt.then(() => {
          if (Number(slide.dataset.index) === state.current) return;
          video.pause();
          try { video.currentTime = 0; } catch { /* starts where it stopped */ }
        }).catch(() => {});
      }
    }
    return false;
  }

  // "Wait for full quality", for a video, goes further than any browser
  // will by itself: the next video is fetched whole into memory before it is
  // reached, and played from there, so none of it is left to arrive. Left to
  // itself a browser fetches only so far ahead of a video that is not playing
  // (Chrome twelve seconds; Safari on a Mac all of it; an iPhone, not known).
  //
  // On a thin link three things decide whether it is here in time, each
  // seen on a link imitated at 0.3 MB/s:
  //   * When it starts. The next video, wherever it is past the photos in
  //     between, is fetched from the moment the item after the last video
  //     comes on screen, so every minute of those photos is time to fetch it.
  //   * Its size. The top size of a one-minute video is 30 to 80 MB, minutes
  //     of a thin link, so the size is the largest that arrives before the
  //     auto-scroll must move on (the time of each item until then, and the
  //     one more turn it waits), at the speed the downloads themselves find.
  //     The first one starts at the top size and is looked at after eight
  //     seconds; every eight seconds after, a download that will be late
  //     starts again smaller, if that arrives sooner.
  //   * Nothing else fetching. No other video of the deck fetches anything
  //     meanwhile (one ahead, given its source, fetched 12.7 MB of itself).
  // Up to 150 MB. At most two are held, the one on screen and the next; any
  // other goes back to being fetched as it plays. One not here when the
  // auto-scroll reaches it plays at the size the link carries.
  const WHOLE_MAX = 150e6;
  const whole = { item: null, ctrl: null, state: '', rung: '', blob: null };
  let wholeRate = 0;                    // bytes a second, as the downloads found it

  // The size a video is shown at while waiting for full quality.
  const autoSize = (item) => (whole.item === item && whole.rung) || topRung(item);
  const rungBytes = (item, name) => (item.rb || [])[(item.r || []).indexOf(name)] || 0;

  // The next video after the item on screen, past any photos. -1: none loaded.
  function wholeTarget() {
    for (let i = state.current + 1; i < state.items.length; i++) if (state.items[i].k === 'v') return i;
    return -1;
  }

  // Seconds until the auto-scroll moves on to item `target` at the latest:
  // the time of the item on screen and of each one in between (a video, its
  // plays), less what has gone, and the one more turn it waits on the last.
  function autoSecondsTo(target) {
    const turn = autoMs() / 1000;
    const span = (item) => (item && item.k === 'v' && item.d > 0 ? playsFor(item.d) * item.d : turn);
    let seconds = turn - auto.shown / 1000;
    for (let i = state.current; i < target; i++) seconds += span(state.items[i]);
    return Math.max(0, seconds);
  }

  // The largest size that arrives in `seconds` at `rate` bytes a second, a
  // fifth spare; the smallest if none does. Not knowing, the top one.
  function wholeRung(item, rate, seconds) {
    const rungs = (item.r || []).map((name) => ({ name, bytes: rungBytes(item, name) }))
      .sort((a, b) => Number(a.name) - Number(b.name));
    if (!rate || !rungs.length || rungs.some((r) => !r.bytes)) return topRung(item);
    const fits = rungs.filter((r) => r.bytes <= WHOLE_MAX && r.bytes <= rate * seconds * 0.8);
    return (fits.pop() || rungs[0]).name;
  }

  function letGoWhole(video) {
    if (!video || !video.dataset.whole) return;
    URL.revokeObjectURL(video.dataset.whole);
    delete video.dataset.whole;
  }

  // What a video of the deck has of the download: nothing while it is on its
  // way (its own loading would race it for the same bytes), the copy in
  // memory once it is here. False: it is not the video being fetched whole.
  function wholeInto(video, item) {
    if (!autoFull() || whole.item !== item || whole.state === 'skip') return false;
    if (whole.state !== 'done') {
      video.preload = 'none';
      if (video.getAttribute('src')) { video.removeAttribute('src'); video.load(); }
      return true;
    }
    if (!video.dataset.whole) {
      video.dataset.whole = URL.createObjectURL(whole.blob);
      video.addEventListener('error', () => {
        // A phone that will not play it from memory: the ordinary way.
        if (!video.dataset.whole) return;
        letGoWhole(video);
        video.removeAttribute('src');
        if (whole.item === item) Object.assign(whole, { state: 'skip', blob: null });
        plainSource(item, autoSize(item));
      }, { once: true });
    }
    video.src = video.dataset.whole;
    video.preload = 'auto';
    return true;
  }

  // The video's ordinary source again, at `size`; and playing, if it is the
  // one on screen.
  function plainSource(item, size) {
    const index = state.items.indexOf(item);
    const slide = slides.get(index);
    const video = mainVideo(slide);
    if (!video || video.getAttribute('src')) return;
    slide.dataset.size = size;
    video.src = videoSource(item, size);
    video.preload = 'auto';
    if (index === state.current) playCurrent(video, slide);
  }

  // The download's news, to its video if it is in the deck.
  function wholeShow(item) {
    const slide = slides.get(state.items.indexOf(item));
    const video = mainVideo(slide);
    if (!video) return;
    slide.dataset.size = autoSize(item);
    if (!wholeInto(video, item)) plainSource(item, slide.dataset.size);
  }

  function stopWhole() {
    const { item, ctrl, state: was } = whole;
    Object.assign(whole, { item: null, ctrl: null, state: '', rung: '', blob: null });
    if (ctrl) ctrl.abort();
    // Not here when it was reached, or no longer wanted: it plays as it
    // arrives, at the size the link carries.
    if (item && was === 'loading') plainSource(item, 'auto');
  }

  function fetchWhole() {
    const index = wholeTarget();
    const item = state.items[index];
    if (item && whole.item === item) return;           // on its way, or here already
    stopWhole();
    const video = mainVideo(slides.get(index));
    if (!autoFull() || !item || (video && video.dataset.whole)) return;
    startWhole(item, wholeRung(item, wholeRate, autoSecondsTo(index)), 0);
  }

  function startWhole(item, rung, tries) {
    const ctrl = new AbortController();
    Object.assign(whole, { item, ctrl, state: 'loading', rung, blob: null });
    wholeShow(item);
    (async () => {
      const response = await fetch(murl(item, `v/${rung}.m4s`), { signal: ctrl.signal });
      const size = Number(response.headers.get('Content-Length')) || 0;
      if (!response.ok || !size || size > WHOLE_MAX || !response.body) throw new Error('not fetched whole');
      const reader = response.body.getReader();
      // Gathered into blobs of a few MB as it comes, so that the whole of it
      // is never held twice over at the end.
      const held = [];
      let parts = [], loose = 0, got = 0, first = 0, firstBytes = 0, looked = 0;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        parts.push(value);
        got += value.length;
        loose += value.length;
        if (loose >= 8e6) { held.push(new Blob(parts)); parts = []; loose = 0; }
        const now = performance.now();
        if (!first) { first = looked = now; firstBytes = got; continue; }
        if (now - looked < 8000) continue;
        looked = now;
        // How fast it is coming, from the first piece on, and whether it is
        // here before the auto-scroll moves on to it.
        wholeRate = (got - firstBytes) / ((now - first) / 1000);
        const seconds = autoSecondsTo(state.items.indexOf(item));
        const fits = wholeRung(item, wholeRate, seconds);
        const late = (size - got) / wholeRate > seconds * 0.9;
        const sooner = Number(fits) < Number(rung) && rungBytes(item, fits) < size - got;
        const better = !tries && Number(fits) > Number(rung) && got < size / 3;
        if (late ? sooner : better) return fits;       // started again at that size
      }
      if (whole.ctrl !== ctrl) return '';
      whole.blob = new Blob([...held, ...parts], { type: 'video/mp4' });
      whole.state = 'done';
      wholeShow(item);
      return '';
    })().then((again) => {
      if (!again || whole.ctrl !== ctrl) return;
      ctrl.abort();
      startWhole(item, again, tries + 1);
    }).catch(() => {
      // Too large, refused, or cut off: the ordinary way, at the size chosen.
      if (whole.ctrl !== ctrl) return;
      ctrl.abort();
      whole.state = 'skip';
      wholeShow(item);
    });
  }

  function autoNext() {
    const next = autoNextIndex();
    if (next < 0) return;
    auto.quietUntil = performance.now() + 1500;
    if (next === state.current) {       // one item only: round again
      const video = currentVideo();
      autoBegin(next);
      if (video) { try { video.currentTime = 0; } catch { /* from where it is */ } video.play().catch(() => {}); }
      return;
    }
    auto.moving = true;
    if (next !== state.current + 1) return setCurrent(next);
    const from = state.current;
    step(1);                            // as a swipe would
    // Should the scroll not land (a browser that sends no scroll events), go there outright.
    setTimeout(() => { if (auto.running && state.current === from) setCurrent(next); }, 1200);
  }

  // The pill: tapping it stops the auto-scroll; its fill is how far through
  // this item the time is, and it breathes while waiting for the next.
  //
  // Nobody but the admin is to see any sign of the auto-scroll (asked for
  // outright). So the pill is not in the page's HTML: it is made here, the
  // first time the admin starts one, and shown only while the admin is
  // signed in. Once the sign-in runs out on the Visible wall the auto-scroll
  // carries on with nothing on the screen to show it; Back stops it, as it
  // closes any viewer.
  let autoPillMade = null;
  function autoPill() {
    if (autoPillMade) return autoPillMade;
    const pill = autoPillMade = el('button', 'pill auto');
    pill.setAttribute('aria-label', 'Stop auto-scroll');
    pill.innerHTML = '<i></i><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>'
      + '<span>Auto</span>';
    pill.addEventListener('click', stopAuto);
    document.querySelector('#viewer .foot').prepend(pill);
    return pill;
  }
  function drawAuto() {
    if (!autoPillMade && !(auto.running && isAdmin())) return;
    const pill = autoPill();
    pill.hidden = !auto.running || !isAdmin();
    if (pill.hidden) return;
    const video = currentVideo();
    let part = auto.shown / autoMs();
    if (video && isFinite(video.duration) && video.duration > 0) {
      part = (auto.plays + video.currentTime / video.duration) / neededPlays(video);
    }
    pill.classList.toggle('waiting', auto.done);
    pill.firstChild.style.width = Math.round(clamp(auto.done ? 1 : part, 0, 1) * 100) + '%';
  }

  // "Slow pan and zoom on photos": the photo drifts from the whole of it to a
  // spot chosen at random, four to sixteen per cent closer, eases to rest, and
  // drifts on to another, one leg every twenty seconds whatever the item's
  // time, never so far as to show its edge. The fingers take over the moment
  // a photo is pinched. An animation made here, not in app.css, so a phone
  // set to reduce motion still does what the box asks.
  // Smoothness: a picture that only slides and grows may be drawn at whole
  // pixels, and so slowly that shows as a judder, a step every second or so.
  // Turned by a fiftieth of a degree, too little to see, it is drawn between
  // pixels at every frame instead; translate3d keeps it on the graphics chip.
  const DRIFT_LEG = 20000;
  function driftPose(first) {
    if (first) return 'translate3d(0%, 0%, 0) scale(1) rotate(0deg)';
    const s = 1.04 + Math.random() * 0.12;
    const room = (s - 1) * 100 - 0.6;   // how far it may slide, less a sliver for the turn
    const x = -(0.3 + Math.random() * room), y = -(0.3 + Math.random() * room);
    return `translate3d(${x.toFixed(2)}%, ${y.toFixed(2)}%, 0) scale(${s.toFixed(3)}) rotate(0.02deg)`;
  }
  function startDrift(slide) {
    const layer = slide.querySelector('.zoom');
    if (!layer || !layer.animate) return;
    stopDrift(slide, true);
    let from = driftPose(true);
    const leg = () => {
      const to = driftPose(false);
      const drift = layer.animate([{ transform: from }, { transform: to }],
                                  { duration: DRIFT_LEG, easing: 'ease-in-out', fill: 'forwards' });
      from = to;
      drift.onfinish = () => {
        if (drifts.get(slide) !== drift) return;   // stopped, or started again, meanwhile
        leg();
        drift.cancel();                 // the next leg already holds the same pose
      };
      drifts.set(slide, drift);
    };
    leg();
  }
  function stopDrift(slide, undo) {
    const drift = slide && drifts.get(slide);
    if (!drift) return;
    if (!undo) return drift.pause();
    drift.cancel();
    drifts.delete(slide);
  }

  // A phone locks its screen after a minute or two untouched, and nobody is
  // meant to touch it. Over https, or on this machine, the page may ask for the
  // screen to stay on. Over plain http, as at a NAS's address, it may not; a
  // playing video keeps the screen on all the same, so a tiny blank one (16 by
  // 16 pixels, two seconds, no sound) plays out of sight, as NoSleep.js does.
  const AWAKE_MP4 = 'AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAAMObW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAAB9AAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAl10cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAB9AAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAABAAAAAQAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAAAfQAAAAAAABAAAAAAHVbWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAABAAAAAgABVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAABgG1pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAAUBzdGJsAAAAuHN0c2QAAAAAAAAAAQAAAKhhdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAABAAEABIAAAASAAAAAAAAAABDExhdmMgbGlieDI2NAAAAAAAAAAAAAAAAAAAAAAAAAAAGP//AAAALmF2Y0MBQsAe/+EAFmdCwB7ZHsBEAAADAAQAAAMAEDxYuSABAAVoy4PLIAAAABBwYXNwAAAAAQAAAAEAAAAUYnRydAAAAAAAAACwAAAAAAAAABhzdHRzAAAAAAAAAAEAAAAEAAAgAAAAABRzdHNzAAAAAAAAAAEAAAABAAAAHHN0c2MAAAAAAAAAAQAAAAEAAAAEAAAAAQAAACRzdHN6AAAAAAAAAAAAAAAEAAAADwAAAAoAAAAKAAAACQAAABRzdGNvAAAAAAAAAAEAAAM+AAAAPXVkdGEAAAA1bWV0YQAAAAAAAAAhaGRscgAAAAAAAAAAbWRpcmFwcGwAAAAAAAAAAAAAAAAIaWxzdAAAAAhmcmVlAAAANG1kYXQAAAALZYiEBTyYoAA/v4AAAAAGQZo4CnqAAAAABkGaVAKeoAAAAAVBmmAS9Q==';
  const awake = { want: false, lock: null, asking: false, video: null };
  function keepAwake(on) {
    awake.want = on;
    if (!on) {
      if (awake.lock) awake.lock.release().catch(() => {});
      awake.lock = null;
      if (awake.video) awake.video.pause();
      return;
    }
    const byVideo = () => {
      if (!awake.want) return;
      if (!awake.video) {
        const video = awake.video = el('video');
        video.muted = true;
        video.setAttribute('muted', '');
        video.playsInline = true;
        video.setAttribute('playsinline', '');
        video.disableRemotePlayback = true;
        video.loop = true;
        // Never at the end, where a phone might count it as stopped.
        video.addEventListener('timeupdate', () => { if (video.currentTime > 1) video.currentTime = Math.random() * 0.5; });
        const bytes = Uint8Array.from(atob(AWAKE_MP4), (c) => c.charCodeAt(0));
        video.src = URL.createObjectURL(new Blob([bytes], { type: 'video/mp4' }));
      }
      awake.video.play().catch(() => {});
    };
    if (!navigator.wakeLock) return byVideo();     // asked for inside the tap that started it
    if (awake.asking || (awake.lock && !awake.lock.released)) return;
    awake.asking = true;
    navigator.wakeLock.request('screen').then((lock) => {
      awake.asking = false;
      if (awake.want) awake.lock = lock; else lock.release().catch(() => {});
    }).catch(() => { awake.asking = false; byVideo(); });
  }
  // Out of sight the browser lets go of the screen; looked at again, ask again.
  document.addEventListener('visibilitychange', () => { if (!document.hidden && auto.running) keepAwake(true); });

  // The time, the two boxes, and Start. Kept to that on purpose: the owner
  // asked for a simple, clean menu. In two places, both the admin's: the
  // admin menu, which starts from the picture at the top of the wall, and the
  // very top of an item's (i), which starts from that item (asked for, so
  // that it can be started from anywhere in the stream). `from` says where.
  function autoPanel(from) {
    const box = el('div');
    const minutes = el('input');
    minutes.type = 'number';
    minutes.min = '1';
    minutes.max = '999';
    minutes.step = '1';
    minutes.inputMode = 'numeric';
    minutes.setAttribute('aria-label', 'Minutes on each item');
    minutes.value = String(Math.max(1, Math.round(autoMinutes())));
    const keep = () => {
      const m = Math.round(Number(minutes.value));
      if (m >= 1 && m !== autoPrefs.minutes) { autoPrefs.minutes = Math.min(999, m); saveAuto(); }
      minutes.value = String(Math.max(1, Math.round(autoMinutes())));
    };
    minutes.addEventListener('change', keep);
    box.appendChild(option('Minutes on each item', minutes));
    box.appendChild(tickBox('Wait for full quality', 'wait'));
    box.appendChild(tickBox('Slow pan and zoom on photos', 'pan'));
    const go = el('button', 'plain primary-ish', 'Start auto-scroll');
    const note = el('p', 'error');
    go.addEventListener('click', () => {
      keep();
      const index = from();
      if (index < 0) { note.textContent = 'There is nothing on this wall to show.'; return; }
      keepAwake(true);                  // inside the tap: a phone lets a clip start only then
      const begin = () => startAuto(index);
      if (!pushed) { closeSheet(); return begin(); }
      // The sheet closes as Back closes it, and the auto-scroll starts after that.
      window.addEventListener('popstate', () => setTimeout(begin, 0), { once: true });
      history.back();
    });
    box.append(go, note);
    return box;
  }
  function tickBox(label, key) {
    const row = el('label', 'check');
    const box = el('input');
    box.type = 'checkbox';
    box.checked = !!autoPrefs[key];
    box.addEventListener('change', () => { autoPrefs[key] = box.checked; saveAuto(); });
    row.append(box, el('span', '', label));
    return row;
  }

  // -------------------------------------------------------------- sheets --
  const sheet = $('sheet');
  let sheetTimer = 0;
  function openSheet(build, again = false) {
    clearInterval(sheetTimer);
    const body = $('sheet-body');
    body.textContent = '';
    build(body);
    const wasOpen = !sheet.hidden;
    sheet.style.transition = '';
    sheet.style.transform = '';
    sheet.hidden = false;
    $('backdrop').hidden = false;
    if (!wasOpen) { sheet.scrollTop = 0; holdPage(true); if (!again) pushHistory(); }
  }
  // The same sheet, closed and opened afresh with what `build` makes, keeping
  // its place in the history. For a sheet whose content has changed out of all
  // recognition: in Firefox, a menu that grew from a few lines to many while it
  // stayed open would not scroll with two fingers until the page was reloaded.
  function reopenSheet(build) {
    closeSheet();
    requestAnimationFrame(() => requestAnimationFrame(() => openSheet(build, true)));
  }
  function closeSheet() {
    clearInterval(sheetTimer);
    sheet.hidden = true;
    sheet.style.transition = '';
    sheet.style.transform = '';
    $('backdrop').hidden = true;
    holdPage(false);
  }
  // With a mouse or a trackpad the page behind a sheet is held still (app.css
  // says why). Where scrollbars take up room, holding the page takes its
  // scrollbar away, so that room is given back as padding and the wall behind
  // does not shift sideways.
  function holdPage(on) {
    const body = document.body;
    const root = document.documentElement;
    if (!on) { body.classList.remove('sheet-open'); body.style.paddingRight = ''; return; }
    const before = root.clientWidth;
    body.classList.add('sheet-open');
    const gained = root.clientWidth - before;
    if (gained > 0) body.style.paddingRight = gained + 'px';
  }
  $('backdrop').addEventListener('click', () => history.back());
  // Beside a sheet, with the page held, there is nothing else to scroll, so
  // two fingers on a trackpad there, or a mouse wheel, move the sheet too:
  // nobody has to aim. It only listens. Refusing the event (preventDefault)
  // is not needed with the page held, and in Firefox it left later swipes
  // over the sheet itself doing nothing.
  const pageIsHeld = window.matchMedia('(hover: hover) and (pointer: fine)');   // as in app.css
  $('backdrop').addEventListener('wheel', (event) => {
    if (event.ctrlKey || !pageIsHeld.matches) return;            // ctrl: a pinch, the browser's zoom
    const by = event.deltaY;                                     // read first, so Firefox counts in pixels
    sheet.scrollTop += event.deltaMode === 1 ? by * 20 : by;
  }, { passive: true });

  // Drag a sheet down to close it. The handle always starts a drag; anywhere
  // else does too, but only when the sheet is scrolled to its top, so that
  // reading a long sheet and dismissing it do not fight over the same gesture.
  let pull = null;
  sheet.addEventListener('touchstart', (event) => {
    if (event.touches.length !== 1) { pull = null; return; }
    const onGrip = !!event.target.closest('#sheet-grip');
    if (!onGrip && sheet.scrollTop > 0) { pull = null; return; }
    pull = { y: event.touches[0].clientY, by: 0, onGrip, active: false, at: performance.now() };
  }, { passive: true });
  sheet.addEventListener('touchmove', (event) => {
    if (!pull) return;
    const by = event.touches[0].clientY - pull.y;
    if (!pull.active) {
      if (by < -6 && !pull.onGrip) { pull = null; return; }      // scrolling the sheet's content
      if (by < 8) return;
      pull.active = true;
      sheet.style.transition = 'none';
    }
    event.preventDefault();
    pull.by = Math.max(0, by);
    sheet.style.transform = `translateY(${pull.by}px)`;
  }, { passive: false });
  function endPull() {
    if (!pull) return;
    const done = pull;
    pull = null;
    if (!done.active) return;
    const quick = done.by / Math.max(1, performance.now() - done.at) > 0.5;   // a flick counts
    sheet.style.transition = 'transform 0.18s ease-out';
    if (done.by > 90 || (quick && done.by > 30)) {
      sheet.style.transform = 'translateY(100%)';
      setTimeout(() => { if (!sheet.hidden) history.back(); }, 160);
    } else {
      sheet.style.transform = '';
    }
  }
  sheet.addEventListener('touchend', endPull);
  sheet.addEventListener('touchcancel', endPull);
  // With a mouse there is nothing to drag with; the handle is a close button.
  $('sheet-grip').addEventListener('click', () => history.back());

  function rows(pairs) {
    const list = el('dl', 'rows');
    for (const [label, value] of pairs) {
      if (value === null || value === undefined || value === '' || value === false) continue;
      const row = el('div');
      row.appendChild(el('dt', '', label));
      const dd = el('dd');
      if (value instanceof Node) dd.appendChild(value); else dd.textContent = value;
      row.appendChild(dd);
      list.appendChild(row);
    }
    return list;
  }
  function segmented(options, value, onPick) {
    const group = el('div', 'segmented');
    for (const [key, label] of options) {
      const button = el('button', '', label);
      button.type = 'button';
      button.setAttribute('aria-pressed', String(key === value));
      button.addEventListener('click', () => {
        for (const b of group.children) b.setAttribute('aria-pressed', String(b === button));
        onPick(key);
      });
      group.appendChild(button);
    }
    return group;
  }
  function option(label, control, wrap) {
    const row = el('div', 'option' + (wrap ? ' wrap' : ''));
    row.append(el('span', '', label), control);
    return row;
  }

  // ------------------------------------------------------------- details --
  $('viewer-info').addEventListener('click', async () => {
    const item = state.items[state.current];
    if (!item) return;
    openSheet((body) => body.appendChild(el('p', 'muted', 'Loading…')));
    let d;
    try { d = await api('/api/item/' + item.id); } catch (error) {
      return openSheet((body) => body.appendChild(el('p', 'error', error.message)));
    }
    openSheet((body) => {
      const slide = slides.get(state.current);
      const video = currentVideo();
      const meta = d.meta || {};
      // The admin's alone, above everything else: start the auto-scroll from here.
      if (isAdmin()) body.append(el('h3', 'first', 'Auto-scroll'), autoPanel(() => state.current));
      body.appendChild(el('h2', '', d.name));

      const section = el('button', 'linkish', d.section_path);
      section.addEventListener('click', () => setSection(d.section_id));
      let place = null;
      if (d.place) {
        place = el('a', '', d.place);
        place.href = `https://maps.apple.com/?ll=${meta.lat},${meta.lon}&q=${meta.lat},${meta.lon}`;
        place.target = '_blank';
        place.rel = 'noopener noreferrer';
      }
      // Tags: the other categories this item also appears in. Anyone can tap
      // one to see that category; only the admin can add or remove them.
      const tagBox = el('div', 'tags');
      const offer = el('div', 'tags');
      const drawTags = () => {
        tagBox.textContent = '';
        for (const tag of d.tags) {
          const chip = el('span', 'tag');
          const open = el('button', '', '#' + tag);
          open.addEventListener('click', () => setTag(tag));
          chip.appendChild(open);
          if (isAdmin()) {
            const remove = el('button', 'x', '×');
            remove.setAttribute('aria-label', 'Remove the tag ' + tag);
            remove.addEventListener('click', () => changeTag(tag, false));
            chip.appendChild(remove);
          }
          tagBox.appendChild(chip);
        }
        if (!d.tags.length) tagBox.appendChild(el('span', 'muted', 'None'));
        // What can be added with one tap: categories that already exist here.
        offer.textContent = '';
        const have = new Set(d.tags.map((t) => t.toLowerCase()).concat([d.section_name.toLowerCase()]));
        for (const name of (d.suggest || []).filter((n) => !have.has(n.toLowerCase())).slice(0, 16)) {
          const chip = el('button', 'tag add', '+ ' + name);
          chip.addEventListener('click', () => changeTag(name, true));
          offer.appendChild(chip);
        }
      };
      const tagNote = el('p', 'error');
      const changeTag = async (tag, on) => {
        tagNote.textContent = '';
        try {
          d.tags = (await api('/api/admin/tag', { id: item.id, tag, on })).tags;
          drawTags();
        } catch (error) { tagNote.textContent = error.message; }
      };
      drawTags();

      body.appendChild(rows([
        ['Section', section],
        ['Taken', fmtTaken(d.taken_local) + (d.taken_src === 'file date' ? ' (file date; the camera recorded none)' : '')],
        ['Place', place],
        ['Camera', meta.camera],
        ['Tags', (d.tags.length || isAdmin()) ? tagBox : null],
      ]));

      if (isAdmin()) {
        // A tag puts this item in another category as well as its own folder.
        const form = el('form', 'tag-form');
        const input = el('input');
        input.type = 'text';
        input.placeholder = 'Also show in… (a new or existing category)';
        input.autocapitalize = 'words';
        input.enterKeyHint = 'done';
        const addIt = el('button', 'plain', 'Add');
        addIt.type = 'submit';
        form.append(input, addIt);
        form.addEventListener('submit', async (event) => {
          event.preventDefault();
          if (!input.value.trim()) return;
          await changeTag(input.value, true);
          if (!tagNote.textContent) input.value = '';
        });
        body.append(offer, form, tagNote);
      }

      if (d.kind === 'video') {
        // Change the size of this video here, without leaving it.
        const sizes = [['auto', 'Auto']].concat((d.rungs || []).map((r) => [r.name, r.name + 'p']));
        body.appendChild(option('Video size', segmented(sizes, (slide && slide.dataset.size) || 'auto',
          (choice) => setVideoSize(choice)), true));
      }

      // A Live Photo shown by a moment of its clip is that moment's size on the
      // wall; the photo as taken is the still's.
      const kp = d.key_photo;
      const [w, h] = kp ? kp.still : [d.width, d.height];
      const megapixels = d.kind === 'photo' ? ` (${(w * h / 1e6).toFixed(0)} MP)` : '';
      const kind = [meta.format, meta.codec, d.hdr ? 'HDR original' : null,
                    meta.fps ? meta.fps + ' fps' : null].filter(Boolean).join(' · ');
      let showing = null;
      if (d.kind === 'video' && video && video.videoHeight) {
        showing = Math.min(video.videoWidth, video.videoHeight) + 'p';
      } else if (d.kind === 'photo' && slide) {
        showing = slide.dataset.size === 'l' ? 'Large copy' : 'Screen copy';
      }
      // Choosing a key photo is the admin's, on either wall, and so is all that
      // is said about it here. Anyone else sees the picture chosen, and these
      // details as they always were.
      const keying = d.live && isAdmin();
      body.appendChild(rows([
        ['Type', (d.kind === 'video' ? 'Video' : d.live ? 'Live Photo' : 'Photo') + (kind ? ' · ' + kind : '')],
        ['Size', `${w} × ${h}${megapixels}`],
        ['Key photo', !keying ? null : kp
          ? `A moment from the ${kp.from === 'clip' ? 'clip' : "clip's small copy"}, ${kp.w} × ${kp.h}`
          : 'The photo as taken'],
        ['Length', d.kind === 'video' ? fmtClock(d.duration) : null],
        ['Showing', showing],
        ['Original', fmtBytes(d.size) + (d.trashed ? ' (not kept: small copies only)' : '')],
        ['Converted', isAdmin() ? d.made_by : null],
        ['Lens', meta.lens],
        ['Exposure', meta.exposure],
      ]));
      if (keying && slide) {
        const change = el('button', 'plain', 'Change the key photo');
        change.addEventListener('click', () => {
          const index = state.current;
          history.back();                 // the sheet closes; the viewer stays on this item
          openKeyer(index);
        });
        body.appendChild(change);
      }

      body.appendChild(el('h3', '', 'Copies on the server'));
      if (d.kind === 'video') {
        body.appendChild(rows((d.rungs || []).map((r) =>
          [r.name + 'p', `${fmtBytes(r.bytes)} · ${(r.avg / 1e6).toFixed(1)} Mbit/s`])
          .concat([['Wall preview', d.sizes && d.sizes.p ? fmtBytes(d.sizes.p) : null]])));
      } else {
        const s = d.sizes || {};
        body.appendChild(rows([['Thumbnail', s.t && fmtBytes(s.t)], ['Screen', s.m && fmtBytes(s.m)],
                               ['Large', s.l && fmtBytes(s.l)],
                               ['Live clip', s.live && `${fmtBytes(s.live)} · hold the photo, or tap LIVE, to play it`],
                               ['Clip as taken', keying && s.clip && `${fmtBytes(s.clip)} · kept for choosing the key photo, never sent`]]));
        if (slide && slide.dataset.size !== 'l') {
          const best = el('button', 'plain', 'Show the large copy');
          best.addEventListener('click', () => {
            const sharp = slide.querySelector('img.sharp');
            if (sharp) { sharp.src = photoUrl(item, 'l'); slide.dataset.size = 'l'; }
            history.back();
          });
          body.appendChild(best);
        }
      }
    });
  });

  // ------------------------------------------------------------ sections --
  $('open-sections').addEventListener('click', async () => {
    try { await loadSections(); } catch { /* show what we have */ }
    openSheet((body) => {
      body.appendChild(el('h2', '', state.realm === 'hidden' ? 'Hidden sections' : 'Sections'));
      if (isAdmin()) {
        body.appendChild(option('Wall', segmented([['visible', 'Visible'], ['hidden', 'Hidden']],
          state.realm, (realm) => setRealm(realm))));
      }
      const describe = (p, v, w) => [p ? plural(p, 'photo') : null, v ? plural(v, 'video') : null,
        w && isAdmin() ? `${w} still converting` : null].filter(Boolean).join(' · ') || 'Empty';
      const add = (current, name, detail, cover, depth, go) => {
        const open = el('button', 'section-row' + (current ? ' current' : ''));
        open.style.paddingLeft = depth * 18 + 'px';
        const thumb = el('div', 'cover');
        if (cover) thumb.style.backgroundImage = `url(${photoUrl(cover, 't')})`;
        const text = el('div', 'text');
        text.append(el('div', 'name', name), el('div', 'muted small', detail));
        open.append(thumb, text);
        open.addEventListener('click', go);
        body.appendChild(open);
      };
      add(state.section === null && state.tag === null, 'Everything',
          describe(state.totals.photos, state.totals.videos),
          (state.sections.find((s) => s.cover) || {}).cover, 0, () => setSection(null));
      for (const s of state.sections) {
        if (!isAdmin() && !(s.total_photos + s.total_videos)) continue;
        add(state.section === s.id, s.name, describe(s.total_photos, s.total_videos, s.waiting),
            s.cover, s.depth, () => setSection(s.id));
      }
      if (state.tags.length) {
        body.appendChild(el('h3', '', 'Tags'));
        for (const t of state.tags) {
          add(state.tag !== null && state.tag.toLowerCase() === t.tag.toLowerCase(), '#' + t.tag,
              describe(t.photos, t.videos), t.cover, 0, () => setTag(t.tag));
        }
      }
    });
  });

  // ------------------------------------------------- settings and admin --
  $('open-settings').addEventListener('click', () => openSettings());

  function openSettings(again = false) {
    (again ? reopenSheet : openSheet)((body) => {
      body.appendChild(el('h2', '', 'Settings'));
      body.appendChild(option('Video quality', segmented(
        [[0, 'Auto'], [720, '720p+'], [1080, '1080p+'], [1440, '1440p']], prefs.videoMin,
        (v) => { prefs.videoMin = v; savePrefs(); })));
      body.appendChild(option('Photo quality', segmented(
        [[false, 'Auto'], [true, 'Large']], prefs.photoHigh,
        (v) => { prefs.photoHigh = v; savePrefs(); })));
      body.appendChild(option('Load ahead', segmented(
        [[0, 'Off'], [1, '1'], [3, '3'], [5, '5']], prefs.ahead,
        (v) => { prefs.ahead = v; savePrefs(); })));
      body.appendChild(option('Moving previews', segmented(
        [[true, 'On'], [false, 'Off']], prefs.motion,
        (v) => { prefs.motion = v; savePrefs(); if (v) schedulePreviews(); else stopAllPreviews(); })));
      const speed = link.samples
        ? `About ${(link.bps / 8e6).toFixed(link.bps < 8e6 ? 2 : 1)} MB per second, ${Math.round(link.rtt)} ms away`
        : 'Not measured yet. Scroll a little first.';
      body.appendChild(rows([['Link', speed],
                             ['Version', state.me.version + (state.me.simulated ? ` · simulated link ${state.me.simulated}` : '')],
                             // The credit the data's licence asks for, where the names are shown.
                             ['Place names', 'GeoNames (geonames.org), CC BY 4.0']]));

      body.appendChild(el('h3', '', 'Admin'));
      if (isAdmin()) adminPanel(body);
      else unlockForm(body);

      // Only when a viewing password is in force is there anything to sign out of.
      if (!isAdmin() && state.me.has_viewer) {
        const out = el('button', 'plain danger', 'Sign out');
        out.addEventListener('click', async () => { await api('/api/logout', {}); await refreshMe(); showGate(); });
        body.appendChild(out);
      }
    });
  }

  // The admin password, asked for here and nowhere else. Getting it right
  // changes this menu and nothing else: the wall stays the Visible one until
  // the Wall switch in the menu is used. Somebody signing in to add pictures
  // to the Visible wall is not shown the Hidden one for it.
  function unlockForm(body) {
    const form = el('form');
    const input = el('input');
    input.type = 'password';
    input.placeholder = 'Admin password';
    input.autocomplete = 'current-password';
    const note = el('p', 'error');
    const go = el('button', 'plain', 'Unlock');
    go.type = 'submit';
    form.append(input, note, go);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      go.disabled = true;
      note.textContent = '';
      try {
        const result = await api('/api/login', { password: input.value });
        await refreshMe();
        if (result.role !== 'admin') { note.textContent = 'That is not the admin password.'; return; }
        openSettings(true);       // as if just opened: see reopenSheet
      } catch (error) {
        note.textContent = error.message;
      } finally {
        go.disabled = false;
      }
    });
    body.appendChild(form);
  }

  function adminPanel(body) {
    // First thing in the panel, so it is never hunted for: leave admin and go
    // back to the site as everyone else sees it.
    const out = el('button', 'plain', 'Log out of admin');
    out.addEventListener('click', async () => {
      await api('/api/logout', {});
      await refreshMe();
      if (!state.me.role) return showGate();
      await setRealm('visible');
    });
    body.appendChild(out);
    body.appendChild(el('p', 'muted small', 'Logging out shows the site as everyone else sees it. The switch below does the same without logging out.'));
    body.appendChild(option('Wall', segmented([['visible', 'Visible'], ['hidden', 'Hidden']],
      state.realm, (realm) => setRealm(realm))));

    body.appendChild(el('h3', '', 'Auto-scroll'));
    body.appendChild(autoPanel(tileNearTop));

    body.appendChild(el('h3', '', 'Library'));
    const status = body.appendChild(el('div'));
    status.appendChild(el('p', 'muted', 'Loading…'));
    const steps = el('ol', 'steps');
    const refresh = () => renderStatus(status, steps).catch(() => {});
    refresh();
    sheetTimer = setInterval(refresh, 3000);
    const rescan = el('button', 'plain', 'Look for new files now');
    // Pressed by a person, it also checks the small copies of everything finished.
    rescan.addEventListener('click', async () => { await api('/api/admin/rescan', { check: true }); refresh(); });
    body.appendChild(rescan);

    body.appendChild(el('h3', '', 'Adding photos and videos'));
    body.appendChild(uploadPanel());
    const fromNas = el('details', 'more');
    fromNas.appendChild(el('summary', '', 'From the NAS itself'));
    fromNas.appendChild(steps);
    body.appendChild(fromNas);

    body.appendChild(el('h3', '', 'Passwords'));
    body.appendChild(passwordForm());

  }

  async function renderStatus(target, steps) {
    const s = await api('/api/admin/status');
    const c = s.counts, w = s.worker;
    target.textContent = '';
    for (const [tool, ok] of Object.entries(s.tools)) {
      if (!ok) target.appendChild(el('p', 'fail', `Missing on the server: ${tool}`));
    }
    let doing = 'Idle. Everything found has been converted.';
    if (w.phase === 'video' || w.phase === 'photos') doing = `Converting ${w.current}`;
    else if (w.phase === 'scanning') doing = 'Looking for new files';
    else if (w.phase === 'checking') doing = 'Checking every small copy';
    else if (w.phase !== 'idle') doing = w.phase;
    target.appendChild(el('p', '', doing));
    if (w.phase === 'video' || w.phase === 'photos' || w.phase === 'checking') {
      const meter = el('div', 'meter');
      meter.appendChild(el('i')).style.width = Math.round(w.fraction * 100) + '%';
      target.appendChild(meter);
    }
    target.appendChild(rows([
      ['Visible', plural(c.visible, 'item')],
      ['Hidden', plural(c.hidden, 'item')],
      ['Waiting', c.waiting ? `${c.photos_waiting} photos, ${c.videos_waiting} videos (${fmtBytes(c.video_bytes_waiting)})` : null],
      ['Speed', w.video_speed ? `${w.video_speed}× (one minute of video takes ${fmtSpan(60 / w.video_speed)})` : null],
      ['Time left', s.eta_seconds ? 'about ' + fmtSpan(s.eta_seconds) : null],
      ['Live Photos', (c.live || c.paired) ? [c.live ? `${c.live.toLocaleString()} with their clip` : null,
        c.paired ? `${plural(c.paired, 'clip')} ${s.keeps_originals ? 'kept beside the photo' : 'waiting for the photo'}` : null]
        .filter(Boolean).join(' · ') : null],
      ['Failed', c.failed || null],
      // What the last check of the finished items found, and what is in the
      // folders without being an item: the two things "idle" cannot say.
      ['Checked', w.checked ? `${plural(w.checked.whole, 'item')} whole`
        + (w.checked.damaged ? `, ${w.checked.damaged} damaged` : '')
        + ' · ' + new Date(w.checked.at * 1000).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : null],
      ['Left alone', w.last_scan ? [
        w.last_scan.partial ? `${plural(w.last_scan.partial, 'file')} that never finished arriving` : null,
        w.last_scan.empty ? plural(w.last_scan.empty, 'empty file') : null,
        w.last_scan.other ? `${plural(w.last_scan.other, 'file')} not a photo or video` : null,
      ].filter(Boolean).join(' · ') || null : null],
      ['Space', s.storage ? `${fmtBytes(s.storage.copies)} of small copies`
        + (s.storage.awaiting ? ` · ${fmtBytes(s.storage.awaiting)} full-size still to convert` : '')
        + ` · ${fmtBytes(s.storage.free)} free` : null],
      ['Video chip', s.hardware.in_use ? 'In use: ' + s.hardware.path
        : 'Not in use. ' + (s.hardware.why || 'Converting in software.')],
      ['Chip misses', s.hardware.fallbacks ? `${s.hardware.fallbacks} files redone in software` : null],
    ]));
    for (const f of s.failures) target.appendChild(el('p', 'fail', `${f.rel_path}: ${f.error}`));
    // Which files were left alone, so that nobody has to go looking for them.
    const left = (w.last_scan && w.last_scan.left) || [];
    const why = { partial: 'never finished arriving', empty: 'empty', other: 'not a photo or video' };
    for (const f of left.slice(0, 12)) target.appendChild(el('p', 'muted small', `${f.path}: ${why[f.why] || f.why}`));
    if (left.length > 12) target.appendChild(el('p', 'muted small', `and ${left.length - 12} more`));
    if (c.failed) {
      const retry = el('button', 'plain', 'Try the failed ones again');
      retry.addEventListener('click', () => api('/api/admin/retry', {}));
      target.appendChild(retry);
    }
    up.piece = s.upload_piece || up.piece;
    if (steps && !steps.childElementCount) {
      const line = (html) => { const li = el('li'); li.innerHTML = html; steps.appendChild(li); };
      line('Files come in over <b>WebDAV</b>, from the machine Waterfall runs on. On a Synology NAS: open <b>File Station</b>, then <b>Tools → Remote Connection → Connection Setup</b>.');
      line(`Choose <b>WebDAV</b>. Address <b>127.0.0.1</b>, port <b>${s.import_port}</b>, any username, and the admin password.`);
      line('<b>Copy</b> folders into <b>Visible</b> or <b>Hidden</b>. Each folder becomes a section.');
      line('Anything put in <b>Hidden</b> is only ever shown on the Hidden wall.');
      if (!s.keeps_originals) {
        line('<b>Copy, never move.</b> Waterfall keeps small copies only: once a file is converted its full-size copy here is removed, and a small text file with its name is left in its place.');
      }
    }
  }

  // -------------------------------------------------------------- upload --
  // Files chosen on this device are sent a piece at a time and put together on
  // the server, so a dropped connection costs one piece and not the file. A few
  // files travel at once; the rest wait their turn, which is what lets several
  // hundred be chosen in one go.
  //
  // It is built for a phone on a slow link, which shapes two things. A piece is
  // sized to take a few seconds on the link as it is, so little is lost when
  // one fails and the number on the panel moves every few seconds. And a
  // connection that drops, as it does every time a phone sleeps, is waited for:
  // the upload picks up where it stopped when the page is looked at again.
  //
  // It carries on when the sheet is closed (a pill on the wall shows it), and
  // when the admin sign-in lapses (it holds a ticket for exactly this).
  const PHOTO_NAME = /\.(heic|heif|jpe?g|png|webp|gif|tiff?|avif)$/i;
  const VIDEO_NAME = /\.(mov|mp4|m4v|mkv|avi|webm|3gp|mts)$/i;
  const UP_AT_ONCE = 3;
  // How carefully pieces are sent. It starts at 0, the ordinary way. Each time
  // a piece goes unanswered it goes up one, for as long as the page lives:
  // with the server asked to close the connection after each answer, and in
  // pieces no larger than this. Since pieces go by fetch (upRequest, below)
  // nothing has been seen to need it; it is kept for a road that loses answers.
  const UP_CARE = [Infinity, 512 * 1024, 128 * 1024, 32 * 1024];
  const UP_SMALLEST = 256 * 1024;
  const up = { piece: 4 * 1024 * 1024, jobs: [], next: 0, active: 0, done: 0, skipped: 0, failed: [],
               bytes: 0, sent: 0, running: false, stopped: false, waiting: false, ticket: null,
               rate: 0, rateAt: 0, rateSent: 0, where: '', note: '', why: '', realm: null, section: '',
               flying: new Set(), waits: new Set(),
               // `?care=1` (to 3) in the address starts it careful: for trying the
               // careful ways of sending against a server where the ordinary way stalls.
               care: clamp(Math.floor(Number(new URLSearchParams(location.search).get('care')) || 0), 0, 3),
               view: null, drawTimer: 0, shownDone: false };

  function upReset() {
    Object.assign(up, { jobs: [], next: 0, active: 0, done: 0, skipped: 0, failed: [], bytes: 0, sent: 0,
                        running: false, stopped: false, waiting: false, rate: 0, rateAt: 0, rateSent: 0,
                        note: '', shownDone: false });
  }

  async function upAdd(files, realm, section) {
    if (!up.running) upReset();
    let left = 0;
    const fresh = [];
    for (const file of files) {
      const name = file.webkitRelativePath || file.name;
      if (name.split('/').some((part) => part.startsWith('.'))) continue;      // system clutter
      if (!PHOTO_NAME.test(file.name) && !VIDEO_NAME.test(file.name)) { left += 1; continue; }
      fresh.push({ file, realm, path: (section ? section + '/' : '') + name, sent: 0,
                   id: [...crypto.getRandomValues(new Uint8Array(8))].map((b) => b.toString(16).padStart(2, '0')).join('') });
    }
    // Photos before videos: the wall fills sooner, and the still of a Live
    // Photo is there before its clip arrives looking for it.
    fresh.sort((a, b) => (VIDEO_NAME.test(a.file.name) - VIDEO_NAME.test(b.file.name)) || a.path.localeCompare(b.path));
    up.jobs.push(...fresh);
    up.bytes += fresh.reduce((sum, job) => sum + job.file.size, 0);
    up.where = (realm === 'hidden' ? 'Hidden' : 'Visible') + ' › ' + (section || 'Unsorted');
    up.note = left ? `${plural(left, 'file')} left out: not a photo or video.` : '';
    if (fresh.length && !up.running) {
      up.running = true;
      up.stopped = false;
      up.rateAt = performance.now();
      up.rateSent = 0;
      upDraw();
      try { up.ticket = (await api('/api/admin/upload-ticket', {})).ticket; } catch { up.ticket = null; }
    }
    upPump();
    upDraw();
  }

  function upPump() {
    while (up.running && !up.stopped && up.active < UP_AT_ONCE && up.next < up.jobs.length) {
      const job = up.jobs[up.next++];
      up.active += 1;
      upSend(job).then((result) => {
        if (result.skipped) up.skipped += 1;
        up.done += 1;
      }).catch((error) => {
        up.sent -= job.sent;
        up.bytes -= job.file.size;
        job.sent = 0;
        up.failed.push({ job, why: error.message });
      }).finally(() => {
        up.active -= 1;
        if (!up.active && (up.stopped || up.next >= up.jobs.length)) {
          up.running = false;
          up.waiting = false;
          if (up.done > up.skipped && isAdmin()) api('/api/admin/rescan', {}).catch(() => {});
        }
        upPump();
        upDraw();
      });
    }
  }

  async function upSend(job) {
    const total = job.file.size;
    if (!total) throw new Error('The file is empty.');
    let offset = 0;
    let piece = Math.min(up.piece, 512 * 1024);
    for (;;) {
      if (up.stopped) throw new Error('Stopped.');
      const began = performance.now();
      const size = Math.min(piece, total - offset, UP_CARE[up.care]);
      const result = await upPiece(job, offset, size, total);
      if (result.again) continue;       // the same place in the file, sent more carefully
      const now = result.done ? total : result.have;
      up.sent += now - job.sent;
      job.sent = now;
      offset = now;
      upDraw();
      if (result.done) return result;
      // Aim for a piece that takes about six seconds on this link, today.
      const seconds = Math.max(0.05, (performance.now() - began) / 1000);
      piece = clamp(Math.round(size / seconds * 6), UP_SMALLEST, up.piece);
    }
  }

  // Wait before trying again, but not past the moment the phone wakes or the
  // network comes back: that is exactly when to try.
  function upWait(ms) {
    return new Promise((resolve) => {
      const done = () => {
        clearTimeout(timer);
        up.waits.delete(done);
        window.removeEventListener('online', done);
        document.removeEventListener('visibilitychange', seen);
        resolve();
      };
      up.waits.add(done);
      const seen = () => { if (!document.hidden) done(); };
      const timer = setTimeout(done, ms);
      window.addEventListener('online', done);
      document.addEventListener('visibilitychange', seen);
    });
  }

  // Stop means now. A piece on its way is dropped and a wait is ended, so the
  // page says "Stopped" at once instead of when the piece in hand gives up.
  function upStop() {
    up.stopped = true;
    for (const going of [...up.flying]) going.abort();
    for (const wake of [...up.waits]) wake();
    upDraw();
  }

  // One piece. A link that drops it is waited for and tried again for as long
  // as the page lives; an answer that says no (wrong kind of file, no room,
  // not allowed) is final for that file.
  async function upPiece(job, offset, size, total) {
    let wait = 1000;
    let asked = false;
    const bytes = await upBytes(job, offset, size);
    for (;;) {
      const answer = await upRequest(job, offset, size, total, bytes);
      if (answer.status >= 200 && answer.status < 300) {
        // An answer that says yes says how much of the file is held. One that
        // does not was cut short on its way here, and counts as no answer:
        // taking it for a yes once turned every number on the panel to NaN.
        if (answer.data.done || Number.isFinite(answer.data.have)) {
          if (up.waiting) { up.waiting = false; upDraw(); }
          return answer.data;
        }
        answer.status = 0;
      }
      if (answer.status === 403 && isAdmin() && !asked) {
        // The ticket has gone stale (a change of password does that). Ask once more.
        asked = true;
        try { up.ticket = (await api('/api/admin/upload-ticket', {})).ticket; continue; } catch { /* below */ }
      }
      if (answer.status >= 400 && answer.status < 500 && answer.status !== 408 && answer.status !== 429) {
        if (answer.status === 401 || answer.status === 403) {
          up.stopped = true;
          throw new Error('Not signed in as the admin. Unlock admin and send it again.');
        }
        throw new Error(answer.data.error || `Refused (${answer.status}).`);
      }
      if (up.stopped) throw new Error('Stopped.');
      // No answer came back in good time: send more carefully from here on,
      // starting with this same place in the file.
      // (The server may well hold the piece; asked again, it says how far it
      // has got and the page carries on from there.)
      if (answer.unanswered && up.care < UP_CARE.length - 1) {
        up.care += 1;
        up.why = '';
        up.waiting = true;
        upDraw();
        return { again: true };
      }
      // Three different troubles, and the page should not call them all "the
      // connection": this device is offline; a piece went and no answer came
      // back; or Waterfall answered and the answer was an error of its own.
      up.why = answer.status ? (answer.data.error || `error ${answer.status}`)
        : navigator.onLine === false ? 'offline' : '';
      up.waiting = true;
      upDraw();
      await upWait(wait);
      wait = Math.min(30000, wait * 2);
      if (up.stopped) throw new Error('Stopped.');
    }
  }

  // A piece is read into memory and sent from there, which is the way that was
  // proven from an iPhone (upRequest, below). A few megabytes held for a moment
  // is a small price for not relying on how a browser streams a file.
  async function upBytes(job, offset, size) {
    const part = job.file.slice(offset, offset + size);
    let late = 0;
    try {
      const bytes = await Promise.race([
        part.arrayBuffer ? part.arrayBuffer() : new Response(part).arrayBuffer(),
        new Promise((_, no) => { late = setTimeout(() => no(new Error('slow')), 60000); })]);
      if (bytes.byteLength !== size) throw new Error('short');
      return bytes;
    } catch {
      throw new Error('This browser could not read the file. Choose it again.');
    } finally {
      clearTimeout(late);
    }
  }

  const upHeaders = (job, offset, total) => {
    const headers = { 'X-Waterfall': '1', 'Content-Type': 'application/octet-stream',
                      'X-Upload-Path': encodeURIComponent(job.path), 'X-Upload-Id': job.id,
                      'X-Upload-Offset': String(offset), 'X-Upload-Total': String(total),
                      'X-Upload-Modified': String(Math.round((job.file.lastModified || 0) / 1000)) };
    if (up.ticket) headers['X-Upload-Ticket'] = up.ticket;
    return headers;
  };

  // The request itself: fetch(), the way Waterfall 0.6.0 sent every piece.
  // Versions 0.6.1 to 0.6.3 used XMLHttpRequest, the one interface that says
  // how much of a body has left, and Safari (on an iPhone, and on a Mac to the
  // NAS) never finished one that carried a piece of half a megabyte or more:
  // the whole answer reached the phone and was read, and the request stayed
  // open for ever, however the server answered (one packet or two, the
  // connection kept or closed). Pieces of 32 KB and less did finish. fetch()
  // finished every way it was tried. It cannot say how much has left, so the
  // numbers move a piece at a time.
  async function upRequest(job, offset, size, total, bytes) {
    const going = new AbortController();
    let gaveUp = false;
    const patience = setTimeout(() => { gaveUp = true; going.abort(); }, upPatience(size));
    up.flying.add(going);
    const headers = upHeaders(job, offset, total);
    if (up.care) headers['X-Upload-Close'] = '1';
    try {
      const response = await fetch('/api/admin/upload?realm=' + job.realm, {
        method: 'POST', credentials: 'same-origin', headers, body: bytes, signal: going.signal });
      // The whole answer, or none: one that stops part-way is no answer at all.
      // One that is not Waterfall's own (a refusal from the web server itself)
      // still says no by its status.
      const text = await response.text();
      let data = {};
      try { data = JSON.parse(text) || {}; } catch { /* not Waterfall's own words */ }
      return { status: response.status, data };
    } catch {
      return { status: 0, data: {}, unanswered: gaveUp };
    } finally {
      clearTimeout(patience);
      up.flying.delete(going);
    }
  }

  // How long a piece may go unanswered: three quarters of a minute, and twice
  // as long again as it should take at the speed seen so far (or at 32 KB a
  // second, before any is known). A piece is sized to take about six seconds.
  function upPatience(size) {
    const each = Math.max(32 * 1024, up.rate / Math.max(1, up.active));
    return 45000 + 2000 * size / each;
  }

  // Everything the two displays need: how far, how fast, how long.
  function upState() {
    const sent = Math.min(up.bytes, up.sent);
    const now = performance.now();
    if (now - up.rateAt > 1000) {
      const rate = Math.max(0, (sent - up.rateSent) / ((now - up.rateAt) / 1000));
      up.rate = up.rate ? up.rate * 0.7 + rate * 0.3 : rate;
      up.rateAt = now;
      up.rateSent = sent;
    }
    const percent = up.bytes ? Math.min(100, Math.floor(sent / up.bytes * 100)) : 100;
    const left = up.rate > 1000 && up.bytes > sent ? (up.bytes - sent) / up.rate : 0;
    const finished = up.done + up.failed.length;
    const state = up.stopped ? 'stopped' : up.running ? (up.waiting ? 'waiting' : 'sending')
      : up.failed.length ? 'failed' : 'done';
    return { sent, percent, left, finished, state, count: up.jobs.length };
  }
  const fmtLeft = (seconds) => seconds < 90 ? `${Math.max(1, Math.round(seconds))} s left`
    : seconds < 5400 ? `${Math.round(seconds / 60)} min left` : `${(seconds / 3600).toFixed(1)} h left`;
  const fmtRate = (rate) => rate >= 1e6 ? (rate / 1e6).toFixed(1) + ' MB/s' : Math.max(1, Math.round(rate / 1e3)) + ' KB/s';

  function upDraw() {
    if (up.drawTimer) return;
    up.drawTimer = setTimeout(() => {
      up.drawTimer = 0;
      const u = upState();
      upPill(u);
      const view = up.view;
      if (!view || !view.isConnected) return;
      view.textContent = '';
      if (!u.count && !up.note) return;
      if (u.count) {
        const head = el('div', 'up-head');
        head.append(el('b', '', u.state === 'done' ? 'Done' : u.percent + '%'),
          el('span', 'muted small', { sending: 'Uploading. Keep this page open.',
            waiting: (up.why === 'offline' ? 'Waiting for the connection.'
              : up.why ? `Waterfall answered: ${up.why}.` : 'No answer came back.') + ' It tries again by itself.',
            stopped: 'Stopped.',
            failed: 'Finished, but not everything went.', done: 'It appears on the wall as it is converted.' }[u.state]));
        const meter = el('div', 'meter' + (u.state === 'sending' ? ' busy' : ''));
        meter.appendChild(el('i')).style.width = u.percent + '%';
        const bits = [`${u.finished.toLocaleString()} of ${plural(u.count, 'file')}`,
                      `${fmtBytes(u.sent)} of ${fmtBytes(Math.max(up.bytes, 1))}`];
        if (u.state === 'sending' && up.rate > 1000) bits.push(fmtRate(up.rate));
        if (u.state === 'sending' && u.left) bits.push(fmtLeft(u.left));
        view.append(head, meter, el('p', 'muted small', bits.join(' · ')), el('p', 'muted small', 'To ' + up.where));
        if (up.skipped) view.appendChild(el('p', 'muted small', `${plural(up.skipped, 'file')} already here, so not sent again.`));
        if (up.care && up.running) {
          view.appendChild(el('p', 'muted small', 'Answers were not coming back, so it is sending in smaller pieces'
            + (UP_CARE[up.care] < 512 * 1024 ? ` (${fmtBytes(UP_CARE[up.care])}).` : '.')));
        }
      }
      if (up.note) view.appendChild(el('p', 'muted small', up.note));
      for (const f of up.failed.slice(0, 12)) view.appendChild(el('p', 'fail', `${f.job.file.name}: ${f.why}`));
      if (up.failed.length > 12) view.appendChild(el('p', 'fail', `and ${up.failed.length - 12} more`));
      if (up.running && !up.stopped) {
        const stop = el('button', 'plain danger', 'Stop');
        stop.addEventListener('click', upStop);
        view.appendChild(stop);
      } else if (!up.running && up.failed.length) {
        const again = el('button', 'plain', 'Send the failed ones again');
        again.addEventListener('click', async () => {
          const jobs = up.failed.map((f) => Object.assign(f.job, { sent: 0 }));
          upReset();
          up.jobs = jobs;
          up.bytes = jobs.reduce((sum, job) => sum + job.file.size, 0);
          up.running = true;
          up.rateAt = performance.now();
          try { up.ticket = (await api('/api/admin/upload-ticket', {})).ticket; } catch { /* the old one may still do */ }
          upPump();
          upDraw();
        });
        view.appendChild(again);
      }
    }, 200);
  }
  // While anything is moving, redraw twice a second whether or not a piece
  // has finished: the speed and the time left are only true if they are fresh.
  setInterval(() => { if (up.running) upDraw(); }, 500);

  // The pill: the upload, in a few characters, on the wall. It is what is left
  // on screen when the sheet is closed, and it opens the sheet again.
  function upPill(u) {
    const pill = $('upload-pill');
    const show = u.count > 0 && !(u.state === 'done' && up.shownDone);
    pill.hidden = !show;
    if (!show) return;
    pill.className = 'upload-pill ' + u.state;
    $('upload-pill-fill').style.width = (u.state === 'done' ? 100 : u.percent) + '%';
    $('upload-pill-text').textContent = {
      sending: `Uploading ${u.percent}%` + (u.left ? ` · ${fmtLeft(u.left)}` : ''),
      waiting: `Upload waiting · ${u.percent}%`,
      stopped: `Upload stopped at ${u.percent}%`,
      failed: `Upload: ${plural(up.failed.length, 'file')} did not go`,
      done: `Upload done · ${plural(up.done, 'file')}` }[u.state];
  }
  $('upload-pill').addEventListener('click', () => {
    if (!up.running && !up.failed.length) { up.shownDone = true; upDraw(); return; }
    if (isAdmin()) openSettings();
  });
  window.addEventListener('beforeunload', (event) => { if (up.running) event.preventDefault(); });

  function uploadPanel() {
    const box = el('div', 'upload');
    let realm = up.realm && (up.realm !== 'hidden' || isAdmin()) ? up.realm : state.realm;
    // Where it goes: a wall, then a folder chosen from the ones there are, or a new one.
    const folder = el('select');
    folder.setAttribute('aria-label', 'Folder');
    const fresh = el('input');
    fresh.type = 'text';
    fresh.placeholder = 'Name of the new folder';
    fresh.autocapitalize = 'words';
    fresh.enterKeyHint = 'done';
    fresh.hidden = true;
    const offer = async () => {
      let sections = [];
      try {
        sections = (realm === state.realm ? state.sections : (await api('/api/sections?realm=' + realm)).sections);
      } catch { /* a new folder can still be named */ }
      folder.textContent = '';
      const add = (value, label) => { const o = el('option', '', label); o.value = value; folder.appendChild(o); return o; };
      add('', 'No folder (Unsorted)');
      for (const sec of sections) if (sec.path) add(sec.path, '  '.repeat(sec.depth) + sec.name);
      add('\n', 'New folder…');
      if (realm === up.realm && [...folder.options].some((o) => o.value === up.section)) folder.value = up.section;
      fresh.hidden = folder.value !== '\n';
    };
    folder.addEventListener('change', () => {
      fresh.hidden = folder.value !== '\n';
      if (!fresh.hidden) fresh.focus();
    });
    offer();
    box.appendChild(option('Wall', segmented([['visible', 'Visible'], ['hidden', 'Hidden']], realm,
      (picked) => { realm = picked; offer(); })));
    box.appendChild(option('Folder', folder));
    box.appendChild(fresh);
    const note = el('p', 'error');

    const picker = (label, whole) => {
      const input = el('input');
      input.type = 'file';
      input.multiple = true;
      input.hidden = true;
      if (whole) input.webkitdirectory = true;
      // Naming HEIC outright asks an iPhone for the photo as it was taken
      // instead of a JPEG made from it.
      else input.accept = 'image/*,video/*,.heic,.heif,.mov,.mp4,.m4v';
      const button = el('button', 'plain' + (whole ? '' : ' primary-ish'), label);
      button.type = 'button';
      button.addEventListener('click', () => {
        note.textContent = '';
        if (folder.value === '\n' && !fresh.value.trim()) {
          note.textContent = 'Type a name for the new folder first.';
          fresh.focus();
          return;
        }
        input.click();
      });
      input.addEventListener('change', () => {
        const typed = folder.value === '\n' ? fresh.value : folder.value;
        const where = typed.split('/').map((part) => part.trim()).filter(Boolean).join('/');
        up.realm = realm;
        up.section = where;
        upAdd([...input.files], realm, where);
        input.value = '';
      });
      box.append(input, button);
    };
    picker('Choose photos and videos', false);
    // A whole folder keeps its sub-folders as sub-sections. Phones cannot choose one.
    if ('webkitdirectory' in HTMLInputElement.prototype && window.matchMedia('(pointer: fine)').matches) {
      picker('Choose a whole folder', true);
    }
    box.appendChild(note);
    up.view = box.appendChild(el('div', 'upload-progress'));
    upDraw();
    return box;
  }

  function passwordForm() {
    const form = el('form');
    const admin = el('input');
    admin.type = 'password';
    admin.placeholder = 'New admin password';
    admin.autocomplete = 'new-password';
    const viewer = el('input');
    viewer.type = 'password';
    viewer.autocomplete = 'new-password';
    const note = el('p', 'muted small');
    const save = el('button', 'plain', 'Save');
    save.type = 'submit';
    const drop = el('button', 'plain', 'Remove the viewing password');
    drop.type = 'button';
    const describe = () => {
      viewer.placeholder = state.me.has_viewer ? 'New viewing password' : 'Viewing password (none)';
      note.style.color = '';
      note.textContent = state.me.has_viewer
        ? 'A viewing password is set: browsing asks for it first.'
        : 'No viewing password: anyone who can reach this address can browse the Visible wall.';
      drop.hidden = !state.me.has_viewer;
    };
    describe();
    form.append(admin, viewer, note, save, drop);
    const send = async (payload) => {
      try {
        const result = await api('/api/admin/passwords', payload);
        state.me.has_viewer = result.has_viewer;
        admin.value = viewer.value = '';
        describe();
        note.textContent = 'Saved. Other devices have been signed out. ' + note.textContent;
      } catch (error) {
        note.style.color = 'var(--danger)';
        note.textContent = error.message;
      }
    };
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      if (!admin.value && !viewer.value) { note.textContent = 'Type a new password first.'; return; }
      send({ admin: admin.value || null, viewer: viewer.value || null });
    });
    drop.addEventListener('click', () => send({ clear_viewer: true }));
    return form;
  }

  // Looked at again after a while away, and every few minutes besides: is this
  // still the code the server would hand out now, and is this still the admin?
  // A sign-in can run out on the server a little before the page's own clock
  // says so, and the Hidden wall must not stay up when it has.
  const freshen = () => refreshMe().then(() => {
    if (state.realm === 'hidden' && !isAdmin()) return setRealm('visible');
    reloadIfStale();
  }).catch(() => {});
  document.addEventListener('visibilitychange', () => { if (!document.hidden) freshen(); });
  setInterval(() => { if (!document.hidden) freshen(); }, 300000);

  start().catch((error) => {
    document.body.appendChild(el('p', 'center error', 'Waterfall could not start: ' + error.message));
  });
})();
