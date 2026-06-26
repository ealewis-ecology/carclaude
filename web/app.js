/* carclaude PWA — voice loop client.
 *
 * Flow per turn: mic -> /api/stt -> /api/message (SSE) -> /api/tts -> play -> (Auto) again.
 * Push-to-talk: hold the button, speak, release. Auto: hands-free loop with adaptive VAD.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const transcript = $('transcript'), statusEl = $('status'), meta = $('meta');
const talkBtn = $('talk'), autoBtn = $('auto'), stopBtn = $('stop'), clearBtn = $('clear'), player = $('player'), ackPlayer = $('ackPlayer');

let token = localStorage.getItem('carclaude_token') || '';
let auto = false;                 // hands-free loop armed
let busy = false;                 // a turn is in flight (STT -> agent -> TTS)
let holding = false;              // talk button currently held
let micStream = null, recorder = null, chunks = [], recMime = '', recTimer = null;
let audioCtx = null, analyser = null, vadTimer = null, bargeTimer = null;
let msgAbort = null;
let vadPauseMs = 1000, vadMaxMs = 30000;   // VAD timing, live from /api/config
let bargeSensitivity = 5;                  // 1 (hard to interrupt) .. 10 (easy)
let vadSpeechFloor = 0.012;                // min normalized RMS (0..1) that counts as speech; live from /api/config
let DEBUG_VAD = true;                       // TEMP: surface live VAD telemetry in the status line (iOS Auto diagnosis)
let assistantEl = null;
let clearing = false;             // a button /clear is driving completion; suppress the aborted-stream tail

// Spoken activity cues in the persona voice — only when the turn is real work. The server names
// each action ("Reading files.", "Running a command.", "Searching the web.") and streams it as a
// `status` event; the client speaks that, so the driver hears WHAT it's doing rather than a generic
// "On it." The pure-thinking cue (no tool yet, past ackDelayMs) is `ackPhrase` ("Thinking.") and
// fires only when `ackThinking` is on — off by default, so plain thinking stays silent and only tool
// actions cue. A quick one-shot answer reaches the reply first and stays silent, so cues aren't
// repeated after every prompt. Each distinct phrase is synthesized once via /api/tts and cached
// (instant + free after first use), cleared when the persona/voice changes. Plays on its own
// <audio> so it never tangles with the reply player or its finishTurn handlers.
let ackEnabled = true, ackPhrase = 'Thinking.';   // live from /api/config (voice-tunable prefs)
let ackThinking = false;          // speak the pure-thinking cue (no tool yet)? tool cues ignore this
let ackDelayMs = 2500;            // when ackThinking is on, how long the agent may think first (0 = every turn)
let ackCache = new Map();         // phrase -> object URL of its synthesized audio
let ackAbort = null;              // in-flight ack TTS fetch (abortable on stop/barge/turn-end/supersede)
let ackTimer = null;              // pending "thinking" cue (fires if the turn runs long with no tool)
let ackPlaying = false;           // ack audio currently sounding (pauses barge self-trigger)
let ackSeq = 0;                   // identifies the latest cue; a superseded one must not mutate shared ack state
let replyStarted = false;         // the real reply is synthesizing/playing -> suppress a late ack
let turnSeq = 0;                  // bumps each turn / on stop / on turn-end -> invalidates a stale ack

// ----------------------------------------------------------------- helpers
function setStatus(s) { statusEl.textContent = s || ''; }
function addMsg(kind, text) {
  const el = document.createElement('div');
  el.className = 'msg ' + kind; el.textContent = text;
  transcript.appendChild(el); transcript.scrollTop = transcript.scrollHeight;
  return el;
}
function authHeaders(extra) { return Object.assign({ 'Authorization': 'Bearer ' + token }, extra || {}); }
async function api(path, opts) {
  opts = opts || {}; opts.headers = authHeaders(opts.headers);
  const res = await fetch(path, opts);
  if (res.status === 401) { showToken(); throw new Error('unauthorized'); }
  return res;
}
function rms(buf) {
  let s = 0; for (let i = 0; i < buf.length; i++) { const v = (buf[i] - 128) / 128; s += v * v; }
  return Math.sqrt(s / buf.length);
}

// Voice "/clear": only a whole utterance that IS the command triggers a reset, so a request
// like "clear the build directory" still reaches the agent untouched. Kept to unambiguous
// meta-commands about the conversation — phrases that double as task instructions ("start
// over") or imply erasing the on-disk log ("clear history") are deliberately excluded.
const CLEAR_PHRASES = new Set([
  'clear', 'clear conversation', 'clear the conversation', 'clear context', 'clear the context',
  'clear chat', 'new conversation', 'reset conversation', 'reset the conversation',
]);
function isClearCommand(text) {
  let s = (text || '').toLowerCase().trim();
  s = s.replace(/^\/+/, '');                            // a literal "/clear"
  s = s.replace(/^(forward[- ]|back[- ])?slash\s+/, ''); // STT of "/" -> "slash clear"
  s = s.replace(/[.!?,;:]+$/g, '').replace(/\s+/g, ' ').trim();
  return CLEAR_PHRASES.has(s);
}
function resetTranscript() { transcript.innerHTML = ''; assistantEl = null; }

// ----------------------------------------------------------------- token gate
function showToken() { $('tokenOverlay').classList.remove('hidden'); }
function hideToken() { $('tokenOverlay').classList.add('hidden'); }
$('tokenSave').onclick = () => {
  const v = $('tokenInput').value.trim();
  if (!v) return;
  token = v; localStorage.setItem('carclaude_token', v); hideToken();
  ensureMic().catch(() => {}); primeAudio();   // this click is a gesture — warm mic + audio now
  init();
};

// ----------------------------------------------------------------- audio + mic
async function ensureMic() {                 // acquire the mic (gates recording)
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!audioCtx) audioCtx = new AC();
  if (audioCtx.state === 'suspended') { try { await audioCtx.resume(); } catch (e) {} }
  if (!micStream) {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    });
    const src = audioCtx.createMediaStreamSource(micStream);
    analyser = audioCtx.createAnalyser(); analyser.fftSize = 1024; src.connect(analyser);
    // iOS Safari only advances an AnalyserNode fed by getUserMedia when the audio graph actually
    // terminates at the destination; left dangling, getByteTimeDomainData stays flat (constant 128,
    // RMS ~0) on iPhone — so the VAD never hears speech and Auto never submits (push-to-talk is fine
    // because it uses MediaRecorder, not this graph). Route the analyser to the destination through a
    // muted node (gain 0 = silent, no feedback) to force the graph to render. Desktop is unaffected.
    const sink = audioCtx.createGain(); sink.gain.value = 0;
    analyser.connect(sink); sink.connect(audioCtx.destination);
  }
}
function primeAudio() {                       // unlock <audio> playback for later TTS (non-blocking)
  for (const el of [player, ackPlayer]) {     // both the reply player and the ack-cue player
    try {
      el.muted = true;
      const p = el.play();
      if (p && p.catch) p.catch(() => {});
      setTimeout(() => { try { el.pause(); el.muted = false; } catch (e) {} }, 40);
    } catch (e) {}
  }
}

function pickMime() {
  const want = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/aac', 'audio/ogg'];
  for (const m of want) if (window.MediaRecorder && MediaRecorder.isTypeSupported(m)) return m;
  return '';
}

// ----------------------------------------------------------------- recording
function clearRecTimer() { if (recTimer) { clearTimeout(recTimer); recTimer = null; } }

function startRec(useVad) {
  if (busy || !micStream) return;
  if (recorder) { if (recorder.state !== 'inactive') return; recorder = null; }  // drop stale
  // iOS Safari suspends the AudioContext after the TTS <audio> plays; the hands-free relisten
  // never goes through ensureMic, so the VAD's analyser would read flat silence and never submit.
  // Resume it here (idempotent, fire-and-forget) so every record window has a live analyser.
  if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume().catch(() => {});
  stopBargeMonitor();
  chunks = [];
  recMime = pickMime();
  try {
    recorder = new MediaRecorder(micStream, recMime ? { mimeType: recMime } : undefined);
  } catch (e) { setStatus('Recorder error'); recorder = null; return; }
  recorder.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
  recorder.onstop = onRecStop;
  recorder.start();
  talkBtn.classList.add('rec');
  setStatus(useVad ? 'Listening…' : 'Recording — release to send');
  clearRecTimer();
  recTimer = setTimeout(() => { if (recorder) stopRec(); }, (useVad ? vadMaxMs : 30000) + 500);  // hard cap
  if (useVad) startVad();
}

function stopRec() {
  stopVad(); clearRecTimer();
  if (recorder && recorder.state !== 'inactive') recorder.stop();
}

function cancelRec() {                         // discard whatever is recording
  stopVad(); clearRecTimer();
  if (recorder && recorder.state !== 'inactive') { recorder.onstop = () => {}; try { recorder.stop(); } catch (e) {} }
  recorder = null; talkBtn.classList.remove('rec');
}

async function onRecStop() {
  clearRecTimer();
  const blob = new Blob(chunks, { type: recMime || 'audio/webm' });
  recorder = null; talkBtn.classList.remove('rec');
  if (blob.size < 1500) { setStatus("Didn't catch that."); if (auto && !busy) relisten(); return; }
  await handleAudio(blob);
}

// Adaptive VAD: tracks the ambient noise floor (works in a noisy car) and stops after a
// pause once speech has been heard.
function startVad() {
  stopVad();
  const buf = new Uint8Array(analyser.fftSize);
  const t0 = Date.now();
  let floor = 0.03, lastVoice = 0, ticks = 0;
  const HANG = vadPauseMs, MAXMS = vadMaxMs, MINMS = 400;
  vadTimer = setInterval(() => {
    analyser.getByteTimeDomainData(buf);
    const r = rms(buf), now = Date.now(), elapsed = now - t0;
    // TEMP diagnostic: live VAD telemetry in the status line. state(r/s/c) · rms · floor · ms since the
    // last speech-level sample (counts up while you pause; submits when it passes the pause setting) ·
    // elapsed (deciseconds). If rms never moves while you talk, the analyser is flat (WebAudio graph).
    if (DEBUG_VAD) setStatus(`L ${audioCtx ? audioCtx.state[0] : '?'} rms${Math.round(r * 1000)} fl${Math.round(floor * 1000)} v${lastVoice ? Math.round(now - lastVoice) : '-'} t${Math.round(elapsed / 100)}`);
    // Warm up the noise floor, but cap it: if the window opens on a loud transient (echo of the
    // reply that just finished), an unclamped floor pushes speakT above normal speech and we'd
    // never register that the user spoke — so we'd never submit.
    if (++ticks < 6) { floor = Math.min(0.06, floor * 0.5 + r * 0.5); return; }
    // Track the noise floor EVERY tick as a fast-falling minimum with a slow upward leak. The old
    // code updated the floor only on non-speech ticks (r <= speakT), so a pause whose level sat
    // ABOVE speakT — a close headset/boom mic, where autoGainControl ramps the gain up the instant
    // you stop talking and inflates breath/self-noise into a steady plateau — froze the floor and
    // re-armed lastVoice every tick: the pause clock never advanced and it listened to the 30s cap.
    // Falling to meet quiet dips keeps the floor pinned at the valleys of MODULATED speech (so faint
    // far-field car speech still towers over speakT and is never cut off); a steady plateau with no
    // dips makes the floor leak upward until speakT clears it and end-of-turn fires ~pause_ms later.
    // The 0.30 cap bounds it so a loud sustained passage can't push speakT above real speech.
    if (r < floor) floor = floor * 0.7 + r * 0.3;        // fall fast toward a quiet dip
    else floor = Math.min(0.30, floor + 0.0015);         // steady plateau: leak the floor up slowly
    // floor*2.0 is the adaptive bar; vadSpeechFloor is the absolute backstop for dead silence AND
    // the knob quiet mics lower (default 0.012) so faint speech still clears it. See speech_floor pref.
    const speakT = Math.max(vadSpeechFloor, floor * 2.0);
    if (r > speakT) lastVoice = now;                      // speech-level audio right now
    if (elapsed > MAXMS) return stopRec();
    // End the turn once we've heard speech and HANG ms have passed with no speech-level audio.
    // Measuring the pause from the LAST speech sample (not from reaching deep silence) is what makes
    // end-of-turn fire reliably in mild ambient — the old "wait for true silence" never tripped when
    // the room floor sat above that silence threshold, so it listened until the 30s hard cap.
    if (lastVoice && elapsed > MINMS && (now - lastVoice) > HANG) stopRec();
  }, 60);
}
function stopVad() { if (vadTimer) { clearInterval(vadTimer); vadTimer = null; } }

// ----------------------------------------------------------------- barge-in
// While the agent thinks/talks (Auto), watch the mic; if the user clearly speaks over the
// current ambient+echo level, stop the agent and listen. Floor adapts continuously (incl.
// the agent's own voice) so it doesn't self-trigger.
function startBargeMonitor() {
  if (!auto || !analyser) return;
  stopBargeMonitor();
  const buf = new Uint8Array(analyser.fftSize);
  const s = bargeSensitivity;                       // 1 (hard) .. 10 (easy)
  const MULT = Math.max(1.6, 3.2 - s * 0.16), NEED = s >= 7 ? 3 : 4;
  let base = 0.05, loud = 0, ticks = 0;
  bargeTimer = setInterval(() => {
    analyser.getByteTimeDomainData(buf);
    const r = rms(buf);
    base = base * 0.92 + r * 0.08;                  // always track ambient (incl. TTS echo)
    if (ackPlaying) { loud = 0; return; }            // our own ack cue is sounding — don't self-barge
    if (++ticks < 8) return;                         // ~560ms warmup before it can fire
    if (r > Math.max(vadSpeechFloor, base * MULT)) { if (++loud >= NEED) bargeIn(); }
    else loud = Math.max(0, loud - 1);
  }, 70);
}
function stopBargeMonitor() { if (bargeTimer) { clearInterval(bargeTimer); bargeTimer = null; } }

function interruptActive() {            // stop agent audio + request + server turn
  stopBargeMonitor(); stopAck();
  try { player.pause(); } catch (e) {}
  if (msgAbort) { try { msgAbort.abort(); } catch (e) {} msgAbort = null; }
  api('/api/interrupt', { method: 'POST' }).catch(() => {});
  busy = false; talkBtn.disabled = false;
}
function bargeIn() { interruptActive(); setStatus('Go ahead…'); startRec(true); }

// ----------------------------------------------------------------- spoken acknowledgement
function clearAckCache() {                    // persona/voice or phrase changed -> re-synthesize
  ackCache.forEach((u) => { try { URL.revokeObjectURL(u); } catch (e) {} });
  ackCache.clear();
}
function stopAck() {                          // silence + invalidate any pending/playing ack cue
  clearAckTimer();
  ackPlaying = false;
  try { ackPlayer.pause(); } catch (e) {}
  if (ackAbort) { try { ackAbort.abort(); } catch (e) {} ackAbort = null; }
  turnSeq++;                                  // a fetch still in flight for that turn won't play
}
function clearAckTimer() { if (ackTimer) { clearTimeout(ackTimer); ackTimer = null; } }
// Arm the "thinking" cue for a turn. It sounds ONLY when the turn is real work — the agent is still
// working after ackDelayMs with no tool yet (long thinking / a long reply). A quick one-shot answer
// reaches `speak` first and clears the timer, so it stays silent. Tool actions cue themselves via
// the server's `status` events (handled in onEvent), independent of this timer.
function armAck(myTurn) {
  clearAckTimer();
  if (!ackEnabled || !ackThinking) return;                  // pure-thinking cue off -> only tools cue
  if (ackDelayMs <= 0) return playAck(myTurn, ackPhrase);   // 0 = cue every turn (old always-on behavior)
  ackTimer = setTimeout(() => { ackTimer = null; playAck(myTurn, ackPhrase); }, ackDelayMs);
}
// Speak a short cue (`phrase`). Best-effort: every failure is swallowed so it can never disrupt the
// turn. A newer cue supersedes the current one: we tear the current cue down up front (abort its
// synth, stop its audio, drop the flag) so no stale audio outlives its owner and ackPlaying can
// never be orphaned true (which would deafen barge-in for the rest of the turn). `mine`/ackSeq then
// guards the async synth gap so a superseded cue can't resurrect the flag once a newer one owns it.
// Cached per phrase, so only a phrase's first use hits the network; after that it's instant and free.
async function playAck(myTurn, phrase) {
  if (!ackEnabled || !token || !phrase) return;
  if (myTurn !== turnSeq || replyStarted) return;       // turn ended, or the reply already owns audio
  const mine = ++ackSeq;                                 // identity: a newer cue supersedes this one
  // Tear down whatever cue is in flight/sounding so this one cleanly replaces it.
  if (ackAbort) { try { ackAbort.abort(); } catch (e) {} ackAbort = null; }
  ackPlaying = false; try { ackPlayer.pause(); } catch (e) {}
  try {
    let url = ackCache.get(phrase);
    if (!url) {
      ackAbort = new AbortController();
      const r = await api('/api/tts', {
        method: 'POST', signal: ackAbort.signal,
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: phrase })
      });
      if (mine !== ackSeq) return;                      // a newer cue superseded us during synth
      ackAbort = null;
      if (!r.ok) return;
      url = URL.createObjectURL(await r.blob());
      ackCache.set(phrase, url);
    }
    if (mine !== ackSeq) return;                        // a newer cue superseded us during synth
    if (myTurn !== turnSeq || replyStarted) return;     // turn ended, or the reply already owns audio
    ackPlaying = true;
    ackPlayer.src = url;
    // Only the latest cue may clear ackPlaying: a superseded older cue's end/error must not flip it
    // off while the newer cue is still sounding (that would un-suppress barge self-trigger).
    ackPlayer.onended = ackPlayer.onerror = () => { if (mine === ackSeq) ackPlaying = false; };
    await ackPlayer.play().catch(() => { if (mine === ackSeq) ackPlaying = false; });
  } catch (e) { if (mine === ackSeq) ackPlaying = false; }
}

// ----------------------------------------------------------------- one turn
async function handleAudio(blob) {
  busy = true; talkBtn.disabled = true;
  try {
    setStatus('Transcribing…');
    const fd = new FormData();
    const ext = (blob.type.split('/')[1] || 'webm').split(';')[0];
    fd.append('audio', blob, 'speech.' + ext);
    const r = await api('/api/stt', { method: 'POST', body: fd });
    if (!r.ok) { setStatus('STT error'); return finishTurn(); }
    const text = ((await r.json()).text || '').trim();
    if (!text) { setStatus("Didn't catch that."); return finishTurn(); }
    if (isClearCommand(text)) return clearConversation(true);   // spoken /clear
    addMsg('user', text);
    await streamMessage(text);
  } catch (e) {
    if (e.name === 'AbortError' && clearing) return;   // a button /clear owns the completion
    if (e.name !== 'AbortError') setStatus('Error: ' + e.message);
    finishTurn();
  }
}

async function streamMessage(text) {
  setStatus('Thinking…');
  replyStarted = false;
  const myTurn = ++turnSeq;
  startBargeMonitor();
  armAck(myTurn);                   // arm the "thinking" cue if the turn runs long; tools cue themselves
  assistantEl = null;
  let full = '';
  msgAbort = new AbortController();
  const r = await api('/api/message', {
    method: 'POST', signal: msgAbort.signal,
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text })
  });
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const line = buf.slice(0, i).split('\n').find((l) => l.startsWith('data:'));
      buf = buf.slice(i + 2);
      if (!line) continue;
      full = onEvent(JSON.parse(line.slice(5).trim()), full, myTurn);
    }
  }
}

function onEvent(evt, full, myTurn) {
  if (evt.type === 'text') {
    if (!assistantEl) assistantEl = addMsg('assistant', '');
    full += evt.text; assistantEl.textContent = full;
    transcript.scrollTop = transcript.scrollHeight;
  } else if (evt.type === 'status') {
    clearAckTimer(); playAck(myTurn, evt.phrase);   // server named the current action — speak what it's doing
  } else if (evt.type === 'tool') {
    const inp = evt.input && (evt.input.command || evt.input.file_path || evt.input.path || evt.input.pattern || '');
    addMsg('tool', '› ' + evt.name + (inp ? ': ' + inp : ''));   // transcript only; voice handled by `status`
  } else if (evt.type === 'denied') {
    addMsg('denied', '⊘ blocked: ' + evt.reason);
  } else if (evt.type === 'error') {
    addMsg('denied', '⚠ ' + evt.message); setStatus('Error');
  } else if (evt.type === 'done') {
    speak((evt.text || full).trim());
  }
  return full;
}

async function speak(text) {
  if (!text) return finishTurn();
  replyStarted = true; clearAckTimer();         // reply is here — a pending/late ack cue must not talk over it
  try {
    setStatus('Speaking…');
    const r = await api('/api/tts', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text })
    });
    if (!r.ok) { setStatus('TTS error'); return finishTurn(); }
    const url = URL.createObjectURL(await r.blob());
    ackPlaying = false; try { ackPlayer.pause(); } catch (e) {}   // hand off the speaker from the cue
    player.src = url;
    player.onended = () => { URL.revokeObjectURL(url); finishTurn(); };
    player.onerror = () => finishTurn();
    await player.play().catch(() => finishTurn());
  } catch (e) { finishTurn(); }
}

function finishTurn() {
  stopBargeMonitor();
  busy = false; talkBtn.disabled = false; msgAbort = null; replyStarted = false;
  turnSeq++;                                    // a still-pending ack for this turn won't play
  clearAckTimer();
  ackPlaying = false; try { ackPlayer.pause(); } catch (e) {}   // silence a cue still sounding (tool-only / empty reply)
  if (ackAbort) { try { ackAbort.abort(); } catch (e) {} ackAbort = null; }
  if (auto) relisten(); else setStatus('Ready');
}
function relisten() { setStatus('Listening…'); setTimeout(() => { if (auto && !busy && !recorder) startRec(true); }, 200); }

// carclaude's /clear: server drops the live session + auto-recall, the UI wipes the transcript.
// `spoken` true (voice trigger) speaks a short confirmation; false (button) is silent.
async function clearConversation(spoken) {
  cancelRec(); stopBargeMonitor(); stopAck();
  setStatus('Clearing…');
  try { await api('/api/clear', { method: 'POST' }); } catch (e) {}
  resetTranscript();
  addMsg('system', 'Conversation cleared.');
  clearing = false;                                    // we own the completion from here
  if (spoken) return speak('Conversation cleared.');   // speaks, then finishTurn (relisten if auto)
  busy = false; talkBtn.disabled = false; msgAbort = null;
  if (auto) relisten(); else setStatus('Conversation cleared.');
}

function stopAll() {
  auto = false; autoBtn.classList.remove('on');
  holding = false;
  cancelRec(); stopBargeMonitor(); stopAck();
  if (msgAbort) { try { msgAbort.abort(); } catch (e) {} msgAbort = null; }
  api('/api/interrupt', { method: 'POST' }).catch(() => {});
  try { player.pause(); } catch (e) {}
  busy = false; talkBtn.disabled = false; setStatus('Stopped');
}

// ----------------------------------------------------------------- controls
async function beginPress() {
  try { await ensureMic(); } catch (e) { setStatus('Allow microphone access'); holding = false; return; }
  primeAudio();
  if (!holding) return;                 // released during mic setup — don't orphan a recording
  if (busy) interruptActive();
  startRec(auto ? true : false);
}
talkBtn.addEventListener('pointerdown', (e) => {
  e.preventDefault();
  holding = true;
  try { talkBtn.setPointerCapture(e.pointerId); } catch (_) {}
  beginPress();
});
function endPress(e) {
  if (e) e.preventDefault();
  holding = false;
  if (recorder && !auto) stopRec();     // push-to-talk: release submits
}
talkBtn.addEventListener('pointerup', endPress);
talkBtn.addEventListener('pointercancel', endPress);

autoBtn.onclick = async () => {
  if (auto) { auto = false; autoBtn.classList.remove('on'); cancelRec(); stopBargeMonitor(); setStatus('Ready'); return; }
  try { await ensureMic(); } catch (e) { setStatus('Allow microphone access'); return; }
  primeAudio();
  auto = true; autoBtn.classList.add('on');
  if (!busy && !recorder) startRec(true);
};

stopBtn.onclick = stopAll;
// Clear wipes the conversation, so it's a tap-to-arm: the first tap turns it red ("Sure?"),
// a second tap within 3s confirms — guarding against a blind mis-tap while reaching for Stop.
// (The spoken "/clear" is already an explicit whole-utterance command and needs no second step.)
let clearTimer = null;
function disarmClear() {
  if (clearTimer) { clearTimeout(clearTimer); clearTimer = null; }
  clearBtn.classList.remove('arm'); clearBtn.textContent = 'Clear';
}
clearBtn.onclick = () => {
  if (!clearTimer) {                                   // first tap: arm
    clearBtn.classList.add('arm'); clearBtn.textContent = 'Sure?';
    clearTimer = setTimeout(disarmClear, 3000);
    return;
  }
  disarmClear();                                       // second tap: confirm + reset
  clearing = true;
  if (busy) interruptActive();    // barge in on a turn in flight, then reset
  clearConversation(false);
};

// ----------------------------------------------------------------- persona + config + usage
async function loadPersonas() {
  const sel = $('personaSelect');
  if (!sel) return;
  try {
    const data = await (await api('/api/personalities')).json();
    const personas = data.personas || [];
    sel.innerHTML = '';
    personas.forEach((p) => {
      const o = document.createElement('option'); o.value = p.id; o.textContent = p.name; sel.appendChild(o);
    });
    if (!personas.length) { sel.classList.add('hidden'); return; }
    sel.classList.remove('hidden');
    let active = data.active;
    if (!active) { active = personas[0].id; await selectPersona(active, true); }
    sel.value = active;
  } catch (e) {}
}
async function selectPersona(id, silent) {
  try {
    const d = await (await api('/api/personality', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id })
    })).json();
    if (!silent) setStatus('Switched to ' + (d.name || id) + ' — applies on the next reply');
    clearAckCache();              // new persona = new voice -> re-synthesize the ack cue
  } catch (e) {}
}
const personaSel = $('personaSelect');
if (personaSel) personaSel.onchange = (e) => selectPersona(e.target.value, false);

async function loadConfig() {
  try {
    const c = await (await api('/api/config')).json();
    if (!c) return;
    if (c.pause_ms) vadPauseMs = c.pause_ms;
    if (c.max_ms) vadMaxMs = c.max_ms;
    if (c.barge_sensitivity) bargeSensitivity = c.barge_sensitivity;
    if (typeof c.speech_floor === 'number') vadSpeechFloor = c.speech_floor;
    if (typeof c.status_ack === 'boolean') ackEnabled = c.status_ack;
    if (typeof c.ack_thinking === 'boolean') ackThinking = c.ack_thinking;
    if (typeof c.ack_delay_ms === 'number') ackDelayMs = c.ack_delay_ms;
    if (c.ack_phrase && c.ack_phrase !== ackPhrase) { ackPhrase = c.ack_phrase; clearAckCache(); }
    meta.textContent = (c.model || '') + ' · ' + (c.stt || '') + '/' + (c.tts || '');
  } catch (e) {}
}
function kfmt(n) { n = +n || 0; return n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : '' + n; }
async function refreshUsage() {
  const el = $('usage'); if (!el) return;
  try {
    const u = await (await api('/api/usage')).json();
    const parts = [];
    if (u.claude) {
      let s = 'Claude $' + (u.claude.cost_usd || 0).toFixed(2) + ' · ' + kfmt(u.claude.tokens) + ' tok';
      if (u.claude.budget_usd > 0) s += ' · today $' + (u.claude.today_usd || 0).toFixed(2) + '/$' + u.claude.budget_usd.toFixed(0);
      else if (u.claude.today_usd) s += ' · today $' + (u.claude.today_usd || 0).toFixed(2);
      parts.push(s);
    }
    if (u.elevenlabs) parts.push('11Labs ' + kfmt(u.elevenlabs.used) + '/' + kfmt(u.elevenlabs.limit) + ' chars');
    if (u.deepgram) {
      const dg = (u.deepgram.balance != null) ? '$' + (+u.deepgram.balance).toFixed(2) + ' left'
        : (u.deepgram.requests || 0) + ' STT';
      parts.push('Deepgram ' + dg);
    }
    el.textContent = parts.join('   ·   ') || '—';
  } catch (e) {}
}

function startApp() {
  talkBtn.disabled = false;
  setStatus('Ready');
  loadPersonas(); loadConfig(); refreshUsage();
  if (!startApp._timer) startApp._timer = setInterval(() => { refreshUsage(); loadConfig(); }, 60000);
}

async function init() {
  if (!token) { showToken(); return; }
  startApp();
}
init();
