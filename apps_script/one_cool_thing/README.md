# One Cool Thing: Apps Script upload inbox

Receives videos from `one-cool-thing.html` and stores them in the Drive folder
"AI-History-2026 One Cool Thing (inbox)" (created automatically on first use).
A ces-server job pulls them through the server API and then deletes them from Drive.
No secrets live in this repo.

## Deploy with clasp

```
cd apps_script/one_cool_thing
clasp login                       # once, in a browser
clasp create --type standalone --title "One Cool Thing inbox" --rootDir .
clasp push --force
clasp deploy --description "v1"
```

`clasp deployments` prints the deployment id. The web app URL is
`https://script.google.com/macros/s/<DEPLOYMENT_ID>/exec`.
After editing code, run `clasp push` then `clasp deploy --deploymentId <ID>` to update the same URL.
First run may need authorization: open the project (`clasp open`), run any function once, and accept the Drive scope.

## Deploy by hand

1. Go to script.google.com, New project.
2. Paste `Code.gs`. Project Settings, tick "Show appsscript.json", paste `appsscript.json`.
3. Deploy, New deployment, type Web app, Execute as: Me, Who has access: Anyone. Authorize.
4. Copy the Web app URL ending in `/exec`.

## Set the server secret

Project Settings, Script properties, Add property: `SERVER_SECRET` = a long random string
(for example `openssl rand -hex 32`). Put the same value only on ces-server. Until it is set the
server API refuses every call.

## Put the URL on the site

Before the first deploy (and after any scope change) open the editor, pick `setup` in the function menu, press Run and accept the Drive permission. Until then the web app answers 403.

Set `APPS_SCRIPT_URL` at the top of the script in `one-cool-thing.html`.

## API

Upload (text/plain JSON): `{v:1,name,email,institution,consent:true,duration_s,mime,user_agent,submitted_at,video_b64}`
returns `{ok:true,id}` or `{ok:false,error}`.

Server (JSON with `secret`): `{action:"list"}`, `{action:"fetch",id}`, `{action:"delete",id,sha256}`.
Delete only trashes the files when `sha256` equals the stored hash.

Limits: 40 MB per video, duration at most 95 s, inbox refuses new uploads above 200 items or 3 GB.
