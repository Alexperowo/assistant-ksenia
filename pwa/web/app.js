// Ксения на планшете: «Говорить» -> запись -> шлюз -> слух -> ядро; ответ звучит голосом Ксении здесь.
// Для слабовидящего: крупно, состояния — словом, формой значка, звуком и вибрацией; TalkBack — через aria.
'use strict';
(() => {
  const $ = (id) => document.getElementById(id);

  // ---------- настройки (только удобство этого планшета) ----------
  function load(key, def) {
    try { const v = localStorage.getItem('ksenia.' + key); return v === null ? def : JSON.parse(v); } catch (e) { return def; }
  }
  function save(key, val) { try { localStorage.setItem('ksenia.' + key, JSON.stringify(val)); } catch (e) { /* приватный режим */ } }
  const settings = { mode: load('mode', 'tap'), vibrate: load('vibrate', true), earcons: load('earcons', true), font: load('font', 26) };

  // ---------- звук: контекст, сигналы, вибрация ----------
  let actx = null;
  function audio() {
    if (!actx) actx = new (window.AudioContext || window.webkitAudioContext)();
    if (actx.state === 'suspended') actx.resume().catch(() => {});
    return actx;
  }
  const EARCON = { listen: [660, 880], done: [880, 660], think: [520], error: [330, 247], offline: [247, 196], online: [523, 659] };
  const VIBE = { listen: [60], done: [40], think: [30, 60, 30], speak: [120], error: [200, 80, 200], offline: [300, 100, 300], online: [60, 60, 60] };
  function signal(kind) {
    if (settings.vibrate && navigator.vibrate) navigator.vibrate(VIBE[kind] || 50);
    if (!settings.earcons || !actx) return 0;
    let t = actx.currentTime + 0.02;
    for (const f of EARCON[kind] || []) {
      const o = actx.createOscillator(), g = actx.createGain();
      o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.3, t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.12);
      o.connect(g).connect(actx.destination);
      o.start(t);
      o.stop(t + 0.13);
      t += 0.14;
    }
    return (t - actx.currentTime) * 1000;
  }
  // Связь с компьютером — единственное, что нельзя сказать голосом Ксении: тут говорит сам планшет.
  function sayLocally(text) {
    try {
      if (!window.speechSynthesis) return;
      const u = new SpeechSynthesisUtterance(text);
      u.lang = 'ru-RU';
      speechSynthesis.cancel();
      speechSynthesis.speak(u);
    } catch (e) { /* нет синтеза */ }
  }
  function announce(text, now) { // для TalkBack; во время записи и речи Ксении не используем — перебьёт
    const el = $(now ? 'announce-now' : 'announce');
    el.textContent = '';
    setTimeout(() => { el.textContent = text; }, 50);
  }

  // ---------- состояние ----------
  const LABEL = { idle: 'Готова', listening: 'Слушаю', thinking: 'Думаю', speaking: 'Говорю', offline: 'Нет связи с компьютером',
    pc_listening: 'Слушаю наушники у компьютера', pc_speaking: 'Говорю у компьютера', unheard: 'Не расслышала. Нажмите ещё раз' };
  const MARK = { pc_listening: 'listening', pc_speaking: 'speaking', unheard: 'idle' };
  let state = 'idle';
  function setState(st) {
    if (st === state) return;
    state = st;
    $('state-box').dataset.state = MARK[st] || st;
    $('state').textContent = LABEL[st];
    const busy = st === 'thinking' || st === 'speaking';
    $('stop').hidden = !busy && st !== 'listening';
    $('talk').setAttribute('aria-pressed', st === 'listening' ? 'true' : 'false');
    $('talk').textContent = st === 'listening' ? (settings.mode === 'hold' ? 'Отпустите, когда закончите' : 'Слушаю… нажмите, чтобы закончить')
      : (busy ? 'Перебить и говорить' : 'Говорить');
    $('talk').disabled = st === 'offline';
    if (st === 'thinking') signal('think');
    if (st === 'speaking') signal('speak');
    if (st === 'idle' && confirmFocusPending) focusConfirm();
  }

  // ---------- сервер ----------
  async function api(path, body) {
    const opts = { method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin' };
    if (body instanceof Blob) { opts.body = body; opts.headers = { 'Content-Type': 'audio/wav' }; } else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers = { 'Content-Type': 'application/json' }; }
    let r;
    try { r = await fetch(path, opts); } catch (e) { return { ok: false, status: 0, data: {} }; }
    let data = {};
    try { data = await r.json(); } catch (e) { /* пусто */ }
    if (r.status === 401) showLogin();
    return { ok: r.ok, status: r.status, data };
  }

  // ---------- воспроизведение ответа (PCM 16 бит, 44,1 кГц) ----------
  const player = {
    turn: null, rate: 44100, next: 0, sources: new Set(), carry: null, ended: false,
    start(turn, rate) { this.stop(); this.turn = turn; this.rate = rate || 44100; this.ended = false; audio(); },
    push(buf) {
      if (this.turn === null || !actx) return;
      let bytes = new Uint8Array(buf);
      if (this.carry) { const m = new Uint8Array(this.carry.length + bytes.length); m.set(this.carry); m.set(bytes, this.carry.length); bytes = m; this.carry = null; }
      if (bytes.length % 2) { this.carry = bytes.slice(-1); bytes = bytes.slice(0, -1); }
      const n = bytes.length / 2;
      if (!n) return;
      const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.length);
      const f = new Float32Array(n);
      for (let i = 0; i < n; i++) f[i] = dv.getInt16(2 * i, true) / 32768;
      const ab = actx.createBuffer(1, n, this.rate);
      ab.copyToChannel(f, 0);
      const src = actx.createBufferSource();
      src.buffer = ab;
      src.connect(actx.destination);
      const t = Math.max(actx.currentTime + 0.15, this.next); // небольшой запас против рывков Wi-Fi
      src.start(t);
      this.next = t + ab.duration;
      this.sources.add(src);
      src.onended = () => { this.sources.delete(src); this.check(); };
      if (state !== 'speaking') setState('speaking');
    },
    end(turn) { if (turn === this.turn) { this.ended = true; this.check(); } },
    check() { if (this.ended && this.sources.size === 0) { this.turn = null; setState(coreBusy ? 'thinking' : 'idle'); } },
    stop() {
      for (const s of this.sources) { try { s.stop(); } catch (e) { /* уже */ } }
      this.sources.clear();
      this.turn = null; this.carry = null; this.next = 0; this.ended = false;
    },
    active() { return this.turn !== null || this.sources.size > 0; },
  };

  // ---------- запись: AudioWorklet -> 16 кГц -> WAV ----------
  // MediaRecorder в Chrome пишет только WebM/MP4, а слух (voice-in) читает WAV: собираем WAV сами.
  const rec = {
    stream: null, node: null, src: null, frames: [], active: false, started: false, silentMs: 0, noise: null,
    win: [], t0: 0, workletReady: false, mode: 'tap',
    async start(mode) {
      const ctx = audio();
      this.mode = mode;
      try {
        this.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
      } catch (e) {
        setState('idle');
        $('state').textContent = 'Нет доступа к микрофону. Разрешите его в настройках сайта.';
        signal('error');
        announce('Нет доступа к микрофону', true);
        return false;
      }
      if (!this.workletReady) { await ctx.audioWorklet.addModule('/recorder-worklet.js'); this.workletReady = true; }
      this.src = ctx.createMediaStreamSource(this.stream);
      this.node = new AudioWorkletNode(ctx, 'recorder');
      const mute = ctx.createGain();
      mute.gain.value = 0; // узел должен быть подключён к выходу, чтобы работать, но себя мы не слышим
      this.src.connect(this.node).connect(mute).connect(ctx.destination);
      this.frames = []; this.started = mode === 'hold'; this.silentMs = 0; this.noise = null; this.win = []; this.t0 = performance.now();
      this.active = true;
      this.node.port.onmessage = (e) => this.frame(e.data);
      api('/api/listening', { on: true });
      return true;
    },
    frame(pcm) {
      if (!this.active) return;
      this.frames.push(pcm);
      let sum = 0;
      for (let i = 0; i < pcm.length; i++) { const v = pcm[i] / 32768; sum += v * v; }
      const rms = Math.sqrt(sum / pcm.length);
      const elapsed = this.frames.length * 20;
      if (this.mode === 'hold') { if (elapsed > 60000) this.finish(); return; }
      if (elapsed < 300) return; // хвост сигнала «слушаю» ещё звучит
      if (this.noise === null) this.noise = rms;
      if (!this.started) {
        this.noise = 0.95 * this.noise + 0.05 * rms;
        const thr = Math.max(this.noise * 3, 0.01);
        this.win.push(rms > thr);
        if (this.win.length > 15) this.win.shift();
        if (this.win.filter(Boolean).length * 20 >= 120) this.started = true;
        else if (elapsed > 8000) { this.cancel(); setState('unheard'); signal('error'); }
      } else {
        const thr = Math.max(this.noise * 2, 0.007);
        this.silentMs = rms < thr ? this.silentMs + 20 : 0;
        if (this.silentMs >= 800 || elapsed > 30000) this.finish();
      }
    },
    release() {
      this.active = false;
      try { this.src && this.src.disconnect(); this.node && this.node.disconnect(); } catch (e) { /* уже */ }
      if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
      this.stream = null;
      api('/api/listening', { on: false });
    },
    cancel() { if (this.active) this.release(); },
    async finish() {
      if (!this.active) return;
      this.release();
      signal('done');
      const total = this.frames.reduce((a, f) => a + f.length, 0);
      const keepTail = this.mode === 'tap' ? Math.max(0, this.silentMs - 200) * 16 : 0;
      const n = Math.max(0, total - keepTail);
      if (!this.started || n < 16000 * 0.3) { setState('unheard'); return; }
      const pcm = new Int16Array(n);
      let off = 0;
      for (const f of this.frames) { const take = Math.min(f.length, n - off); if (take <= 0) break; pcm.set(f.subarray(0, take), off); off += take; }
      setState('thinking');
      const r = await api('/api/utterance', wav(pcm, 16000));
      if (!r.ok) { setState('idle'); $('state').textContent = r.data.error || 'Не получилось отправить'; signal('error'); announce($('state').textContent, true); return; }
      if (!r.data.text) { setState('unheard'); signal('error'); return; }
      addUser(r.data.text);
    },
  };

  function wav(pcm, rate) {
    const buf = new ArrayBuffer(44 + pcm.length * 2);
    const v = new DataView(buf);
    const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    str(0, 'RIFF'); v.setUint32(4, 36 + pcm.length * 2, true); str(8, 'WAVE'); str(12, 'fmt ');
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true);
    v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); str(36, 'data');
    v.setUint32(40, pcm.length * 2, true);
    new Int16Array(buf, 44).set(pcm);
    return new Blob([buf], { type: 'audio/wav' });
  }

  async function startTalking() {
    audio();
    if (player.active() || state === 'thinking' || state === 'speaking') { // перебить Ксению
      player.stop();
      api('/api/stop', {});
    }
    const wait = signal('listen'); // говорить после сигнала, как у компьютера
    setState('listening');
    await new Promise((r) => setTimeout(r, wait));
    if (!(await rec.start(settings.mode))) return;
  }

  // ---------- разговор на экране ----------
  let lastUser = { text: '', t: 0 };
  let replyItem = null;
  function addLine(who, text) {
    const li = document.createElement('li');
    const w = document.createElement('span');
    w.className = 'who';
    w.textContent = who;
    li.append(w, document.createTextNode(text));
    const list = $('history');
    list.prepend(li); // новые сверху: последнее видно сразу, без прокрутки (и при экранной лупе)
    while (list.children.length > 100) list.lastChild.remove();
    return li;
  }
  function addUser(text) {
    if (text === lastUser.text && Date.now() - lastUser.t < 8000) return; // «услышала» от шлюза и «реплика» от ядра
    lastUser = { text, t: Date.now() };
    $('last-user').textContent = text;
    $('last-ksenia').textContent = '…';
    addLine('Вы', text);
    replyItem = null;
  }
  function addKsenia(text) {
    if (!replyItem) {
      $('last-ksenia').textContent = text;
      replyItem = addLine('Ксения', text);
    } else {
      $('last-ksenia').textContent += ' ' + text;
      replyItem.lastChild.textContent += ' ' + text;
    }
  }

  // ---------- подтверждение ----------
  let confirmFocusPending = false;
  function showConfirm(q) {
    $('confirm-q').textContent = q;
    $('confirm').hidden = false;
    confirmFocusPending = true; // фокус (и чтение TalkBack) — когда Ксения договорит, чтобы не говорить хором
    if (!player.active() && state !== 'listening') focusConfirm();
    signal('listen');
  }
  function focusConfirm() { confirmFocusPending = false; if (!$('confirm').hidden) $('confirm-q').focus(); }
  function hideConfirm() { $('confirm').hidden = true; confirmFocusPending = false; }
  async function answer(text) {
    audio();
    hideConfirm();
    player.stop();
    addUser(text === 'да' ? 'Да' : 'Нет');
    setState('thinking');
    const r = await api('/api/text', { text });
    if (!r.ok) { setState('idle'); signal('error'); }
  }

  // ---------- связь ----------
  let ws = null, wsUp = false, coreUp = false, coreBusy = false, offlineTimer = null, wasOffline = false, wsFails = 0;
  function linkChanged() {
    const up = wsUp && coreUp;
    clearTimeout(offlineTimer);
    if (!up) {
      offlineTimer = setTimeout(() => { // короткие обрывы Wi-Fi не дёргают
        if (wsUp && coreUp) return;
        rec.cancel(); player.stop();
        setState('offline'); signal('offline');
        if (!wasOffline) sayLocally('Нет связи с компьютером');
        wasOffline = true;
        $('nolink').hidden = true;
      }, 2500);
    } else if (wasOffline) {
      wasOffline = false;
      setState('idle'); signal('online'); sayLocally('Связь есть');
    }
  }
  function connect() {
    ws = new WebSocket(`wss://${location.host}/api/ws`);
    ws.binaryType = 'arraybuffer';
    let ping = null;
    ws.onopen = () => { wsUp = true; wsFails = 0; linkChanged(); ping = setInterval(() => { try { ws.send('ping'); } catch (e) { /* закрыт */ } }, 20000); };
    ws.onmessage = (e) => { if (typeof e.data === 'string') { try { onEvent(JSON.parse(e.data)); } catch (err) { /* мусор */ } } else player.push(e.data); };
    ws.onclose = async () => {
      clearInterval(ping);
      wsUp = false;
      linkChanged();
      wsFails += 1;
      if (wsFails % 3 === 0) { const s = await api('/api/session'); if (s.ok && !s.data.authorized) return showLogin(); }
      setTimeout(connect, Math.min(1000 * wsFails, 8000));
    };
  }
  function onEvent(ev) {
    switch (ev.type) {
      case 'link': coreUp = !!ev.core; linkChanged(); break;
      case 'hello': coreBusy = !!ev.busy; if (ev.confirm) showConfirm(ev.confirm.question); break;
      case 'state':
        if (ev.state === 'thinking') { coreBusy = true; if (!player.active() && state !== 'listening') setState('thinking'); } else if (ev.state === 'idle') { coreBusy = false; if (!player.active() && state !== 'listening' && state !== 'offline') setState('idle'); } else if (ev.where === 'pc' && state !== 'listening' && !player.active()) setState(ev.state === 'listening' ? 'pc_listening' : 'pc_speaking');
        break;
      case 'heard': if (ev.text) addUser(ev.text); break;
      case 'user': addUser(ev.text); break;
      case 'say': addKsenia(ev.text); break;
      case 'audio_start': player.start(ev.turn, ev.rate); break;
      case 'audio_end': player.end(ev.turn); break;
      case 'audio_stop': player.stop(); if (!coreBusy) setState('idle'); break;
      case 'confirm': showConfirm(ev.question); break;
      case 'confirm_clear': hideConfirm(); break;
      case 'error': $('state').textContent = ev.text; signal('error'); announce(ev.text, true); break;
      default: break;
    }
  }

  // ---------- экран не гаснет ----------
  let wakeLock = null;
  async function keepAwake() {
    try { if ('wakeLock' in navigator && document.visibilityState === 'visible' && !wakeLock) { wakeLock = await navigator.wakeLock.request('screen'); wakeLock.addEventListener('release', () => { wakeLock = null; }); } } catch (e) { /* не дали */ }
  }
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') keepAwake(); });

  // ---------- вход ----------
  function showLogin() {
    $('app').hidden = true; $('login').hidden = false;
    setTimeout(() => $('pin').focus(), 100);
  }
  async function showApp() {
    $('login').hidden = true; $('app').hidden = false;
    keepAwake();
    if (!ws) connect();
    setTimeout(() => $('talk').focus(), 100);
  }
  async function login() {
    audio();
    const r = await api('/api/login', { pin: $('pin').value });
    if (r.ok) { $('login-error').textContent = ''; $('pin').value = ''; showApp(); return; }
    $('login-error').textContent = r.data.error || 'Не получилось войти';
  }

  // ---------- кнопки ----------
  function bind() {
    const talk = $('talk');
    talk.addEventListener('click', () => {
      if (settings.mode === 'hold' && holdUsed) { holdUsed = false; return; }
      if (rec.active) rec.finish(); else startTalking();
    });
    let holdUsed = false;
    talk.addEventListener('pointerdown', (e) => { if (settings.mode !== 'hold' || rec.active) return; holdUsed = true; talk.setPointerCapture(e.pointerId); startTalking(); });
    const up = () => { if (settings.mode === 'hold' && rec.active) rec.finish(); };
    talk.addEventListener('pointerup', up);
    talk.addEventListener('pointercancel', up);
    talk.addEventListener('contextmenu', (e) => e.preventDefault());
    $('stop').addEventListener('click', () => { rec.cancel(); player.stop(); api('/api/stop', {}); setState('idle'); });
    $('yes').addEventListener('click', () => answer('да'));
    $('no').addEventListener('click', () => answer('нет'));
    $('login-button').addEventListener('click', login);
    $('pin').addEventListener('keydown', (e) => { if (e.key === 'Enter') login(); });
    $('speak-pin').addEventListener('click', async () => {
      const r = await api('/api/pin/speak', {});
      $('login-error').textContent = r.ok ? 'Слушайте компьютер: Ксения называет код.' : (r.data.error || 'Компьютер не ответил');
    });
    $('logout').addEventListener('click', async () => { await api('/api/logout', {}); location.reload(); });
    for (const r of document.querySelectorAll('input[name="mode"]')) {
      r.checked = r.value === settings.mode;
      r.addEventListener('change', () => {
        settings.mode = r.value; save('mode', r.value);
        $('talk-hint').textContent = r.value === 'hold' ? 'Держите кнопку, пока говорите, и отпустите.' : 'Нажмите и говорите после сигнала. Я сама пойму, когда вы закончили.';
      });
    }
    $('vibrate').checked = settings.vibrate;
    $('vibrate').addEventListener('change', (e) => { settings.vibrate = e.target.checked; save('vibrate', settings.vibrate); });
    $('earcons').checked = settings.earcons;
    $('earcons').addEventListener('change', (e) => { settings.earcons = e.target.checked; save('earcons', settings.earcons); });
    const font = (d) => { settings.font = Math.max(20, Math.min(48, settings.font + d)); save('font', settings.font); document.documentElement.style.setProperty('--font', settings.font + 'px'); };
    $('font-down').addEventListener('click', () => font(-3));
    $('font-up').addEventListener('click', () => font(3));
    font(0);
    document.addEventListener('pointerdown', audio, { once: true }); // звук на Android — только после касания
  }

  async function check() {
    const s = await api('/api/session');
    if (s.status === 0) { $('nolink').hidden = false; setTimeout(check, 5000); return; }
    $('nolink').hidden = true;
    if (s.data.authorized) showApp(); else showLogin();
  }
  bind();
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
  check();
})();
