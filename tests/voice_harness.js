// Drives the pure parts of trading_agent/ui/voice.js (state machine, word mapping, history cap, chips) and the voice choice and
// reading-aloud queue of trading_agent/ui/ask.js with stubs. No browser, no network, no microphone.
// Prints {"n": checks run, "failures": [...]} for tests/test_voice.py.
const assert = require("assert");
const path = require("path");
const UI = path.join(__dirname, "..", "trading_agent", "ui");

const failures = []; let n = 0; const queue = [];
function check(name, fn) { queue.push([name, fn]); }

// ---- voice.js core (no window, so only the helpers load) ----
const V = require(path.join(UI, "voice.js"));

check("state machine: the normal run", () => {
  let s = "idle";
  for (const [ev, want] of [["mic", "listening"], ["heard", "thinking"], ["result", "speaking"], ["spoken", "done"], ["mic", "listening"], ["stop", "idle"]]) {
    s = V.transition(s, ev); assert.strictEqual(s, want, ev);
  }
});
check("state machine: typed question skips listening", () => {
  assert.strictEqual(V.transition("idle", "submit"), "thinking");
  assert.strictEqual(V.transition("done", "submit"), "thinking");
  assert.strictEqual(V.transition("speaking", "submit"), "thinking");
});
check("state machine: errors and mic blocked", () => {
  assert.strictEqual(V.transition("listening", "fail"), "error");
  assert.strictEqual(V.transition("thinking", "fail"), "error");
  assert.strictEqual(V.transition("speaking", "fail"), "error");
  assert.strictEqual(V.transition("error", "mic"), "listening");      // Try again
  assert.strictEqual(V.transition("error", "dismiss"), "idle");       // Type instead
  assert.strictEqual(V.transition("error", "submit"), "thinking");
});
check("state machine: close from anywhere, unknown events change nothing", () => {
  for (const s of Object.keys(V.TRANSITIONS)) assert.strictEqual(V.transition(s, "close"), "idle", s);
  assert.strictEqual(V.transition("thinking", "spoken"), "thinking");
  assert.strictEqual(V.transition("idle", "result"), "idle");
  assert.strictEqual(V.transition("nonsense", "mic"), "nonsense");
  assert.strictEqual(V.transition("idle", undefined), "idle");
});
check("state machine: speaking can be stopped or replayed", () => {
  assert.strictEqual(V.transition("speaking", "stop"), "done");
  assert.strictEqual(V.transition("done", "replay"), "speaking");
});

check("wordIndexAt maps a character to the word being read", () => {
  const t = "Today your portfolio is down 19,000 rupees.";
  assert.strictEqual(V.wordIndexAt(t, 0), 0);
  assert.strictEqual(V.wordIndexAt(t, 4), 0);        // the space after "Today" still belongs to it
  assert.strictEqual(V.wordIndexAt(t, 6), 1);
  assert.strictEqual(V.wordIndexAt(t, 11), 2);
  assert.strictEqual(V.wordIndexAt(t, t.length + 50), 6);
  assert.strictEqual(V.wordIndexAt(t, -5), 0);
  assert.strictEqual(V.wordIndexAt("", 3), 0);
  assert.strictEqual(V.wordIndexAt("  two  words ", 7), 1);
  assert.strictEqual(V.wordIndexAt(undefined, 3), 0);
});
check("layout gives every word and where each piece starts", () => {
  const l = V.layout(["One two.", "Three four five.", ""]);
  assert.deepStrictEqual(l.words, ["One", "two.", "Three", "four", "five."]);
  assert.deepStrictEqual(l.starts, [0, 2, 5]);
  assert.deepStrictEqual(V.layout(null), {words: [], starts: []});
  // the global index of a boundary in piece 1 at char 6 ("four")
  assert.strictEqual(l.starts[1] + V.wordIndexAt("Three four five.", 6), 3);
});
check("history keeps the last six turns", () => {
  let h = [];
  for (let i = 1; i <= 9; i++) h = V.capHistory(h, {q: "q" + i});
  assert.strictEqual(h.length, V.MAX_TURNS); assert.strictEqual(V.MAX_TURNS, 6);
  assert.deepStrictEqual(h.map(x => x.q), ["q4", "q5", "q6", "q7", "q8", "q9"]);
  const before = h.slice(); V.capHistory(h, {q: "x"}); assert.deepStrictEqual(h, before);   // the old list is not changed
  assert.deepStrictEqual(V.capHistory(null, {q: "a"}), [{q: "a"}]);
  assert.strictEqual(V.capHistory([1, 2, 3], 4, 2).length, 2);
});
check("follow-up chips come from the intent", () => {
  const labels = (r) => V.chipsFor(r).map(c => c.label);
  assert.deepStrictEqual(labels({intent: "news_today", facts: [{sym: "INFY"}]}), ["Read it again", "Open INFY"]);
  assert.deepStrictEqual(labels({intent: "news_today", scope: "stock", ticker: "TCS", facts: []}), ["Read it again", "Open TCS", "Market news"]);
  assert.deepStrictEqual(labels({intent: "portfolio_summary", topic: "pnl_today"}), ["Top gainer today", "Cash", "Open portfolio"]);
  assert.deepStrictEqual(labels({intent: "portfolio_summary", topic: "cash"}), ["Top gainer today", "Open portfolio"]);
  assert.deepStrictEqual(labels({intent: "open_stock", ticker: "TCS"}), ["News on TCS", "Show chart"]);
  assert.deepStrictEqual(labels({intent: "refused"}), ["Read the news", "Profit and loss today", "How is my portfolio"]);
  assert.strictEqual(V.chipsFor({intent: "news_today", facts: []})[0].action, "replay");
  for (const c of V.chipsFor({intent: "unknown"})) assert.ok(!/buy|sell|order/i.test(c.label + c.text), c.label);
  assert.ok(V.chipsFor(null).length > 0);
});

// ---- ask.js: the voice choice and the reading queue, with a stubbed speech engine ----
const store = {};
global.localStorage = {getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; }};
const spoken = [];
function install(voices) {
  spoken.length = 0;
  global.window = {
    TA: {esc: (s) => String(s), api: async () => ({})},
    speechSynthesis: {voices, getVoices() { return this.voices; }, cancel() {}, speak(u) { spoken.push(u); setTimeout(() => u.onend && u.onend({}), 0); }, addEventListener() {}, removeEventListener() {}},
    SpeechSynthesisUtterance: function (t) { this.text = t; },
  };
  global.document = {body: null, getElementById: () => null, createElement: () => ({})};
  delete require.cache[require.resolve(path.join(UI, "ask.js"))];
  require(path.join(UI, "ask.js"));
  return global.window.TA.ask;
}
const V0 = (name, lang, uri) => ({name, lang, voiceURI: uri || name});
const ask0 = install([]);

check("pickVoice: Indian English beats British English beats any English", () => {
  const p = ask0.pickVoice;
  const gb = V0("Daniel", "en-GB"), us = V0("Samantha", "en-US"), ind = V0("Rishi", "en-IN"), fr = V0("Amelie", "fr-FR");
  assert.strictEqual(p([gb, us, ind, fr], ""), ind);
  assert.strictEqual(p([us, gb, fr], ""), gb);
  assert.strictEqual(p([fr, us], ""), us);
  assert.strictEqual(p([fr, V0("Google हिन्दी", "hi-IN")], ""), null);   // no English: the default voice
});
check("pickVoice: odd language spellings", () => {
  const p = ask0.pickVoice;
  const under = V0("A", "en_IN"), lower = V0("B", "en-in"), gb = V0("C", "EN-gb");
  assert.strictEqual(p([gb, under], ""), under);
  assert.strictEqual(p([gb, lower], ""), lower);
  assert.strictEqual(p([V0("D", "en")], "").name, "D");
});
check("pickVoice: a natural-sounding Indian voice wins among several", () => {
  const p = ask0.pickVoice;
  const plain = V0("English India", "en-IN"), neural = V0("Microsoft Neerja Online (Natural) - English (India)", "en-IN");
  assert.strictEqual(p([plain, neural], ""), neural);
  assert.strictEqual(p([plain], ""), plain);
});
check("pickVoice: the saved voice wins while it exists; a missing one falls back", () => {
  const p = ask0.pickVoice;
  const gb = V0("Daniel", "en-GB", "uri-gb"), ind = V0("Rishi", "en-IN", "uri-in");
  assert.strictEqual(p([gb, ind], "uri-gb"), gb);          // by voiceURI
  assert.strictEqual(p([gb, ind], "Daniel"), gb);          // by name
  assert.strictEqual(p([gb, ind], "uri-gone"), ind);       // saved voice no longer installed
});
check("pickVoice: empty and odd input", () => {
  const p = ask0.pickVoice;
  assert.strictEqual(p([], ""), null); assert.strictEqual(p(null, "x"), null); assert.strictEqual(p(undefined), null);
  assert.strictEqual(p([null, {name: "no lang"}, V0("X", "en-IN")], "").name, "X");
});

check("speak: the utterance takes its language and voice from the chosen voice", async () => {
  const ind = V0("Rishi", "en-IN"), gb = V0("Daniel", "en-GB");
  let ask = install([gb, ind]);
  assert.strictEqual(ask.speak(["Hello there."]), true);
  await new Promise(r => setTimeout(r, 5));
  assert.strictEqual(spoken.length, 1); assert.strictEqual(spoken[0].lang, "en-IN"); assert.strictEqual(spoken[0].voice, ind);
  assert.strictEqual(spoken[0].rate, 1.0);
  ask = install([gb, V0("Samantha", "en-US")]);
  ask.speak("Cheerio."); await new Promise(r => setTimeout(r, 5));
  assert.strictEqual(spoken[0].lang, "en-GB"); assert.strictEqual(spoken[0].voice.name, "Daniel");
});
check("speak: one utterance per piece, in order, with hooks", async () => {
  const ask = install([V0("Rishi", "en-IN")]);
  const log = [];
  ask.speak(["One.", "Two."], {onStart: (i) => log.push("s" + i), onEnd: () => log.push("end")});
  await new Promise(r => setTimeout(r, 20));
  assert.deepStrictEqual(spoken.map(u => u.text), ["One.", "Two."]);
  // onStart needs a real engine event; the end hook must fire once
  assert.strictEqual(log.filter(x => x === "end").length, 1);
});
check("speak: a saved voice is used, and a missing one is ignored", async () => {
  const gb = V0("Daniel", "en-GB", "uri-gb"), ind = V0("Rishi", "en-IN", "uri-in");
  let ask = install([gb, ind]);
  ask.setVoice("uri-gb"); assert.strictEqual(store["ta.voice"], "uri-gb");
  ask.speak("Hi."); await new Promise(r => setTimeout(r, 5));
  assert.strictEqual(spoken[0].voice, gb); assert.strictEqual(spoken[0].lang, "en-GB");
  ask.setVoice("uri-gone"); ask = install([gb, ind]);
  ask.speak("Hi."); await new Promise(r => setTimeout(r, 5));
  assert.strictEqual(spoken[0].voice, ind);
  ask.setVoice(""); assert.ok(!("ta.voice" in store));
});
check("voiceInfo: the menu entries, the auto label and the no-English-voice case", () => {
  let ask = install([V0("Daniel", "en-GB", "g"), V0("Rishi", "en-IN", "i"), V0("Amelie", "fr-FR", "f")]);
  let info = ask.voiceInfo();
  assert.deepStrictEqual(info.voices.map(v => v.lang), ["en-GB", "en-IN"]);
  assert.strictEqual(info.auto, "Indian English (auto)"); assert.strictEqual(info.chosen, ""); assert.strictEqual(info.none, false);
  ask = install([V0("Daniel", "en-GB", "g")]);
  assert.strictEqual(ask.voiceInfo().auto, "British English (auto)");
  ask = install([V0("Amelie", "fr-FR", "f")]);
  info = ask.voiceInfo(); assert.strictEqual(info.none, true); assert.deepStrictEqual(info.voices, []);
  assert.strictEqual(ask.speak("Bonjour"), false);          // text only
  ask = install([]);
  assert.strictEqual(ask.voiceInfo().none, false);          // nothing reported yet: not a verdict
  store["ta.voice"] = "i"; ask = install([V0("Rishi", "en-IN", "i")]);
  assert.strictEqual(ask.voiceInfo().chosen, "i"); delete store["ta.voice"];
});
check("sentences splits for speech without breaking 1.86", () => {
  assert.deepStrictEqual(ask0.sentences("Holdings value is 1.86 lakh rupees. Cash is 5,000 rupees."), ["Holdings value is 1.86 lakh rupees.", "Cash is 5,000 rupees."]);
});

(async () => {
  for (const [name, fn] of queue) { n++; try { await fn(); } catch (e) { failures.push(name + ": " + (e && e.message || e)); } }
  console.log(JSON.stringify({n, failures})); process.exit(0);
})();
