/* MUZZIC — analysis UI. */
(() => {
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  const state = { busy: false, timer: null, phase: 0 };

  /* ── tabs ───────────────────────────────────────────────── */

  const tabs = Array.from(document.querySelectorAll('.tab'));

  function activate(tab, focus) {
    tabs.forEach((t) => {
      const on = t === tab;
      t.classList.toggle('is-active', on);
      t.setAttribute('aria-selected', String(on));
      t.tabIndex = on ? 0 : -1;
    });
    document.querySelectorAll('.panel').forEach((p) => {
      p.classList.toggle('is-active', p.id === `panel-${tab.dataset.tab}`);
    });
    if (tab.dataset.tab === 'tune' && window.Tuner) window.Tuner.resume();
    if (focus) tab.focus();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  tabs.forEach((tab, i) => {
    tab.addEventListener('click', () => activate(tab, false));
    tab.addEventListener('keydown', (e) => {
      const dir = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
      if (!dir) return;
      e.preventDefault();
      activate(tabs[(i + dir + tabs.length) % tabs.length], true);
    });
  });

    // When the page is served by server.py (any host or port) a relative URL
  // reaches the API. Opened straight off disk (file://) the browser resolves
  // '/api/...' against nothing, so pin the default loopback origin instead;
  // the server answers CORS for exactly this case.
  const API = location.protocol === 'file:' ? 'http://127.0.0.1:8765' : '';

const engine = (mode, text) => {
    const n = $('engineStatus');
    n.className = `engine ${mode || ''}`.trim();
    n.textContent = '';
    n.appendChild(document.createElement('i'));
    n.appendChild(document.createTextNode(text || mode || 'idle'));
  };

  // Visible connectivity. A dropped file used to fail with the raw
  // "Failed to fetch" and no way to tell the server was simply not there.
  // Poll so the panel recovers by itself the moment the server is back.
  async function checkHealth() {
    try {
      const r = await fetch(`${API}/api/health`, { cache: 'no-store' });
      if (!r.ok) throw new Error(String(r.status));
      const h = await r.json();
      $('footEngine').textContent = h.ffmpeg ? 'decoder: ffmpeg' : 'decoder: built-in';
      if (state.offline) {
        state.offline = false;
        if (state.offlineCard) { $('errorCard').hidden = true; $('errorText').textContent = ''; }
        engine('idle', 'idle');
      }
    } catch {
      if (!state.offline) {
        state.offline = true;
        state.offlineCard = true;
        engine('err', 'offline');
        $('errorText').textContent = `The analysis server (${API || location.origin}) is not reachable from this page (${location.href}). Start it with server.py, or open the app at ${API || location.origin} — this message clears itself when it is back.`;
        $('errorCard').hidden = false;
        $('retryBtn').hidden = true;
      }
    }
  }
  checkHealth();
  setInterval(checkHealth, 5000);

  /* ── upload ─────────────────────────────────────────────── */

  const dz = $('dropzone');
  const input = $('fileInput');

  $('pickFile').addEventListener('click', () => input.click());
  input.addEventListener('change', () => input.files[0] && start(input.files[0]));
  $('resetBtn').addEventListener('click', () => {
    $('results').hidden = true;
    $('errorCard').hidden = true;
    dz.hidden = false;
    input.value = '';
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });

  ['dragenter', 'dragover'].forEach((ev) =>
    document.addEventListener(ev, (e) => {
      if (!e.dataTransfer?.types?.includes('Files')) return;
      e.preventDefault();
      dz.classList.add('dragging');
    }));

  ['dragleave', 'drop'].forEach((ev) =>
    document.addEventListener(ev, (e) => {
      if (ev === 'dragleave' && e.relatedTarget) return;
      e.preventDefault();
      dz.classList.remove('dragging');
    }));

  document.addEventListener('drop', (e) => {
    const file = e.dataTransfer?.files?.[0];
    if (file) start(file);
  });

  async function start(file) {
    if (state.busy) return;
    if (!/audio|video|octet/.test(file.type) && !/\.(wav|mp3|flac|ogg|oga|aif|aiff|m4a|opus|webm)$/i.test(file.name)) {
      return fail('That does not look like an audio file.');
    }
    if (file.size > 80 * 1024 * 1024) return fail('File is over 80 MB. Try a shorter excerpt.');

    state.lastFile = file;
    state.busy = true;
    engine('busy', 'analysing');
    dz.hidden = true;
    $('results').hidden = true;
    $('errorCard').hidden = true;
    $('retryBtn').hidden = true;
    $('loading').hidden = false;
    beginPhases();

    try {
      const res = await fetch(`${API}/api/analyze`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/octet-stream',
          'X-File-Name': encodeURIComponent(file.name),
          'X-Seconds': '90',
        },
        body: file,
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || `server said ${res.status}`);
      render(data);
      endPhases();
      $('loading').hidden = true;
      $('results').hidden = false;
      engine('ready', `done in ${data.elapsed}s`);
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } catch (err) {
      endPhases();
      $('loading').hidden = true;
      if (err instanceof TypeError) {
        // The request never reached the server: it was down or restarting.
        // Keep the file, offer a retry, and let the health poll recover the
        // panel by itself once the server answers again.
        state.offline = true;
        state.offlineCard = true;
        fail('The analysis server was not reachable — it may have just restarted. Press Try again.');
        $('retryBtn').hidden = false;
      } else {
        dz.hidden = false;
        fail(err.message);
      }
      engine('err', 'failed');
    } finally {
      state.busy = false;
    }
  }

  function fail(msg) {
    $('errorText').textContent = msg;
    $('errorCard').hidden = false;
    engine('err', 'failed');
  }

  $('retryBtn').addEventListener('click', () => {
    if (state.lastFile) start(state.lastFile);
  });

  /* ── microphone recording ───────────────────────────────── */

  const REC_MAX = 90;
  const recPanel = $('recorder');
  let rec = null;
  let recPending = false;

  const clock = (s) =>
    `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;

  $('recordBtn').addEventListener('click', () => beginRecording());
  $('recStop').addEventListener('click', () => stopRecording(true));
  $('recCancel').addEventListener('click', () => stopRecording(false));

  async function beginRecording() {
    if (rec || recPending || state.busy) return;
    $('errorCard').hidden = true;

    const AC = window.AudioContext || window.webkitAudioContext;
    if (!navigator.mediaDevices?.getUserMedia || !AC) {
      return fail('This browser cannot record audio. Try a current Chrome, Edge, Firefox or Safari.');
    }
    if (!window.isSecureContext) {
      return fail('Microphone recording needs a secure context — open the app at http://127.0.0.1:8765 or over https.');
    }

    recPending = true;
    const ctx = new AC();
    ctx.resume().catch(() => {});

    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
          channelCount: 1,
        },
      });
    } catch (err) {
      recPending = false;
      ctx.close().catch(() => {});
      const blocked = err && (err.name === 'NotAllowedError' || err.name === 'SecurityError');
      return fail(blocked
        ? 'Microphone blocked — allow it in the browser site settings, then press Record again.'
        : `Microphone unavailable: ${(err && err.message) || err}`);
    }

    const src = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 2048;
    src.connect(analyser);

    // ScriptProcessor is deprecated but still the simplest capture path that
    // works in every current browser without a separate worklet module.
    // A silent branch keeps the processor running without monitoring the mic
    // back through the speakers.
    const proc = ctx.createScriptProcessor(4096, 1, 1);
    const mute = ctx.createGain();
    mute.gain.value = 0;
    src.connect(proc);
    proc.connect(mute);
    mute.connect(ctx.destination);

    rec = {
      ctx, stream, src, proc, analyser,
      chunks: [], frames: 0, done: false,
      buf: new Float32Array(analyser.fftSize),
      raf: 0, ticker: 0,
    };
    recPending = false;

    proc.onaudioprocess = (e) => {
      if (!rec || rec.done) return;
      rec.chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
      rec.frames += e.inputBuffer.length;
    };

    $('dropzone').hidden = true;
    recPanel.hidden = false;
    $('recHint').textContent = 'Sing, hum or play into the microphone. Up to 90 seconds.';
    $('recTime').textContent = '0:00';
    $('recLevel').style.width = '0%';
    $('recStop').focus();

    rec.raf = requestAnimationFrame(paintLevel);
    rec.ticker = setInterval(() => {
      if (!rec || rec.done) return;
      const elapsed = rec.frames / rec.ctx.sampleRate;
      $('recTime').textContent = clock(elapsed);
      if (elapsed >= REC_MAX) stopRecording(true);
    }, 200);
  }

  function paintLevel() {
    if (!rec || rec.done) return;
    rec.raf = requestAnimationFrame(paintLevel);
    rec.analyser.getFloatTimeDomainData(rec.buf);
    let sum = 0;
    for (let i = 0; i < rec.buf.length; i++) sum += rec.buf[i] * rec.buf[i];
    const rms = Math.sqrt(sum / rec.buf.length);
    const db = 20 * Math.log10(Math.max(rms, 1e-5));
    const level = Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
    $('recLevel').style.width = `${level.toFixed(1)}%`;
  }

  function stopRecording(analyse) {
    if (!rec || rec.done) return;
    const { ctx, stream, src, proc, chunks, frames } = rec;
    const sampleRate = ctx.sampleRate;
    rec.done = true;
    cancelAnimationFrame(rec.raf);
    clearInterval(rec.ticker);
    proc.onaudioprocess = null;
    try { src.disconnect(); proc.disconnect(); } catch { /* already torn down */ }
    stream.getTracks().forEach((t) => t.stop());
    ctx.close().catch(() => {});
    rec = null;

    recPanel.hidden = true;
    $('recLevel').style.width = '0%';
    $('dropzone').hidden = false;

    if (!analyse) return;

    const seconds = frames / sampleRate;
    if (seconds < 1) {
      return fail('That take was under a second — press Record and play a little longer.');
    }

    const name = `Mic take ${Math.floor(seconds / 60)}m${String(Math.round(seconds % 60)).padStart(2, '0')}s.wav`;
    const file = new File([encodeWav(chunks, sampleRate)], name, { type: 'audio/wav' });
    start(file);
  }

  function encodeWav(chunks, sampleRate) {
    const frames = chunks.reduce((n, c) => n + c.length, 0);
    const view = new DataView(new ArrayBuffer(44 + frames * 2));
    const ascii = (offset, text) => {
      for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
    };
    ascii(0, 'RIFF');
    view.setUint32(4, 36 + frames * 2, true);
    ascii(8, 'WAVE');
    ascii(12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);                 // PCM
    view.setUint16(22, 1, true);                 // mono
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);    // byte rate
    view.setUint16(32, 2, true);                 // block align
    view.setUint16(34, 16, true);                // bits per sample
    ascii(36, 'data');
    view.setUint32(40, frames * 2, true);
    let off = 44;
    for (const chunk of chunks) {
      for (let i = 0; i < chunk.length; i++) {
        const s = Math.max(-1, Math.min(1, chunk[i]));
        view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
        off += 2;
      }
    }
    return new Blob([view.buffer], { type: 'audio/wav' });
  }

  const PHASES = 5;
  function beginPhases() {
    state.phase = 0;
    paintPhases();
    state.timer = setInterval(() => {
      if (state.phase < PHASES - 1) state.phase += 1;
      paintPhases();
    }, 1700);
  }
  function paintPhases() {
    const items = document.querySelectorAll('.phases li');
    items.forEach((li, i) => {
      li.classList.toggle('done', i < state.phase);
      li.classList.toggle('now', i === state.phase);
      if (i === state.phase) $('phase').textContent = li.textContent;
    });
  }
  function endPhases() {
    clearInterval(state.timer);
    document.querySelectorAll('.phases li').forEach((li) => { li.classList.add('done'); li.classList.remove('now'); });
  }

  /* ── render ─────────────────────────────────────────────── */

  const fmtTime = (s) => {
    const m = Math.floor(s / 60);
    return `${m}:${String(Math.round(s % 60)).padStart(2, '0')}`;
  };
  const pct = (x) => Math.max(0, Math.min(100, x));
  const titleCase = (s) => s.charAt(0).toUpperCase() + s.slice(1);

  function render(data) {
    const { theory, drums, critique, track } = data;

    $('trackName').textContent = track.name;
    $('trackMeta').textContent =
      `${fmtTime(track.seconds)} · ${(track.bytes / 1048576).toFixed(1)} MB · analysed in ${data.elapsed}s`;
    $('verdict').textContent = critique.verdict;

    renderKey(theory);
    renderTempo(theory, drums, track);
    renderTaste(critique);
    renderHarmony(theory);
    renderTimbre(theory.timbre);
    renderDrums(drums, data.midi ? `${API}${data.midi}` : null);
    renderNotes(critique);
  }

  function renderKey(theory) {
    const k = theory.key;
    $('keyLabel').textContent = k.label;
    const chips = $('scaleDegrees');
    chips.textContent = '';
    k.scale_degrees.forEach((d, i) => {
      chips.appendChild(el('span', 'chip' + (i === 0 ? ' tonic' : ''), d));
    });
    $('confFill').style.width = pct(k.confidence * 100) + '%';
    $('confText').textContent = `${Math.round(k.confidence * 100)}%` + (k.ambiguous ? ' · ambiguous' : '');

    const alts = (k.alternatives || []).slice(0, 2)
      .map((a) => `${a.label} ${Number(a.score ?? 0).toFixed(2)}`).join(' · ');
    $('keyAlt').textContent = alts ? `runners-up: ${alts}` : 'no close runner-up';
  }

  function renderTempo(theory, drums, track) {
    $('bpmVal').textContent = theory.tempo.bpm.toFixed(1);
    $('meterVal').textContent = `${theory.meter.meter}/4`;
    $('beatConf').textContent = Math.round(theory.tempo.beat_confidence * 100) + '%';
    $('downbeat').textContent = drums && drums.downbeat_phase != null
      ? `beat ${drums.downbeat_phase + 1}` : '—';
    $('duration').textContent = fmtTime(track.seconds);

    const ribbon = $('structure');
    ribbon.textContent = '';
    const sections = theory.structure.sections || [];
    const total = sections.length ? sections[sections.length - 1].end - sections[0].start : 1;
    // Floor each section so short ones stay legible, then renormalise to 100%.
    const widths = sections.map((s) => Math.max(9, ((s.end - s.start) / total) * 100));
    const sum = widths.reduce((a, b) => a + b, 0) || 1;
    sections.forEach((s, i) => {
      const node = el('span', null, s.label);
      node.style.flex = `0 0 ${(widths[i] / sum) * 100}%`;
      node.title = `${s.label} · ${s.start}s → ${s.end}s`;
      node.style.animationDelay = `${i * 45}ms`;
      ribbon.appendChild(node);
    });
    if (!sections.length) ribbon.appendChild(el('span', null, 'no sections'));
  }

  function renderTaste(critique) {
    const ring = $('ringFg');
    const C = 2 * Math.PI * 52;
    ring.style.strokeDasharray = C;
    ring.style.strokeDashoffset = C;
    requestAnimationFrame(() => {
      ring.style.strokeDashoffset = C * (1 - critique.overall / 100);
    });
    $('scoreNum').textContent = Math.round(critique.overall);
    $('grade').textContent = `GRADE ${critique.grade}`;

    const axes = $('axes');
    axes.textContent = '';
    critique.axes.forEach((a) => {
      const row = el('div', 'axis');
      row.appendChild(el('span', 'axis-name', a.label));
      const track = el('div', 'axis-track');
      const fill = el('span', 'axis-fill');
      track.appendChild(fill);
      row.appendChild(track);
      row.appendChild(el('span', 'axis-val', Math.round(a.score)));
      axes.appendChild(row);
      requestAnimationFrame(() => { fill.style.width = pct(a.score) + '%'; });
    });
  }

  function renderHarmony(theory) {
    const h = theory.harmony;
    const root = theory.key.tonic;
    const box = $('progression');
    box.textContent = '';
    h.progression.slice(0, 16).forEach((c, i) => {
      const node = el('div', 'chord' + (c.root === root ? ' home' : ''));
      node.style.animationDelay = `${i * 35}ms`;
      node.appendChild(el('b', null, c.chord));
      node.appendChild(el('i', null, c.roman));
      node.appendChild(el('small', null, `${c.t.toFixed(1)}s`));
      node.title = `${c.chord} (${c.roman}) — ${c.beats} beats from ${c.t}s`;
      box.appendChild(node);
    });
    $('harmonyFootnote').textContent =
      `${h.unique_chords} distinct chords · ${h.changes_per_bar.toFixed(2)} changes per bar · ` +
      `tonic accents outlined in violet`;
  }

  function renderTimbre(t) {
    const rows = [
      ['Brightness', `${t.brightness_hz} Hz`, t.brightness_hz / 5000],
      ['Percussive share', `${Math.round(t.percussive_ratio * 100)}%`, t.percussive_ratio],
      ['Dynamic range', `${(t.rms_p90_db - t.rms_p10_db).toFixed(1)} dB`, (t.rms_p90_db - t.rms_p10_db) / 30],
      ['Spectral flatness', t.flatness.toFixed(3), t.flatness / 0.4],
      ['Peak level', `${t.rms_p90_db.toFixed(1)} dBFS`, (t.rms_p90_db + 40) / 40],
    ];
    const box = $('timbreBars');
    box.textContent = '';
    rows.forEach(([label, value, ratio]) => {
      const row = el('div', 'bar-row');
      row.appendChild(el('span', 'bl', label));
      row.appendChild(el('span', 'bv', value));
      const track = el('div', 'bar-track');
      const fill = el('span', 'bar-fill');
      track.appendChild(fill);
      row.appendChild(track);
      box.appendChild(row);
      requestAnimationFrame(() => { fill.style.width = pct(ratio * 100) + '%'; });
    });
  }

  function renderDrums(drums, midiUrl) {
    const grid = $('drumGrid');
    grid.textContent = '';
    const meta = $('drumMeta');

    if (!drums || drums.error) {
      meta.textContent = drums?.error || 'drum transcription unavailable';
      $('drumTab').textContent = '';
      $('midiBtn').style.display = 'none';
      return;
    }

    const order = ['crash', 'openhat', 'hat', 'tom', 'clap', 'snare', 'kick'];
    const steps = drums.meter * 4;
    order.filter((i) => drums.core[i]).forEach((inst) => {
      const row = el('div', 'drum-row');
      row.appendChild(el('span', 'dname', inst));
      const on = new Set(drums.core[inst].steps);
      for (let s = 0; s < steps; s++) {
        row.appendChild(el('span', 'dcell' + (on.has(s) ? ' on' : '')));
      }
      grid.appendChild(row);
    });

    const bars = (drums.tab || '').split(/\n(?=bar )/);
    const shown = bars.slice(0, 3).join('\n');
    $('drumTab').textContent = bars.length > 3
      ? `${shown}\n… ${bars.length - 3} more bars in the .mid file`
      : shown;
    $('drumMeta').textContent =
      `${drums.onsets} onsets · ${drums.bars} bars · offbeat ${Math.round(drums.feel.offbeat_ratio * 100)}% · ` +
      `spread ${drums.feel.timing_spread_ms.toFixed(0)} ms`;

    const btn = $('midiBtn');
    if (midiUrl) { btn.href = midiUrl; btn.style.display = ''; }
    else btn.style.display = 'none';

    const hits = $('hits');
    hits.textContent = '';
    Object.entries(drums.instruments).forEach(([inst, s]) => {
      const row = el('div', 'hit');
      row.appendChild(el('span', 'hn', inst));
      const track = el('div', 'ht');
      const fill = el('span', 'hf');
      track.appendChild(fill);
      row.appendChild(track);
      row.appendChild(el('span', 'hv', `${s.per_bar}/bar`));
      row.title = `offset ${s.mean_offset_ms >= 0 ? '+' : ''}${s.mean_offset_ms} ms, spread ${s.offset_spread_ms} ms`;
      hits.appendChild(row);
      requestAnimationFrame(() => { fill.style.width = pct((s.per_bar / steps) * 100) + '%'; });
    });
    if (!Object.keys(drums.instruments).length) hits.appendChild(el('p', 'fine', 'no percussion found'));
  }

  function renderNotes(critique) {
    $('notesVerdict').textContent = critique.verdict;

    const list = $('fixes');
    list.textContent = '';
    critique.fixes.forEach((f, i) => {
      const li = el('li');
      li.appendChild(el('span', 'rank', String(i + 1).padStart(2, '0')));
      const body = el('div');
      const head = el('div', 'fix-head');
      head.appendChild(el('b', null, f.metric));
      head.appendChild(el('em', null, `−${f.impact} pts`));
      body.appendChild(head);
      body.appendChild(el('p', null, f.action));
      body.appendChild(el('small', null, f.issue));
      li.appendChild(body);
      list.appendChild(li);
    });
    if (!critique.fixes.length) list.appendChild(el('li', null, 'nothing to fix — it already sits in the pocket.'));

    const st = $('strengths');
    st.textContent = '';
    (critique.strengths || []).forEach((s) => st.appendChild(el('span', null, s)));
    (critique.weaknesses || []).forEach((s) => {
      const n = el('span', null, s);
      n.style.color = 'var(--warn)';
      st.appendChild(n);
    });
  }
})();
