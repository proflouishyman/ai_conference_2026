// Hostile-input self-test for apps_script/one_cool_thing/Code.gs (stubs the Apps Script services). Run: node scripts/oct_code_gs_selftest.js [path/to/Code.gs]
const fs=require('fs'), vm=require('vm');
const code=fs.readFileSync((process.argv[2]||require('path').join(__dirname,'..','apps_script','one_cool_thing','Code.gs')),'utf8');
const cacheStore={};
const ctx={console, Date, Math, JSON, parseInt, isNaN, isFinite, String, Array, Object, RegExp,
 ContentService:{createTextOutput:(t)=>({t, setMimeType(){return this}}), MimeType:{JSON:1}},
 CacheService:{getScriptCache:()=>({get:k=>cacheStore[k]||null,put:(k,v)=>{cacheStore[k]=v}})},
 Utilities:{base64Decode:(s)=>{const b=Buffer.from(s,'base64');return Array.from(b).map(x=>x>127?x-256:x)}, DigestAlgorithm:{SHA_256:1}, Charset:{UTF_8:1},
   computeDigest:()=>[1,2], formatDate:()=>'20261012-000000', newBlob:(b)=>({b})},
 LockService:{getScriptLock:()=>({tryLock:()=>true,releaseLock(){}})},
 PropertiesService:{getScriptProperties:()=>({getProperty:(k)=>k==='FOLDER_ID'?'F':null,setProperty(){}})},
 DriveApp:{getFolderById:()=>({getFiles:()=>({hasNext:()=>false}), createFile:(n,c)=>({getSize:()=>(n&&n.b)?n.b.length:1})})}};
vm.createContext(ctx); vm.runInContext(code,ctx);
const mp4=Buffer.concat([Buffer.from([0,0,0,24]),Buffer.from('ftypmp42'),Buffer.alloc(40)]).toString('base64');
const webm=Buffer.concat([Buffer.from([0x1a,0x45,0xdf,0xa3]),Buffer.alloc(40)]).toString('base64');
const CT=vm.runInContext('CONSENT_BY_MODE',ctx);
const good=()=>({v:2,mode:'text',name:'A B',email:'a@example.test',institution:'JHU',consent:true,consent_text:CT.text,user_agent:'ua',submitted_at:new Date().toISOString(),text:'hello'});
const goodv=()=>({v:2,mode:'video',name:'A B',email:'a@example.test',institution:'JHU',consent:true,consent_text:CT.video,user_agent:'ua',submitted_at:new Date().toISOString(),duration_s:5,mime:'video/mp4',video_b64:mp4});
function call(o){ const r=ctx.doPost({postData:{contents:typeof o==='string'?o:JSON.stringify(o)}}); return JSON.parse(r.t); }
let fails=0; const t=(n,c)=>{console.log((c?'PASS':'FAIL')+' '+n); if(!c)fails++;};
// reset rate per test by clearing cache
const clr=()=>{for(const k in cacheStore)delete cacheStore[k]};
const rej=(n,o,sub)=>{clr();const r=call(o);t('rejects '+n+' -> '+r.error,r.ok===false&&(!sub||r.error.includes(sub)));};
clr(); let r=call(good()); t('good text accepted',r.ok===true);
clr(); r=call(goodv()); t('good mp4 accepted',r.ok===true);
clr(); let w=goodv(); w.mime='video/webm'; w.video_b64=webm; w.consent_text=CT.video; r=call(w); t('good webm accepted',r.ok===true);
let x;
x=good(); x.email='a@example.test,b@example.test'; rej('email comma',x);
x=good(); x.email='a@example.test;b@example.test'; rej('email semicolon',x);
x=good(); x.email='a@example.test\r\nBcc: e@example.test'; rej('email crlf',x);
x=good(); x.email='<a@example.test>'; rej('email angle',x);
x=good(); x.email='"a"@example.test'; rej('email quote',x);
x=good(); x.email='a b@example.test'; rej('email space',x);
x=good(); x.email='a'.repeat(250)+'@example.test'; rej('email long',x);
x=good(); x.name='n'.repeat(121); rej('name 121',x);
x=good(); x.name='a\nb'; rej('name newline',x);
x=good(); x.institution='i'.repeat(201); rej('inst 201',x);
x=good(); x.institution='$(touch /tmp/x)\u0000'; rej('inst NUL',x);
x=good(); x.consent_text='I agree'; rej('consent_text wrong',x);
x=good(); x.consent_text=CT.video; rej('consent_text wrong mode',x);
x=good(); delete x.consent_text; rej('consent_text missing v2',x);
x=good(); x.text='x'.repeat(2001); rej('text 2001',x);
x=good(); x.text='a\u001b[2Jb\u0000\tc\r\nd'; clr(); r=call(x); t('text control chars stripped, accepted',r.ok===true);
x=goodv(); x.video_b64='AAAA!!!!'; rej('b64 bad chars',x,'base64');
x=goodv(); x.video_b64='AAA'; rej('b64 bad length',x,'base64');
x=goodv(); x.video_b64='A'.repeat(56000000); rej('b64 oversize',x,'too large');
x=goodv(); x.mime='video/webm'; rej('mime/magic mismatch (mp4 bytes as webm)',x,'does not look like');
x=goodv(); x.video_b64=webm; rej('mime/magic mismatch (webm bytes as mp4)',x,'does not look like');
x=goodv(); x.video_b64=Buffer.from('#!/bin/sh\ntouch /tmp/p\n......').toString('base64'); rej('script as mp4',x,'does not look like');
x=goodv(); x.duration_s=96; rej('duration 96',x);
x=goodv(); x.mime='text/html'; rej('mime html',x);
rej('not json','{{{'); rej('array','[]'); rej('oversized body','x'.repeat(60*1024*1024),'too large');
x=good(); x.name={toString(){}}; rej('name object',x);
x=good(); x.name='A'; x.v=3; rej('bad version',x);
// generic error on internal failure
clr(); ctx.DriveApp.getFolderById=()=>{throw new Error('Drive secret detail XYZ')}; r=call(good()); t('internal failure is generic: '+r.error, r.ok===false && !r.error.includes('XYZ'));
// rate limit
clr(); let okc=0,lim=0; for(let i=0;i<70;i++){ ctx.DriveApp.getFolderById=()=>({getFiles:()=>({hasNext:()=>false}), createFile:()=>({getSize:()=>1})}); const q=call(good()); if(q.ok)okc++; else if(/Too many/.test(q.error))lim++; }
t('rate limit: 60 ok then rejected ('+okc+'/'+lim+')', okc===60&&lim===10);
// server api unauth
clr(); r=call({action:'list',secret:'nope'}); t('server api unauthorized generic: '+r.error, r.ok===false&&r.error==='unauthorized');
process.exit(fails?1:0);
