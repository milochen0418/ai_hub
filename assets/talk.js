/*
 * The talking AI's page (ai_hub/views.py talk_view): its face, its voice, its ears, and what the
 * page it was opened over shows (asked through the intent bridge). It drives the Reflex form:
 * what is heard is typed into it and sent, with the context; what Reflex puts in #talk-say is read
 * out, with the mood the reply began with.
 *
 * The voice and the ears are the hub's (/api/talk, Kokoro and Whisper) once they are ready; until
 * then, or without them, the browser's own speech (speechSynthesis, SpeechRecognition).
 */
(() => {
  if (window.__talk) return;
  const T = (window.__talk = { speaking: false, listening: false, hearing: "", level: 0, mood: "neutral", tts: "", stt: "", said: [] });
  const $ = (id) => document.getElementById(id);
  const store = {
    get(k, d) { try { return localStorage.getItem(k) ?? d; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private window */ } },
  };
  let root = null;
  const cfg = () => (root && root.dataset) || {};
  const api = (path) => new URL("../api/talk/" + path, cfg().backend || location.href).toString();
  const lang = () => cfg().lang || "";
  const isZh = (text) => /[぀-ヿ㐀-䶿一-鿿]/.test(text || "") || (!text && /^zh/i.test(lang()));
  const busy = () => cfg().busy === "1";

  // ------------------------------------------------------------------ the face
  // Blendshapes of each mood (AvatarFace's names); the jaw follows the voice.
  const MOODS = {
    neutral: {},
    happy: { mouthSmileLeft: 0.75, mouthSmileRight: 0.75, cheekSquintLeft: 0.35, cheekSquintRight: 0.35 },
    laughing: { mouthSmileLeft: 0.95, mouthSmileRight: 0.95, cheekSquintLeft: 0.6, cheekSquintRight: 0.6, eyeSquintLeft: 0.5, eyeSquintRight: 0.5, jawOpen: 0.3 },
    surprised: { eyeWideLeft: 0.7, eyeWideRight: 0.7, browInnerUp: 0.8, browOuterUpLeft: 0.6, browOuterUpRight: 0.6, jawOpen: 0.35 },
    thinking: { browDownLeft: 0.35, browInnerUp: 0.3, eyeLookUpLeft: 0.6, eyeLookUpRight: 0.6, eyeLookOutLeft: 0.3, eyeLookInRight: 0.3, mouthPucker: 0.3, mouthLeft: 0.3 },
    sad: { mouthFrownLeft: 0.6, mouthFrownRight: 0.6, browInnerUp: 0.7, eyeSquintLeft: 0.2, eyeSquintRight: 0.2 },
    angry: { browDownLeft: 0.8, browDownRight: 0.8, mouthFrownLeft: 0.3, mouthFrownRight: 0.3, noseSneerLeft: 0.3, noseSneerRight: 0.3, mouthPressLeft: 0.4, mouthPressRight: 0.4 },
    wink: { eyeBlinkLeft: 1, mouthSmileLeft: 0.7, mouthSmileRight: 0.7 },
  };
  const STYLES = ["human", "cat", "panda", "robot"];
  const face = {
    canvas: null, cur: null, goal: null, mood: "neutral", moodAt: 0, nextBlink: 0, blinkUntil: 0, last: 0,
    style: STYLES.includes(store.get("talk_style", "human")) ? store.get("talk_style", "human") : "human",
    seed: "AI",
  };

  function setMood(mood) {
    face.mood = MOODS[mood] ? mood : "neutral";
    face.moodAt = performance.now();
    T.mood = face.mood;
  }

  function goalShapes(now) {
    const A = window.AvatarFace, g = face.goal;
    g.fill(0);
    const age = (now - face.moodAt) / 1000;
    const w = age < 12 ? 1 : Math.max(0.3, 1 - (age - 12) / 25); // a mood fades, never quite to nothing
    for (const [k, v] of Object.entries(MOODS[face.mood] || {})) g[A.INDEX[k]] = v * w;
    if (busy() && !T.speaking) {
      // Thinking: eyes up and away, a brow down.
      for (const [k, v] of Object.entries(MOODS.thinking)) g[A.INDEX[k]] = Math.max(g[A.INDEX[k]], v * 0.8);
    } else if (T.listening && !T.speaking) {
      g[A.INDEX.browInnerUp] = Math.max(g[A.INDEX.browInnerUp], T.hearing ? 0.45 : 0.2);
      if (T.hearing) g[A.INDEX.eyeWideLeft] = g[A.INDEX.eyeWideRight] = 0.25;
    }
    return g;
  }

  function animate(now) {
    requestAnimationFrame(animate);
    const A = window.AvatarFace;
    if (!A || !face.canvas) return;
    if (!face.cur) { face.cur = A.neutral(); face.goal = new Float32Array(A.SHAPES.length); }
    const dt = Math.min(0.1, (now - face.last) / 1000 || 0.016);
    face.last = now;
    const cur = face.cur, g = goalShapes(now);
    const k = 1 - Math.exp(-dt / 0.12);
    for (let i = 0; i < g.length; i++) cur.shapes[i] += (g[i] - cur.shapes[i]) * k;
    // The mouth follows the voice: the jaw by its loudness, the lips rounding now and then.
    const jaw = A.INDEX.jawOpen, funnel = A.INDEX.mouthFunnel, pucker = A.INDEX.mouthPucker;
    const want = T.speaking ? Math.min(1, T.level * 1.7 + (T.level > 0.02 ? 0.08 : 0)) : g[jaw];
    cur.shapes[jaw] += (want - cur.shapes[jaw]) * (1 - Math.exp(-dt / 0.045));
    const round = T.speaking ? Math.max(0, Math.sin(now / 210)) * 0.35 * Math.min(1, T.level * 3) : 0;
    cur.shapes[funnel] += (round - cur.shapes[funnel]) * (1 - Math.exp(-dt / 0.08));
    if (!T.speaking) cur.shapes[pucker] += (g[pucker] - cur.shapes[pucker]) * k;
    // Blinks, unless winking.
    if (now > face.nextBlink) { face.blinkUntil = now + 140; face.nextBlink = now + 2200 + Math.random() * 3800; }
    if (now < face.blinkUntil && face.mood !== "wink") cur.shapes[A.INDEX.eyeBlinkLeft] = cur.shapes[A.INDEX.eyeBlinkRight] = 1;
    // The head: breathing, a little sway; up and tilted while thinking, nodding while hearing.
    const think = busy() && !T.speaking;
    const goals = {
      yaw: Math.sin(now / 1700) * 0.05 + (think ? 0.12 : 0),
      pitch: (think ? -0.1 : T.hearing ? 0.06 + Math.sin(now / 260) * 0.03 : 0) + (T.speaking ? Math.sin(now / 430) * 0.03 : 0),
      roll: think ? 0.1 : Math.sin(now / 2300) * 0.02,
      x: 0,
      y: Math.sin(now / 900) * 0.03,
    };
    const kp = 1 - Math.exp(-dt / 0.25);
    for (const key of Object.keys(goals)) cur[key] += (goals[key] - cur[key]) * kp;
    paint(face.canvas, cur, { style: face.style, seed: face.seed, badge: false, voice: T.speaking ? T.level * 2 : 0 });
  }

  function paint(canvas, f, opts) {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const cw = Math.round(w * dpr), ch = Math.round(h * dpr);
    if (canvas.width !== cw || canvas.height !== ch) { canvas.width = cw; canvas.height = ch; }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    window.AvatarFace.draw(ctx, w, h, f, opts);
  }

  // ------------------------------------------------------------------ the voice
  const voice = { on: store.get("talk_voice", "1") !== "0", ctx: null, analyser: null, data: null, queue: [], busy: false, source: null, seq: 0, fake: 0 };

  function audio() {
    if (!voice.ctx) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      voice.ctx = new Ctx();
      voice.analyser = voice.ctx.createAnalyser();
      voice.analyser.fftSize = 1024;
      voice.analyser.smoothingTimeConstant = 0.4;
      voice.analyser.connect(voice.ctx.destination);
      voice.data = new Uint8Array(voice.analyser.fftSize);
    }
    if (voice.ctx.state === "suspended") voice.ctx.resume().catch(() => {});
    return voice.ctx;
  }

  function measure() {
    if (voice.source && voice.analyser) {
      voice.analyser.getByteTimeDomainData(voice.data);
      let sum = 0;
      for (let i = 0; i < voice.data.length; i++) { const v = (voice.data[i] - 128) / 128; sum += v * v; }
      const rms = Math.sqrt(sum / voice.data.length);
      T.level += (Math.min(1, rms * 4) - T.level) * 0.5;
    } else if (voice.fake) {
      T.level = 0.18 + 0.22 * Math.abs(Math.sin(performance.now() / 70)) * Math.abs(Math.sin(performance.now() / 330));
    } else {
      T.level *= 0.7;
    }
  }
  setInterval(measure, 33);

  // The words cut into sentences: the first plays while the next are made.
  function sentences(text) {
    const out = [];
    for (const line of String(text || "").split(/\n+/)) {
      for (const piece of line.split(/(?<=[。！？!?；;])\s*|(?<=[.!?])\s+(?=[A-Z0-9"“(])/)) {
        const p = piece.trim();
        if (!p) continue;
        if (out.length && p.length < 6 && !/[.!?。！？]$/.test(p) && !isZh(p) && out[out.length - 1].length + p.length < 200) out[out.length - 1] += " " + p;
        else out.push(p);
      }
    }
    return out;
  }

  async function fetchTts(item) {
    const r = await fetch(api("tts"), { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text: item.text, lang: lang() }) });
    if (!r.ok) throw new Error("tts " + r.status);
    item.buffer = await audio().decodeAudioData(await r.arrayBuffer());
  }

  function prefetch() {
    for (const it of voice.queue.slice(0, 2)) {
      if (it.promise) continue;
      it.promise = (T.tts === "server" ? fetchTts(it) : Promise.reject(new Error("browser"))).catch((e) => { it.error = e; });
    }
  }

  function playBuffer(item) {
    return new Promise((resolve) => {
      const ctx = audio();
      const source = ctx.createBufferSource();
      source.buffer = item.buffer;
      source.connect(voice.analyser);
      source.onended = () => { if (voice.source === source) voice.source = null; resolve(); };
      voice.source = source;
      try { source.start(); } catch (e) { voice.source = null; resolve(); }
    });
  }

  function speakBrowser(text) {
    return new Promise((resolve) => {
      const synth = window.speechSynthesis;
      if (!synth) { resolve(); return; }
      const u = new SpeechSynthesisUtterance(text);
      const want = isZh(text) ? "zh" : "en";
      u.lang = want === "zh" ? (/^zh-(cn|hans)/i.test(lang()) ? "zh-CN" : "zh-TW") : "en-US";
      const voices = synth.getVoices().filter((v) => v.lang.toLowerCase().startsWith(want));
      const pick = voices.find((v) => v.lang.toLowerCase() === u.lang.toLowerCase()) || voices[0];
      if (pick) u.voice = pick;
      u.rate = 1.0;
      let done = false;
      const finish = () => { if (done) return; done = true; voice.fake = 0; resolve(); };
      u.onend = finish;
      u.onerror = finish;
      voice.fake = 1;
      synth.speak(u);
      setTimeout(finish, 1000 + text.length * 350); // a voice that never says it ended
    });
  }

  async function pump() {
    if (voice.busy) return;
    const it = voice.queue.shift();
    if (!it) { T.speaking = false; if (root) root.dataset.speaking = ""; return; }
    voice.busy = true;
    T.speaking = true;
    if (root) root.dataset.speaking = "1";
    prefetch();
    await it.promise;
    if (it.seq === voice.seq) {
      T.said.push(it.text);
      if (it.buffer) await playBuffer(it);
      else await speakBrowser(it.text);
    }
    voice.busy = false;
    pump();
  }

  function caption(text) {
    const el = $("talk-caption");
    if (el) el.textContent = text || "";
  }

  function say(seq, text, mood) {
    stopSpeaking();
    setMood(mood);
    caption(text);
    if (!voice.on || !text) return;
    voice.seq = seq;
    voice.queue = sentences(text).map((t) => ({ seq, text: t }));
    prefetch();
    pump();
  }

  function stopSpeaking() {
    voice.seq = String(Number(voice.seq) + 0.5); // nothing queued matches any more
    voice.queue = [];
    if (voice.source) { try { voice.source.stop(); } catch (e) { /* ended */ } voice.source = null; }
    if (window.speechSynthesis) window.speechSynthesis.cancel();
    voice.fake = 0;
    T.speaking = false;
    if (root) root.dataset.speaking = "";
  }

  // ------------------------------------------------------------------ the ears
  const ears = { on: false, stream: null, ws: null, node: null, rec: null, mode: "", retry: null };

  function hearing(text) {
    T.hearing = text || "";
    const el = $("talk-hearing");
    if (el) el.textContent = text ? "… " + text : "";
    const mic = $("talk-mic");
    if (mic) mic.dataset.hearing = text ? "1" : "0";
  }

  function heard(text) {
    hearing("");
    const input = $("talk-input");
    if (!input || !text) return;
    input.value = text;
    submit();
  }

  async function startEars() {
    if (ears.on) return;
    try {
      ears.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    } catch (e) {
      status("The microphone can't be used here: " + (e && e.message ? e.message : e));
      return;
    }
    ears.on = true;
    T.listening = true;
    store.set("talk_mic", "1");
    ui();
    await listen();
  }

  // The hub's ears when they are ready, else the browser's; switched when that changes.
  async function listen() {
    if (!ears.on) return;
    const mode = T.stt === "server" ? "server" : "browser";
    if (ears.mode === mode) return;
    stopListening();
    ears.mode = mode;
    if (mode === "server") await listenServer();
    else listenBrowser();
  }

  async function listenServer() {
    const ctx = audio();
    try {
      await ctx.audioWorklet.addModule("/talk_worklet.js");
    } catch (e) {
      status("Can't read the microphone: " + e.message);
      ears.mode = "";
      return;
    }
    if (!ears.on || !ears.stream) return;
    const src = ctx.createMediaStreamSource(ears.stream);
    const node = new AudioWorkletNode(ctx, "talk-pcm");
    const mute = ctx.createGain();
    mute.gain.value = 0;
    src.connect(node);
    node.connect(mute).connect(ctx.destination);
    ears.node = node;
    const open = () => {
      if (!ears.on || ears.mode !== "server") return;
      const ws = new WebSocket(api("stt?lang=" + encodeURIComponent(lang())).replace(/^http/, "ws"));
      ears.ws = ws;
      ws.onmessage = (ev) => {
        let m;
        try { m = JSON.parse(ev.data); } catch (e) { return; }
        if (m.type === "status") { applyStatus({ stt: m }); return; }
        if (m.type !== "subtitle") return;
        if (m.final) { if (m.text && !T.speaking) heard(m.text); else hearing(""); }
        else hearing(m.text);
      };
      ws.onclose = () => { if (ears.ws === ws && ears.on && ears.mode === "server") ears.retry = setTimeout(open, 2000); };
    };
    // While the AI speaks nothing goes to the ears: its own voice must not come back as words.
    node.port.onmessage = (ev) => { if (ears.ws && ears.ws.readyState === 1 && !T.speaking) ears.ws.send(ev.data); };
    open();
  }

  function listenBrowser() {
    const R = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!R) { status("This browser can't hear by itself: type instead, or wait for the hub's ears."); return; }
    const rec = new R();
    rec.lang = /^zh/i.test(lang()) ? (/^zh-(cn|hans)/i.test(lang()) ? "zh-CN" : "zh-TW") : (lang() || navigator.language || "en-US");
    rec.continuous = true;
    rec.interimResults = true;
    rec.onresult = (ev) => {
      let final = "", interim = "";
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const t = ev.results[i][0].transcript;
        if (ev.results[i].isFinal) final += t; else interim += t;
      }
      if (interim) hearing(interim);
      if (final.trim()) { if (!T.speaking) heard(final.trim()); else hearing(""); }
    };
    rec.onend = () => { if (ears.on && ears.mode === "browser" && ears.rec === rec) { try { rec.start(); } catch (e) { /* already */ } } };
    rec.onerror = (ev) => { if (ev.error === "not-allowed") status("The microphone isn't allowed here."); };
    ears.rec = rec;
    try { rec.start(); } catch (e) { status("Can't listen: " + e.message); }
  }

  function stopListening() {
    if (ears.retry) { clearTimeout(ears.retry); ears.retry = null; }
    if (ears.ws) { const ws = ears.ws; ears.ws = null; try { ws.close(); } catch (e) { /* closed */ } }
    if (ears.node) { try { ears.node.port.onmessage = null; ears.node.disconnect(); } catch (e) { /* gone */ } ears.node = null; }
    if (ears.rec) { const rec = ears.rec; ears.rec = null; try { rec.onend = null; rec.stop(); } catch (e) { /* stopped */ } }
    ears.mode = "";
    hearing("");
  }

  function stopEars() {
    ears.on = false;
    T.listening = false;
    store.set("talk_mic", "0");
    stopListening();
    if (ears.stream) { for (const t of ears.stream.getTracks()) t.stop(); ears.stream = null; }
    ui();
  }

  // ------------------------------------------------------------------ the hub's voice and ears
  const ready = { tts: null, stt: null, timer: null };

  function status(text) {
    const el = $("talk-status");
    if (el) el.textContent = text || "";
  }

  function describe(s) {
    if (!s) return "";
    if (s.phase === "ready") return "ready";
    if (s.phase === "downloading") return "downloading" + (s.progress ? " " + s.progress : "");
    if (s.phase === "loading") return "loading";
    if (s.phase === "unavailable") return "not installed";
    if (s.phase === "error") return "failed";
    return "starting";
  }

  function applyStatus(s) {
    if (s.tts) ready.tts = s.tts;
    if (s.stt) ready.stt = s.stt;
    T.tts = ready.tts && ready.tts.phase === "ready" ? "server" : "browser";
    T.stt = ready.stt && ready.stt.phase === "ready" ? "server" : "browser";
    const both = T.tts === "server" && T.stt === "server";
    const final = (x) => x && (x.phase === "ready" || x.phase === "unavailable" || x.phase === "error");
    if (both) status("");
    else status("The hub's voice: " + describe(ready.tts) + " · its ears: " + describe(ready.stt) + (both ? "" : " (the browser's own speech meanwhile)"));
    if (final(ready.tts) && final(ready.stt) && ready.timer) { clearInterval(ready.timer); ready.timer = null; }
    if (ears.on) listen();
  }

  async function poll() {
    try {
      const r = await fetch(api("status"));
      if (r.ok) applyStatus(await r.json());
    } catch (e) { /* the hub is away: the browser speaks */ }
  }

  // ------------------------------------------------------------------ the page under the dialog
  async function refreshContext() {
    const el = $("talk-context");
    if (!el) return;
    const page = window.__ddnsIntentPage;
    if (!page || !page.ask) { el.value = ""; return; }
    try {
      const data = await page.ask(800);
      el.value = data == null ? "" : JSON.stringify(data);
    } catch (e) { el.value = ""; }
  }

  function submit() {
    const form = $("talk-form");
    if (!form) return;
    refreshContext().then(() => form.requestSubmit());
  }

  // ------------------------------------------------------------------ wiring
  function ui() {
    const mic = $("talk-mic"), spk = $("talk-speaker");
    if (mic) { mic.dataset.on = ears.on ? "1" : "0"; mic.title = ears.on ? "Stop listening" : "Listen to me (hands free)"; }
    if (spk) spk.dataset.on = voice.on ? "1" : "0";
  }

  let lastSeq = "";
  function check() {
    const el = $("talk-say");
    if (!el) return;
    const seq = el.dataset.seq || "";
    if (seq && seq !== lastSeq) { lastSeq = seq; say(seq, el.dataset.text || "", el.dataset.mood || "neutral"); }
  }

  function wire() {
    root = $("talk-root");
    if (!root || !window.AvatarFace || !$("talk-say")) { setTimeout(wire, 100); return; }
    face.canvas = $("talk-face");
    face.seed = (cfg().app || "AI") + ":" + face.style;
    const style = $("talk-style");
    if (style && !style.options.length) {
      for (const s of STYLES) { const o = document.createElement("option"); o.value = s; o.textContent = s; style.append(o); }
      style.value = face.style;
      style.onchange = () => { face.style = style.value; store.set("talk_style", face.style); face.seed = (cfg().app || "AI") + ":" + face.style; };
    }
    $("talk-mic").onclick = () => { audio(); if (ears.on) stopEars(); else startEars(); };
    $("talk-speaker").onclick = () => { audio(); voice.on = !voice.on; store.set("talk_voice", voice.on ? "1" : "0"); if (!voice.on) stopSpeaking(); ui(); };
    $("talk-stop").onclick = () => stopSpeaking();
    face.canvas.onclick = () => { if (T.speaking) stopSpeaking(); };
    document.addEventListener("keydown", (e) => {
      const el = $("talk-input");
      if (e.target === el && e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
        e.preventDefault();
        if (el.value.trim()) submit();
      }
    });
    new MutationObserver(check).observe($("talk-say"), { attributes: true });
    check();
    ui();
    requestAnimationFrame(animate);
    poll();
    ready.timer = setInterval(poll, 3000);
    refreshContext();
    setInterval(refreshContext, 2000);
    // New words scroll into view.
    let height = 0;
    setInterval(() => {
      const list = $("talk-messages");
      if (list && list.scrollHeight !== height) { list.scrollTop = list.scrollHeight; height = list.scrollHeight; }
    }, 300);
    // The microphone as it was left last time (the page under it was clicked, so sound may start).
    if (store.get("talk_mic", "0") === "1") startEars();
    T.ready = true;
  }
  T.say = say;
  T.stop = stopSpeaking;
  T.sentences = sentences;
  T.state = () => ({ speaking: T.speaking, listening: T.listening, hearing: T.hearing, level: T.level, mood: T.mood, tts: T.tts, stt: T.stt, queued: voice.queue.length, said: T.said.slice(), style: face.style });
  wire();
})();
