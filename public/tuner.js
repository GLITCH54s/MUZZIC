/* MUZZIC — chromatic / guitar / piano tuner (Web Audio, all client-side). */
(() => {
  const NOTES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
  const BLACK = new Set([1, 3, 6, 8, 10]);

  const TUNINGS = {
    'Standard E': [40, 45, 50, 55, 59, 64],
    'Drop D': [38, 45, 50, 55, 59, 64],
    'Half-step down': [39, 44, 49, 54, 58, 63],
    'Open G': [38, 43, 50, 55, 59, 62],
    'Open D': [38, 45, 50, 54, 57, 62],
    DADGAD: [38, 45, 50, 55, 57, 62],
  };

  const midiToFreq = (m) => 440 * Math.pow(2, (m - 69) / 12);
  const noteName = (m) => `${NOTES[m % 12]}${Math.floor(m / 12) - 1}`;

  const $ = (id) => document.getElementById(id);

  const S = {
    ctx: null,
    analyser: null,
    stream: null,
    buf: null,
    running: false,
    raf: 0,
    last: 0,
    history: [],
    hold: { freq: 0, until: 0 },
    octave: 3,
    stringRows: [],
    keyNodes: new Map(),
  };

  /* ── pitch detection (McLeod NSDF) ──────────────────────── */

  function detectPitch(buf, sr) {
    const n = buf.length;
    let rms = 0;
    for (let i = 0; i < n; i++) rms += buf[i] * buf[i];
    rms = Math.sqrt(rms / n);
    if (rms < 0.0035) return { freq: 0, rms };

    const maxLag = Math.min(Math.floor(sr / 55), n - 4);
    const minLag = Math.max(3, Math.floor(sr / 1600));
    const nsdf = new Float32Array(maxLag + 2);

    for (let lag = minLag; lag <= maxLag; lag++) {
      let ac = 0;
      let m = 0;
      const stop = n - lag;
      for (let i = 0; i < stop; i++) {
        const a = buf[i];
        const b = buf[i + lag];
        ac += a * b;
        m += a * a + b * b;
      }
      nsdf[lag] = m > 0 ? (2 * ac) / m : 0;
    }

    let peak = 0;
    for (let lag = minLag; lag <= maxLag; lag++) if (nsdf[lag] > peak) peak = nsdf[lag];
    if (peak < 0.42) return { freq: 0, rms };

    const threshold = 0.92 * peak;
    let chosen = -1;
    for (let lag = minLag + 1; lag < maxLag; lag++) {
      if (nsdf[lag] > nsdf[lag - 1] && nsdf[lag] >= nsdf[lag + 1] && nsdf[lag] >= threshold) {
        chosen = lag;
        break;
      }
    }
    if (chosen < 0) return { freq: 0, rms };

    const y1 = nsdf[chosen - 1];
    const y2 = nsdf[chosen];
    const y3 = nsdf[chosen + 1];
    const denom = y1 - 2 * y2 + y3;
    const shift = Math.abs(denom) > 1e-9 ? (0.5 * (y1 - y3)) / denom : 0;
    const period = chosen + Math.max(-0.5, Math.min(0.5, shift));
    if (!Number.isFinite(period) || period <= 0) return { freq: 0, rms };

    return { freq: sr / period, clarity: peak, rms };
  }

  function median(values) {
    const s = [...values].sort((a, b) => a - b);
    const mid = s.length >> 1;
    return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
  }

  /* ── audio graph ────────────────────────────────────────── */

  async function startMic() {
    try {
      $('micNote').textContent = 'asking for microphone…';
      S.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
          channelCount: 1,
        },
      });
      S.ctx = S.ctx || new (window.AudioContext || window.webkitAudioContext)();
      await S.ctx.resume();
      const src = S.ctx.createMediaStreamSource(S.stream);
      S.analyser = S.ctx.createAnalyser();
      S.analyser.fftSize = 4096;
      S.analyser.smoothingTimeConstant = 0;
      src.connect(S.analyser);
      S.buf = new Float32Array(S.analyser.fftSize);
      S.running = true;
      S.history = [];
      $('micBtn').textContent = 'Stop listening';
      $('micNote').textContent = `listening · ${(S.ctx.sampleRate / 1000).toFixed(1)} kHz`;
      tick();
    } catch (err) {
      $('micNote').textContent =
        err && err.name === 'NotAllowedError'
          ? 'microphone blocked — allow it in the browser site settings'
          : `microphone unavailable: ${err.message || err}`;
    }
  }

  function stopMic() {
    S.running = false;
    cancelAnimationFrame(S.raf);
    if (S.stream) S.stream.getTracks().forEach((t) => t.stop());
    S.stream = null;
    S.analyser = null;
    S.history = [];
    $('micBtn').textContent = 'Enable microphone';
    $('micNote').textContent = 'Audio stays on this machine.';
    paintReadout(null);
    S.stringRows.forEach((r) => r.node.classList.remove('live', 'tuned'));
    S.keyNodes.forEach((n) => n.classList.remove('is-detected', 'is-ok'));
  }

  function tick() {
    if (!S.running) return;
    S.raf = requestAnimationFrame(tick);
    const now = performance.now();
    if (now - S.last < 55) return;
    S.last = now;

    S.analyser.getFloatTimeDomainData(S.buf);
    const hit = detectPitch(S.buf, S.ctx.sampleRate);

    if (hit && !hit.freq) {
      quietHint(hit.rms);
    } else if (hit) {
      const midiFloat = 69 + 12 * Math.log2(hit.freq / 440);
      S.history.push(midiFloat);
      if (S.history.length > 7) S.history.shift();
      const smoothed = median(S.history);
      const freq = 440 * Math.pow(2, (smoothed - 69) / 12);
      S.hold = { midi: smoothed, freq, until: now + 420 };
      lastHitAt = now;
      clearHint();
      paintReadout({ midi: smoothed, freq });
    } else if (now > S.hold.until) {
      S.history = [];
      paintReadout(null);
    }
  }

  let quietAt = 0;
  let lastHitAt = 0;
  const listeningText = () => `listening · ${(S.ctx.sampleRate / 1000).toFixed(1)} kHz`;

  function quietHint(rms) {
    const now = performance.now();
    if (now - lastHitAt < 4000 || now - quietAt < 2000) return;
    quietAt = now;
    $('micNote').textContent = rms < 0.0008
      ? 'no signal — microphone may be muted'
      : 'signal is faint — play closer to the microphone';
  }

  function clearHint() {
    if ($('micNote').textContent !== listeningText()) $('micNote').textContent = listeningText();
  }

  /* ── readout ────────────────────────────────────────────── */

  function paintReadout(data) {
    const note = $('detNote');
    const needle = $('needle');
    const centsEl = $('centsVal');

    if (!data) {
      note.textContent = '—';
      note.className = 'det-note';
      $('detFreq').textContent = '— Hz';
      centsEl.textContent = '—';
      needle.style.left = '50%';
      needle.className = 'needle';
      S.stringRows.forEach((r) => {
        r.node.classList.remove('live', 'tuned');
        r.fill.style.left = '50%';
        r.cents.textContent = '—';
      });
      S.keyNodes.forEach((n) => n.classList.remove('is-detected', 'is-ok'));
      return;
    }

    const midi = data.midi;
    const nearest = Math.round(midi);
    const cents = (midi - nearest) * 100;
    const shown = Math.abs(cents) < 0.05 ? 0 : cents;
    const inTune = Math.abs(cents) <= 3;

    note.textContent = noteName(nearest);
    note.className = 'det-note ' + (inTune ? 'tuned' : Math.abs(cents) > 20 ? 'off' : '');
    $('detFreq').textContent = `${data.freq.toFixed(2)} Hz`;
    centsEl.textContent = `${shown >= 0 ? '+' : ''}${shown.toFixed(1)} cents`;
    needle.style.left = `${50 + Math.max(-50, Math.min(50, cents))}%`;
    needle.className = 'needle ' + (inTune ? 'tuned' : Math.abs(cents) > 20 ? 'off' : '');

    paintStrings(midi, cents);
    paintKeys(nearest, inTune);
  }

  function paintStrings(midi, cents) {
    let best = null;
    let bestDist = Infinity;
    S.stringRows.forEach((r) => {
      const d = Math.abs(midi - r.midi);
      if (d < bestDist) { bestDist = d; best = r; }
    });

    S.stringRows.forEach((r) => {
      const same = r === best && bestDist < 1.2;
      if (!same) {
        r.node.classList.remove('live', 'tuned');
        r.fill.style.left = '50%';
        r.cents.textContent = '—';
        return;
      }
      const c = (midi - r.midi) * 100;
      const cs = Math.abs(c) < 0.5 ? 0 : c;
      const ok = Math.abs(c) <= 3;
      r.node.classList.toggle('live', !ok);
      r.node.classList.toggle('tuned', ok);
      r.fill.style.left = `${50 + Math.max(-50, Math.min(50, c))}%`;
      r.cents.textContent = `${cs >= 0 ? '+' : ''}${cs.toFixed(0)}¢`;
    });
  }

  function paintKeys(nearest, inTune) {
    S.keyNodes.forEach((node, midi) => {
      const same = midi === nearest;
      node.classList.toggle('is-detected', same);
      node.classList.toggle('is-ok', same && inTune);
    });
  }

  /* ── reference tones ────────────────────────────────────── */

  function playTone(midi, seconds = 1.9) {
    if (!S.ctx) S.ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (S.ctx.state === 'suspended') S.ctx.resume();
    const t = S.ctx.currentTime;
    const freq = midiToFreq(midi);
    const osc = S.ctx.createOscillator();
    const gain = S.ctx.createGain();
    osc.type = 'sine';
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(0.22, t + 0.03);
    gain.gain.exponentialRampToValueAtTime(0.12, t + 0.35);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + seconds);
    osc.connect(gain).connect(S.ctx.destination);
    osc.start(t);
    osc.stop(t + seconds + 0.05);
  }

  /* ── guitar panel ───────────────────────────────────────── */

  function buildStrings() {
    const box = $('strings');
    box.textContent = '';
    S.stringRows = [];
    const midis = TUNINGS[$('tuning').value] || TUNINGS['Standard E'];

    midis.forEach((midi) => {
      const node = document.createElement('div');
      node.className = 'string';

      const name = document.createElement('span');
      name.className = 's-name';
      name.textContent = noteName(midi);

      const target = document.createElement('span');
      target.className = 's-target';
      target.textContent = `${midiToFreq(midi).toFixed(2)} Hz`;

      const bar = document.createElement('div');
      bar.className = 's-bar';
      const fill = document.createElement('span');
      fill.className = 's-fill';
      bar.appendChild(fill);

      const cents = document.createElement('span');
      cents.className = 's-cents';
      cents.textContent = '—';

      const play = document.createElement('button');
      play.className = 's-play';
      play.type = 'button';
      play.title = `Play ${noteName(midi)} reference tone`;
      play.textContent = '▶';
      play.addEventListener('click', () => playTone(midi));

      node.append(name, target, bar, cents, play);
      box.appendChild(node);
      S.stringRows.push({ node, midi, fill, cents });
    });
  }

  /* ── piano panel ────────────────────────────────────────── */

  function buildKeyboard() {
    const box = $('keyboard');
    box.textContent = '';
    S.keyNodes.clear();

    const white = [0, 2, 4, 5, 7, 9, 11];
    const base = 12 * (S.octave + 1);

    white.forEach((semi) => {
      const midi = base + semi;
      const key = document.createElement('button');
      key.type = 'button';
      key.className = 'key white';
      key.textContent = noteName(midi);
      key.addEventListener('click', () => playTone(midi));
      box.appendChild(key);
      S.keyNodes.set(midi, key);
    });

    white.forEach((semi, i) => {
      const sharp = semi + 1;
      if (!BLACK.has(sharp)) return;
      const midi = base + sharp;
      const key = document.createElement('button');
      key.type = 'button';
      key.className = 'key black';
      key.style.left = `${((i + 1) / 7) * 100}%`;
      key.title = noteName(midi);
      key.addEventListener('click', () => playTone(midi));
      box.appendChild(key);
      S.keyNodes.set(midi, key);
    });

    $('octLabel').textContent = S.octave;
  }

  /* ── wiring ─────────────────────────────────────────────── */

  function init() {
    const sel = $('tuning');
    Object.keys(TUNINGS).forEach((name) => {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      sel.appendChild(opt);
    });
    sel.addEventListener('change', buildStrings);

    $('micBtn').addEventListener('click', () => (S.running ? stopMic() : startMic()));

    $('octUp').addEventListener('click', () => {
      S.octave = Math.min(6, S.octave + 1);
      buildKeyboard();
    });
    $('octDown').addEventListener('click', () => {
      S.octave = Math.max(0, S.octave - 1);
      buildKeyboard();
    });

    buildStrings();
    buildKeyboard();
  }

  window.Tuner = {
    init,
    resume() { if (S.ctx && S.ctx.state === 'suspended') S.ctx.resume(); },
  };

  init();
})();
