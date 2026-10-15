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
    items: [], more: true, loading: false, generation: 0,
    cols: [], heights: [],
    viewing: false, current: -1, wantSound: false,
  };
  function newSeed() { return 1 + Math.floor(Math.random() * 2147483000); }
  const isAdmin = () => !!(state.me && state.me.admin);

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
    if (!state.me.admin) state.realm = 'visible';
  }
  const murl = (item, name) => `/m/${state.pipeline}/${item.key}/${name}`;

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
  function noteActivity() {
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
      if (!state.me || !state.me.role) showGate(); else await setRealm('visible');
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
    const data = await api('/api/sections?realm=' + state.realm);
    state.sections = data.sections;
    state.totals = data.totals;
    state.sectionById = new Map(data.sections.map((s) => [s.id, s]));
    state.tags = data.tags || [];
    if (state.section !== null && !state.sectionById.has(state.section)) state.section = null;
    if (state.tag !== null && !state.tags.some((t) => t.tag.toLowerCase() === state.tag.toLowerCase())) state.tag = null;
  }

  // Switching wall is the one place the two are told apart, so it resets
  // everything: no item from one can linger on screen in the other.
  async function setRealm(realm) {
    unwindHistory();
    closeViewer(true);
    closeSheet();
    state.realm = realm === 'hidden' && isAdmin() ? 'hidden' : 'visible';
    state.section = null;
    state.tag = null;
    try { await loadSections(); } catch { /* the wall will say what is wrong */ }
    resetWall();
  }

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

  function resetWall() {
    stopAllPreviews();
    inView.clear();
    previewWatcher.disconnect();
    state.generation += 1;
    state.items = [];
    state.more = true;
    state.loading = false;
    buildColumns();
    const hidden = state.realm === 'hidden';
    $('bar').classList.toggle('is-hidden', hidden);
    $('realm-tag').hidden = !hidden;
    const current = state.section === null ? null : state.sectionById.get(state.section);
    const tagged = state.tag === null ? null
      : state.tags.find((t) => t.tag.toLowerCase() === state.tag.toLowerCase());
    $('title-text').textContent = state.tag !== null ? '#' + state.tag : current ? current.name : 'Everything';
    const totals = tagged ? [tagged.photos, tagged.videos]
      : current ? [current.total_photos, current.total_videos]
      : [state.totals.photos, state.totals.videos];
    const shown = state.kind === 'photo' ? totals[0] : state.kind === 'video' ? totals[1] : totals[0] + totals[1];
    $('wall-count').textContent = shown ? plural(shown, 'item') : '';
    $('wall-status').textContent = '';
    window.scrollTo(0, 0);
    loadMore();
  }

  async function loadMore() {
    if (state.loading || !state.more) return;
    state.loading = true;
    const generation = state.generation;
    const query = new URLSearchParams({
      realm: state.realm, order: prefs.order, seed: state.seed,
      offset: state.items.length, limit: 40, kind: state.kind,
    });
    if (state.section !== null) query.set('section', state.section);
    if (state.tag !== null) query.set('tag', state.tag);
    try {
      const data = await api('/api/feed?' + query);
      if (generation !== state.generation) return;      // the wall was reset meanwhile
      const base = state.items.length;
      state.items.push(...data.items);
      state.more = data.more;
      data.items.forEach((item, i) => addTile(item, base + i));
      if (!state.items.length) {
        $('wall-status').textContent = isAdmin()
          ? 'Nothing here yet. Add photos and videos from the ··· menu.'
          : 'Nothing here yet.';
      } else if (!state.more) {
        $('wall-status').textContent = 'That is everything.';
      }
    } catch (error) {
      if (generation === state.generation && error.message !== 'Signed out.') {
        $('wall-status').textContent = error.message;
      }
    } finally {
      if (generation === state.generation) state.loading = false;
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
    img.src = murl(item, 't.webp');
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
      video.preload = 'metadata';
      video.src = videoSource(item);
      slide.dataset.size = 'auto';
      const spinner = el('div', 'spinner');
      spinner.hidden = true;
      video.addEventListener('waiting', () => { spinner.hidden = false; });
      video.addEventListener('playing', () => { spinner.hidden = true; slide.classList.remove('is-paused'); });
      video.addEventListener('pause', () => { if (index === state.current) slide.classList.add('is-paused'); });
      video.addEventListener('timeupdate', () => { if (index === state.current) showTime(video); });
      video.addEventListener('loadedmetadata', () => { if (index === state.current) showTime(video); });
      const paused = el('div', 'paused');
      paused.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>';
      slide.append(video, spinner, paused);
    } else {
      // The thumbnail is almost certainly cached from the wall, so there is
      // something real on screen at once; the sharp copy fades in over it.
      const low = el('img');
      low.alt = '';
      low.draggable = false;
      low.src = murl(item, 't.webp');
      const sharp = el('img', 'sharp');
      sharp.alt = '';
      sharp.draggable = false;
      sharp.decoding = 'async';
      sharp.addEventListener('load', () => sharp.classList.add('in'), { once: true });
      sharp.src = murl(item, prefs.photoHigh ? 'l.webp' : 'm.webp');
      slide.dataset.size = prefs.photoHigh ? 'l' : 'm';
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
      new Image().src = murl(item, item.k === 'v' || !prefs.photoHigh ? 'm.webp' : 'l.webp');
    }
    // The very next video also starts buffering, so it plays the moment it
    // is reached. Only one, so a quick flick past it wastes little.
    const video = mainVideo(slides.get(state.current + 1));
    if (video && prefs.ahead > 0 && video.preload !== 'auto') {
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

  function playLive(slide) {
    const item = state.items[Number(slide.dataset.index)];
    if (!item || !item.lv) return;
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
    zoom.slide.querySelector('img.sharp').src = murl(item, 'l.webp');
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
    const by = { ArrowDown: 1, ArrowRight: 1, ' ': 1, PageDown: 1, ArrowUp: -1, ArrowLeft: -1, PageUp: -1 }[event.key];
    if (by) { event.preventDefault(); step(by); }
    else if (event.key === 'Escape') history.back();
    else if (event.key === 'i') $('viewer-info').click();
    else if (event.key === 'm') $('viewer-sound').click();
  });

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

      const megapixels = d.kind === 'photo' ? ` (${(d.width * d.height / 1e6).toFixed(0)} MP)` : '';
      const kind = [meta.format, meta.codec, d.hdr ? 'HDR original' : null,
                    meta.fps ? meta.fps + ' fps' : null].filter(Boolean).join(' · ');
      let showing = null;
      if (d.kind === 'video' && video && video.videoHeight) {
        showing = Math.min(video.videoWidth, video.videoHeight) + 'p';
      } else if (d.kind === 'photo' && slide) {
        showing = slide.dataset.size === 'l' ? 'Large copy' : 'Screen copy';
      }
      body.appendChild(rows([
        ['Type', (d.kind === 'video' ? 'Video' : d.live ? 'Live Photo' : 'Photo') + (kind ? ' · ' + kind : '')],
        ['Size', `${d.width} × ${d.height}${megapixels}`],
        ['Length', d.kind === 'video' ? fmtClock(d.duration) : null],
        ['Showing', showing],
        ['Original', fmtBytes(d.size) + (d.trashed ? ' (not kept: small copies only)' : '')],
        ['Converted', isAdmin() ? d.made_by : null],
        ['Lens', meta.lens],
        ['Exposure', meta.exposure],
      ]));

      body.appendChild(el('h3', '', 'Copies on the server'));
      if (d.kind === 'video') {
        body.appendChild(rows((d.rungs || []).map((r) =>
          [r.name + 'p', `${fmtBytes(r.bytes)} · ${(r.avg / 1e6).toFixed(1)} Mbit/s`])
          .concat([['Wall preview', d.sizes && d.sizes.p ? fmtBytes(d.sizes.p) : null]])));
      } else {
        const s = d.sizes || {};
        body.appendChild(rows([['Thumbnail', s.t && fmtBytes(s.t)], ['Screen', s.m && fmtBytes(s.m)],
                               ['Large', s.l && fmtBytes(s.l)],
                               ['Live clip', s.live && `${fmtBytes(s.live)} · hold the photo, or tap LIVE, to play it`]]));
        if (slide && slide.dataset.size !== 'l') {
          const best = el('button', 'plain', 'Show the large copy');
          best.addEventListener('click', () => {
            const sharp = slide.querySelector('img.sharp');
            if (sharp) { sharp.src = murl(item, 'l.webp'); slide.dataset.size = 'l'; }
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
        if (cover) thumb.style.backgroundImage = `url(${murl(cover, 't.webp')})`;
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
  // still the code the server would hand out now?
  const freshen = () => refreshMe().then(reloadIfStale).catch(() => {});
  document.addEventListener('visibilitychange', () => { if (!document.hidden) freshen(); });
  setInterval(() => { if (!document.hidden) freshen(); }, 300000);

  start().catch((error) => {
    document.body.appendChild(el('p', 'center error', 'Waterfall could not start: ' + error.message));
  });
})();
