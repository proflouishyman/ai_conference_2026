/**
 * One Cool Thing: upload inbox for the AI and History Conference 2026.
 *
 * Upload  : POST text/plain JSON {v:1|2, mode:"video"|"transcript_only"|"text" (absent = video),
 *           name, email, institution, consent, consent_text, user_agent, submitted_at,
 *           video modes: duration_s, mime, video_b64 ; text mode: text (1..2000 chars)}
 *           ->  {ok:true,id,mode} | {ok:false,error}
 * Server  : POST JSON {action:"list"|"fetch"|"delete", secret, id?, sha256?}
 *           secret must equal Script Property SERVER_SECRET (never stored in the repo).
 * Failures are loud: every error path returns {ok:false,error} and logs.
 *
 * v5 SECURITY: every submitted field is UNTRUSTED DATA. Caps and strict validation below
 * (name 120, institution 200, text 2000, user_agent 300, exact consent wording per mode, strict single
 * email address, strict base64 with magic-byte check). Request size cap and a per-10-minute upload
 * rate limit blunt floods. Public callers only ever see generic errors for internal failures
 * (details go to the execution log); validation messages are fixed strings that never echo input.
 * Never put stored submission text in front of an LLM or agent as instructions.
 */

var FOLDER_NAME = 'AI-History-2026 One Cool Thing (inbox)';
var MAX_VIDEO_BYTES = 40 * 1024 * 1024;
var MAX_DURATION_S = 95;
var MAX_ITEMS = 200;
var MAX_INBOX_BYTES = 3 * 1024 * 1024 * 1024;
var MIME_EXT = { 'video/mp4': 'mp4', 'video/webm': 'webm' };
var ID_RE = /^\d{8}-\d{6}-[0-9a-f]{6}$/;
var MAX_NAME = 120, MAX_INSTITUTION = 200, MAX_UA = 300, MAX_ISO = 64, MAX_EMAIL = 254;
// ~ (40MB * 4/3) base64 plus JSON overhead; bigger bodies are refused before JSON.parse
var MAX_REQUEST_CHARS = Math.ceil((MAX_VIDEO_BYTES + 2) / 3) * 4 + 64 * 1024;
var RATE_LIMIT = 60, RATE_WINDOW_S = 600;
var CONSENT_BY_MODE = {
  video: 'I agree that the AI and History Conference, Johns Hopkins University and the American Historical Association may use this video publicly, including on websites and social media.',
  transcript_only: 'I agree that the AI and History Conference, Johns Hopkins University and the American Historical Association may publish a written transcript or quotations from this recording, including on websites and social media. The video itself will not be shown publicly.',
  text: 'I agree that the AI and History Conference, Johns Hopkins University and the American Historical Association may publish what I wrote, including on websites and social media.'
};
var GENERIC_ERROR = 'Something went wrong on our side. Please try again in a moment, or email lhyman6@jh.edu.';
var CTRL_ALL_RE = /[\u0000-\u001f\u007f\u0085\u2028\u2029]/;
var CTRL_NO_NL_RE = /[\u0000-\u0009\u000b-\u001f\u007f\u0085\u2028\u2029]/g;
var EMAIL_RE = /^[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24}$/;
var B64_RE = /^[A-Za-z0-9+\/]+={0,2}$/;
// v5 review: lone UTF-16 surrogates cannot be encoded as UTF-8 on the server (poison item that fails every
// run); bidi overrides and zero-width/format characters let a name or institution display as something else.
var LONE_SURROGATE_RE = /[\ud800-\udbff](?![\udc00-\udfff])|(?:^|[^\ud800-\udbff])[\udc00-\udfff]/;
var LONE_SURROGATE_G = /[\ud800-\udbff](?![\udc00-\udfff])|([^\ud800-\udbff]|^)[\udc00-\udfff]/g;
var FORMAT_RE = /[\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb]/;
var FORMAT_G = /[\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb]/g;
var ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/;   // what Date.toISOString() emits
var DAILY_LIMIT = 400;   // accepted uploads per New York calendar day (bounds server disk and mail volume)

/** An error whose message is safe and intended for the caller (fixed text, never echoes input). */
function bad_(msg) { var e = new Error(msg); e.pub = true; return e; }

function doGet() {
  return json_({ ok: false, error: 'POST only' });
}

function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) throw bad_('empty request body');
    if (e.postData.contents.length > MAX_REQUEST_CHARS) throw bad_('request too large');
    var body;
    try { body = JSON.parse(e.postData.contents); }
    catch (err) { throw bad_('body is not valid JSON'); }
    if (body === null || typeof body !== 'object' || Array.isArray(body)) throw bad_('body must be a JSON object');
    if (body.action !== undefined) return json_(serverApi_(body));
    rateLimit_();
    return json_(upload_(body));
  } catch (err) {
    console.error('doPost failed: ' + err + (err && err.stack ? '\n' + err.stack : ''));
    return json_({ ok: false, error: (err && err.pub) ? String(err.message) : GENERIC_ERROR });
  }
}

/** Crude flood brake: at most RATE_LIMIT upload attempts per RATE_WINDOW_S across everyone. */
function rateLimit_() {
  var cache = CacheService.getScriptCache();
  var key = 'rl_' + Math.floor(Date.now() / (RATE_WINDOW_S * 1000));
  var n = parseInt(cache.get(key) || '0', 10) + 1;
  cache.put(key, String(n), RATE_WINDOW_S + 60);
  if (n > RATE_LIMIT) {
    console.error('rate limit exceeded: ' + n + ' uploads in the current ' + RATE_WINDOW_S + ' s window');
    throw bad_('Too many uploads right now. Please try again in a few minutes.');
  }
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function isStr_(v) { return typeof v === 'string'; }

var MODES = ['video', 'transcript_only', 'text'];
var MAX_TEXT_CHARS = 2000;
var MAX_CONSENT_TEXT = 1000;

function cleanLineOk_(v, max) {
  return isStr_(v) && v.length <= max && !CTRL_ALL_RE.test(v) && !LONE_SURROGATE_RE.test(v) && !FORMAT_RE.test(v);
}

function validate_(b) {
  // v1 clients send no mode and mean "video". v2 clients must send mode.
  if (b.v !== 1 && b.v !== 2) throw bad_('unsupported version: v must be 1 or 2');
  if (b.mode === undefined) {
    if (b.v !== 1) throw bad_('mode is required for v2');
  } else if (!isStr_(b.mode) || MODES.indexOf(b.mode) < 0) {
    throw bad_('mode must be video, transcript_only or text');
  }
  var mode = b.mode === undefined ? 'video' : b.mode;
  if (!isStr_(b.name) || !b.name.trim()) throw bad_('name is required');
  if (!cleanLineOk_(b.name, MAX_NAME)) throw bad_('name must be at most ' + MAX_NAME + ' characters with no line breaks or control characters');
  if (!isStr_(b.email) || b.email.length > MAX_EMAIL || b.email.indexOf('..') >= 0 || !EMAIL_RE.test(b.email.trim()) || b.email !== b.email.trim()) {
    throw bad_('a valid single email address is required (letters, digits and . _ % + - only)');
  }
  if (b.institution !== undefined && b.institution !== null) {
    if (!cleanLineOk_(b.institution, MAX_INSTITUTION)) throw bad_('institution must be at most ' + MAX_INSTITUTION + ' characters with no line breaks or control characters');
  }
  if (b.consent !== true) throw bad_('consent must be true');
  if (b.consent_text === undefined) {
    if (b.v !== 1) throw bad_('consent_text is required');
  } else if (!isStr_(b.consent_text) || b.consent_text !== CONSENT_BY_MODE[mode]) {
    throw bad_('consent_text does not match the consent wording for this mode');
  }
  if (b.user_agent !== undefined && !isStr_(b.user_agent)) throw bad_('user_agent must be a string');
  if (b.submitted_at !== undefined && (!isStr_(b.submitted_at) || b.submitted_at.length > MAX_ISO || !ISO_RE.test(b.submitted_at) || isNaN(Date.parse(b.submitted_at)))) throw bad_('submitted_at must be an ISO date string');
  if (mode === 'text') {
    if (b.video_b64 !== undefined || b.mime !== undefined || b.duration_s !== undefined) throw bad_('text mode must not include video_b64, mime or duration_s');
    if (!isStr_(b.text)) throw bad_('text is required');
    if (LONE_SURROGATE_RE.test(b.text)) throw bad_('text contains an invalid character');
    var t = cleanText_(b.text);
    if (t.length < 1) throw bad_('text is required');
    if (t.length > MAX_TEXT_CHARS) throw bad_('text too long: at most ' + MAX_TEXT_CHARS + ' characters');
    return;
  }
  if (b.text !== undefined) throw bad_('text is only allowed in text mode');
  if (typeof b.duration_s !== 'number' || !isFinite(b.duration_s) || b.duration_s <= 0) throw bad_('duration_s must be a positive number');
  if (b.duration_s > MAX_DURATION_S) throw bad_('video too long: at most ' + MAX_DURATION_S + ' seconds');
  if (!isStr_(b.mime) || !MIME_EXT.hasOwnProperty(b.mime)) throw bad_('mime must be video/webm or video/mp4');
  if (!isStr_(b.video_b64) || !b.video_b64.length) throw bad_('video_b64 is required');
  // size cap BEFORE decoding; strict alphabet and padding
  if (b.video_b64.length > Math.ceil((MAX_VIDEO_BYTES + 2) / 3) * 4) throw bad_('video too large (limit 40MB)');
  if (b.video_b64.length % 4 !== 0 || !B64_RE.test(b.video_b64)) throw bad_('video is not valid base64');
}

/** Typed text: CRLF to LF, tabs to spaces, every other control and bidi/format character removed, then trimmed. */
function cleanText_(t) {
  return t.replace(/\r\n?/g, '\n').replace(/\t/g, ' ').replace(CTRL_NO_NL_RE, '').replace(FORMAT_G, '').trim();
}

/** Free-text header we keep but never trust (user agent): one line, no format characters, no lone surrogates. */
function cleanUa_(v) {
  return (v || '').replace(CTRL_NO_NL_RE, ' ').replace(/\n/g, ' ').replace(FORMAT_G, '').slice(0, MAX_UA)
    .replace(LONE_SURROGATE_G, '$1');
}

/** Counts accepted uploads per New York day in Script Properties. Caller holds the script lock. */
function dailyCap_(now) {
  var props = PropertiesService.getScriptProperties();
  var day = Utilities.formatDate(now, 'America/New_York', 'yyyyMMdd');
  var cur = (props.getProperty('DAILY_COUNT') || '').split(':');
  var n = (cur[0] === day ? parseInt(cur[1], 10) || 0 : 0) + 1;
  if (n > DAILY_LIMIT) {
    console.error('daily upload limit reached: ' + DAILY_LIMIT);
    throw bad_('We have reached today\'s limit for submissions. Please try again tomorrow, or email lhyman6@jh.edu.');
  }
  props.setProperty('DAILY_COUNT', day + ':' + n);
}

/** 6 hex characters from a cryptographically random UUID (Math.random is predictable). */
function randHex6_() {
  var u = Utilities.getUuid().replace(/-/g, '').toLowerCase();
  if (!/^[0-9a-f]{32}$/.test(u)) throw new Error('getUuid returned an unexpected value');
  return u.slice(0, 6);
}

/** mp4 has "ftyp" at offset 4; webm starts with the EBML magic 1A 45 DF A3 (bytes are signed here). */
function magicOk_(bytes, mime) {
  if (mime === 'video/mp4') return bytes.length >= 12 && bytes[4] === 0x66 && bytes[5] === 0x74 && bytes[6] === 0x79 && bytes[7] === 0x70;
  if (mime === 'video/webm') return bytes.length >= 4 && (bytes[0] & 255) === 0x1a && (bytes[1] & 255) === 0x45 && (bytes[2] & 255) === 0xdf && (bytes[3] & 255) === 0xa3;
  return false;
}

function upload_(b) {
  validate_(b);
  var mode = b.mode === undefined ? 'video' : b.mode;
  var bytes = null, text = null;
  if (mode === 'text') {
    text = cleanText_(b.text);
  } else {
    try { bytes = Utilities.base64Decode(b.video_b64); }
    catch (err) { throw bad_('video is not valid base64'); }
    if (!bytes.length) throw bad_('video decoded to zero bytes');
    if (bytes.length > MAX_VIDEO_BYTES) throw bad_('video too large (limit 40MB)');
    if (!magicOk_(bytes, b.mime)) throw bad_('the file does not look like a ' + b.mime + ' recording');
  }

  var lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) throw bad_('server busy, try again in a moment');
  try {
    var folder = getFolder_();
    var st = inboxStats_(folder);
    if (st.items > MAX_ITEMS || st.bytes > MAX_INBOX_BYTES) {
      console.error('inbox full: ' + JSON.stringify(st));
      throw bad_('The inbox is full right now. Please email lhyman6@jh.edu.');
    }
    var now = new Date();
    var id = Utilities.formatDate(now, 'America/New_York', 'yyyyMMdd-HHmmss') + '-' + randHex6_();
    if (findFile_(folder, id + '.json')) id = Utilities.formatDate(now, 'America/New_York', 'yyyyMMdd-HHmmss') + '-' + randHex6_();
    if (findFile_(folder, id + '.json')) throw new Error('id collision twice for ' + id);
    dailyCap_(now);
    var meta = {
      id: id, v: b.v, mode: mode,
      name: b.name.trim(), email: b.email.trim(), institution: (b.institution || '').trim(),
      consent: true, consent_text: b.consent_text || '',
      user_agent: cleanUa_(b.user_agent), submitted_at: b.submitted_at || '',
      received_at: now.toISOString()
    };
    if (mode === 'text') {
      meta.text = text;
      meta.bytes = 0;
      meta.sha256 = hex_(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, text, Utilities.Charset.UTF_8));
      var mf0 = folder.createFile(id + '.json', JSON.stringify(meta), 'application/json');
      if (!mf0.getSize()) throw new Error('stored metadata is empty');
      return { ok: true, id: id, mode: mode };
    }
    var ext = MIME_EXT[b.mime];
    meta.duration_s = b.duration_s; meta.mime = b.mime;
    meta.bytes = bytes.length;
    meta.sha256 = hex_(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, bytes));
    var vf = folder.createFile(Utilities.newBlob(bytes, b.mime, id + '.' + ext));
    var mf = folder.createFile(id + '.json', JSON.stringify(meta), 'application/json');
    // read back to confirm both files exist with the right size
    if (vf.getSize() !== bytes.length) throw new Error('stored video size mismatch');
    if (!mf.getSize()) throw new Error('stored metadata is empty');
    return { ok: true, id: id, mode: mode };
  } finally {
    lock.releaseLock();
  }
}

function hex_(digest) {
  return digest.map(function (x) { return ('0' + (x < 0 ? x + 256 : x).toString(16)).slice(-2); }).join('');
}

function getFolder_() {
  var props = PropertiesService.getScriptProperties();
  var id = props.getProperty('FOLDER_ID');
  if (id) return DriveApp.getFolderById(id); // throws loudly if missing
  var it = DriveApp.getFoldersByName(FOLDER_NAME);
  var folder = it.hasNext() ? it.next() : DriveApp.createFolder(FOLDER_NAME);
  props.setProperty('FOLDER_ID', folder.getId());
  return folder;
}

function inboxStats_(folder) {
  var items = 0, bytes = 0, files = folder.getFiles();
  while (files.hasNext()) {
    var f = files.next();
    bytes += f.getSize();
    if (/\.json$/.test(f.getName())) items++;
  }
  return { items: items, bytes: bytes };
}

function secretOk_(given) {
  var expected = PropertiesService.getScriptProperties().getProperty('SERVER_SECRET');
  if (!expected) { console.error('server API disabled: SERVER_SECRET is not set'); return false; }
  if (!isStr_(given)) return false;
  var diff = given.length ^ expected.length;
  for (var i = 0; i < expected.length; i++) {
    diff |= (given.charCodeAt(i % Math.max(given.length, 1)) || 0) ^ expected.charCodeAt(i);
  }
  return diff === 0;
}

function findFile_(folder, name) {
  var it = folder.getFilesByName(name);
  return it.hasNext() ? it.next() : null;
}

function serverApi_(b) {
  if (!secretOk_(b.secret)) throw bad_('unauthorized');
  // Authenticated caller (ces-server): detailed errors are returned so its failures stay loud.
  try { return serverApiAuthed_(b); }
  catch (err) { err.pub = true; throw err; }
}

function serverApiAuthed_(b) {
  var folder = getFolder_();
  if (b.action === 'list') {
    var items = [], files = folder.getFiles();
    while (files.hasNext()) {
      var f = files.next();
      var n = f.getName();
      if (!/\.json$/.test(n)) continue;
      items.push({ id: n.replace(/\.json$/, ''), json: JSON.parse(f.getBlob().getDataAsString()) });
    }
    items.sort(function (a, c) { return a.id < c.id ? -1 : 1; });
    return { ok: true, items: items };
  }
  if (!isStr_(b.id) || !ID_RE.test(b.id)) throw new Error('invalid id');
  var mf = findFile_(folder, b.id + '.json');
  if (!mf) throw new Error('no such id: ' + b.id);
  var meta = JSON.parse(mf.getBlob().getDataAsString());
  var mode = meta.mode === undefined ? 'video' : meta.mode;
  var vf = null;
  if (mode !== 'text') vf = findFile_(folder, b.id + '.' + MIME_EXT[meta.mime]);
  if (b.action === 'fetch') {
    if (mode === 'text') {
      if (!isStr_(meta.text)) throw new Error('text missing in metadata for id ' + b.id);
      return { ok: true, id: b.id, mode: 'text', sha256: meta.sha256, text: meta.text };
    }
    if (!vf) throw new Error('video file missing for id ' + b.id);
    return { ok: true, id: b.id, mode: mode, mime: meta.mime, sha256: meta.sha256, video_b64: Utilities.base64Encode(vf.getBlob().getBytes()) };
  }
  if (b.action === 'delete') {
    if (!isStr_(b.sha256) || b.sha256 !== meta.sha256) throw new Error('sha256 mismatch, not deleting');
    // Permanent delete (not trash): trashed files still count against Drive storage.
    if (vf) Drive.Files.remove(vf.getId());
    Drive.Files.remove(mf.getId());
    return { ok: true, id: b.id, deleted: true };
  }
  throw new Error('unknown action');
}

/** Run once from the Apps Script editor: grants the Drive permission and creates the inbox folder. */
function setup() {
  var f = getFolder_();
  console.log('Inbox folder ready: ' + f.getName() + ' (' + f.getId() + ')');
}
