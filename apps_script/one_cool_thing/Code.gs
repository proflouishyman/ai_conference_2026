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
 */

var FOLDER_NAME = 'AI-History-2026 One Cool Thing (inbox)';
var MAX_VIDEO_BYTES = 40 * 1024 * 1024;
var MAX_DURATION_S = 95;
var MAX_ITEMS = 200;
var MAX_INBOX_BYTES = 3 * 1024 * 1024 * 1024;
var MIME_EXT = { 'video/mp4': 'mp4', 'video/webm': 'webm' };
var ID_RE = /^\d{8}-\d{6}-[0-9a-f]{6}$/;

function doGet() {
  return json_({ ok: false, error: 'POST only' });
}

function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) throw new Error('empty request body');
    var body;
    try { body = JSON.parse(e.postData.contents); }
    catch (err) { throw new Error('body is not valid JSON'); }
    if (body === null || typeof body !== 'object' || Array.isArray(body)) throw new Error('body must be a JSON object');
    if (body.action !== undefined) return json_(serverApi_(body));
    return json_(upload_(body));
  } catch (err) {
    console.error('doPost failed: ' + err + (err && err.stack ? '\n' + err.stack : ''));
    return json_({ ok: false, error: String(err && err.message ? err.message : err) });
  }
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function isStr_(v) { return typeof v === 'string'; }

var MODES = ['video', 'transcript_only', 'text'];
var MAX_TEXT_CHARS = 2000;
var MAX_CONSENT_TEXT = 1000;

function validate_(b) {
  // v1 clients send no mode and mean "video". v2 clients must send mode.
  if (b.v !== 1 && b.v !== 2) throw new Error('unsupported version: v must be 1 or 2');
  if (b.mode === undefined) {
    if (b.v !== 1) throw new Error('mode is required for v2');
  } else if (!isStr_(b.mode) || MODES.indexOf(b.mode) < 0) {
    throw new Error('mode must be video, transcript_only or text');
  }
  var mode = b.mode === undefined ? 'video' : b.mode;
  if (!isStr_(b.name) || !b.name.trim()) throw new Error('name is required');
  if (b.name.length > 200) throw new Error('name too long');
  if (!isStr_(b.email) || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(b.email.trim())) throw new Error('a valid email is required');
  if (b.email.length > 254) throw new Error('email too long');
  if (b.institution !== undefined && b.institution !== null && !isStr_(b.institution)) throw new Error('institution must be a string');
  if (b.institution && b.institution.length > 300) throw new Error('institution too long');
  if (b.consent !== true) throw new Error('consent must be true');
  if (b.consent_text !== undefined && (!isStr_(b.consent_text) || b.consent_text.length > MAX_CONSENT_TEXT)) throw new Error('consent_text must be a string of at most ' + MAX_CONSENT_TEXT + ' characters');
  if (b.user_agent !== undefined && !isStr_(b.user_agent)) throw new Error('user_agent must be a string');
  if (b.submitted_at !== undefined && (!isStr_(b.submitted_at) || isNaN(Date.parse(b.submitted_at)))) throw new Error('submitted_at must be an ISO date string');
  if (mode === 'text') {
    if (b.video_b64 !== undefined || b.mime !== undefined || b.duration_s !== undefined) throw new Error('text mode must not include video_b64, mime or duration_s');
    if (!isStr_(b.text)) throw new Error('text is required');
    var t = b.text.trim();
    if (t.length < 1) throw new Error('text is required');
    if (t.length > MAX_TEXT_CHARS) throw new Error('text too long: ' + t.length + ' > ' + MAX_TEXT_CHARS + ' characters');
    return;
  }
  if (b.text !== undefined) throw new Error('text is only allowed in text mode');
  if (typeof b.duration_s !== 'number' || !isFinite(b.duration_s) || b.duration_s <= 0) throw new Error('duration_s must be a positive number');
  if (b.duration_s > MAX_DURATION_S) throw new Error('video too long: duration_s ' + b.duration_s + ' > ' + MAX_DURATION_S);
  if (!isStr_(b.mime) || !MIME_EXT.hasOwnProperty(b.mime)) throw new Error('mime must be video/webm or video/mp4');
  if (!isStr_(b.video_b64) || !b.video_b64.length) throw new Error('video_b64 is required');
  if (Math.floor(b.video_b64.length * 3 / 4) > MAX_VIDEO_BYTES + 3) throw new Error('video too large (limit 40MB)');
}

function upload_(b) {
  validate_(b);
  var mode = b.mode === undefined ? 'video' : b.mode;
  var bytes = null, text = null;
  if (mode === 'text') {
    text = b.text.trim();
  } else {
    bytes = Utilities.base64Decode(b.video_b64);
    if (!bytes.length) throw new Error('video decoded to zero bytes');
    if (bytes.length > MAX_VIDEO_BYTES) throw new Error('video too large (limit 40MB)');
  }

  var lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) throw new Error('server busy, try again in a moment');
  try {
    var folder = getFolder_();
    var st = inboxStats_(folder);
    if (st.items > MAX_ITEMS || st.bytes > MAX_INBOX_BYTES) {
      console.error('inbox full: ' + JSON.stringify(st));
      throw new Error('inbox full');
    }
    var now = new Date();
    var rand = '';
    for (var i = 0; i < 6; i++) rand += '0123456789abcdef'.charAt(Math.floor(Math.random() * 16));
    var id = Utilities.formatDate(now, 'America/New_York', 'yyyyMMdd-HHmmss') + '-' + rand;
    var meta = {
      id: id, v: b.v, mode: mode,
      name: b.name.trim(), email: b.email.trim(), institution: (b.institution || '').trim(),
      consent: true, consent_text: b.consent_text || '',
      user_agent: (b.user_agent || '').slice(0, 500), submitted_at: b.submitted_at || '',
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
  if (!expected) throw new Error('server API disabled: SERVER_SECRET is not set');
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
  if (!secretOk_(b.secret)) throw new Error('bad secret');
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
  throw new Error('unknown action: ' + b.action);
}

/** Run once from the Apps Script editor: grants the Drive permission and creates the inbox folder. */
function setup() {
  var f = getFolder_();
  console.log('Inbox folder ready: ' + f.getName() + ' (' + f.getId() + ')');
}
