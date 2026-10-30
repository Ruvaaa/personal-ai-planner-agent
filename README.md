# Personal AI Agent Desktop

This project is a local-first productivity desktop app that combines a Python FastAPI backend, a React/Vite frontend, and a Tauri shell. It keeps task data and Google Calendar authentication on-device, while using Gemini Flash for chat responses and planning support.

## Requirements

- Python 3.11+
- Node.js 20+
- Rust 1.85+ and the platform prerequisites for Tauri
- A Google Gemini API key with access to Gemini Flash

## Repository layout

- `api.py` – loopback-only FastAPI API on `127.0.0.1:8787`
- `gemini_service.py` – Gemini Flash integration and response filtering
- `main.py` – task storage, calendar helpers, and task management logic
- `frontend/` – React + Vite frontend
- `frontend/src-tauri/` – Tauri desktop shell
- Local task, planner, OAuth-token, and settings files are stored in the user's application data directory.
- `credentials.json` is a user-provided Google OAuth client file and is never part of the installer.

## Python setup

Create a virtual environment and install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Copy the example environment configuration and add your Gemini key:

```powershell
Copy-Item .env.example .env
```

Then edit `.env` and set values such as:

```env
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-3.8-flash
```

The backend reads `GEMINI_API_KEY` and `GEMINI_MODEL` from the local `.env` file or active environment. The desktop backend reads `.env` from the application data directory; when running directly from the repository, it reads the repository-root `.env`. The model can also be changed in Settings; that selection is stored separately in `gemini_settings.json` in the same data directory and takes precedence over `GEMINI_MODEL`. Gemini 3.1 Flash-Lite is available as a lower-cost option for routine requests. Do not expose or commit the real key. The frontend never receives the API key.

The Assistant can create tasks, mark tasks complete, generate a daily schedule proposal, and prepare Google Calendar events. It uses structured Gemini output for these actions. Daily plans remain proposals until reviewed in Daily Planner; calendar events are checked for conflicts and are not created until you confirm them in chat. Chat requires a configured Gemini API key; the terminal-only local agent is not used by the desktop API.

Explicit event requests can include a title, date, time, and duration in one message or across a clarification. The assistant prepares a confirmation proposal; it does not write to Google Calendar until you confirm.

Calendar is reported as connected only after the saved OAuth authorization has the required event scope and a Google Calendar API request succeeds. Refreshed tokens are saved in the application data directory. An event is reported as created only after Google returns a matching event with a real event ID; retries use a stable event ID to avoid duplicates. Gemini's live connectivity and model access can be checked with `POST /api/gemini/diagnostics`; provider errors include actionable status information without exposing the API key.

### Workload insight calendar actions

Capacity insights can suggest a free work slot in CAT. Review the proposed time, then choose **Accept & add to Calendar** to recheck availability, schedule the task, and create its Google Calendar event. If the slot is no longer available or the Calendar write fails, the insight is not applied. Other insight types remain informational.

On Windows, the installed desktop app stores persistent files under the Tauri application data directory for `com.personalai.agent` (normally `%APPDATA%\com.personalai.agent`). Put the installed app's `.env` and Google OAuth `credentials.json` there. The app copies known files from its legacy executable/resource directory into this location only when the destination does not already exist. It does not delete the original files. Keep this directory when updating or uninstalling if you want to retain local data.

## WhatsApp Cloud API (optional)

WhatsApp is disabled unless `WHATSAPP_ENABLED=true`. This feature uses Meta's official WhatsApp Business Platform Cloud API; it does not automate a personal WhatsApp Web session. You need to create/configure a Meta developer app with the WhatsApp product, a registered Business Platform phone number, a long-lived access token with messaging permission, the phone-number ID, and a webhook subscription to the `messages` field. The person messaging the assistant must have opted in as required by Meta's messaging policy.

Set these backend-only values in `.env`:

```env
WHATSAPP_ENABLED=true
WHATSAPP_ACCESS_TOKEN=your_meta_access_token
WHATSAPP_PHONE_NUMBER_ID=your_business_phone_number_id
WHATSAPP_VERIFY_TOKEN=your_random_webhook_verification_string
WHATSAPP_APP_SECRET=your_meta_app_secret
WHATSAPP_NGROK_PROXY_TOKEN=your_random_ngrok_edge_marker
WHATSAPP_ALLOWED_PHONE_NUMBERS=263771234567
WHATSAPP_PUBLIC_WEBHOOK_URL=https://your-ngrok-domain.ngrok-free.app
WHATSAPP_GRAPH_API_VERSION=v26.0
```

`WHATSAPP_ALLOWED_PHONE_NUMBERS` is a comma-separated allowlist in international format without relying on local dialing prefixes. The service normalizes spaces, `+`, punctuation, and country code formatting before comparing senders. Never place these values in frontend code or commit `.env`.

### Expose the webhook with ngrok

Meta requires a publicly reachable HTTPS callback. Start the existing backend, then run ngrok against its current port:

```powershell
ngrok http 8787
```

Do not run a bare `ngrok http 8787`: that would publish the local API. Use an ngrok Traffic Policy that strips any client-supplied marker, adds your private marker only on the webhook path, and returns 404 for all other paths. Generate a marker with `python -c "import secrets; print(secrets.token_urlsafe(32))"`, put it in `.env` as `WHATSAPP_NGROK_PROXY_TOKEN`, and copy the same value into a local-only `ngrok/whatsapp-traffic-policy.yml` file:

```yaml
on_http_request:
	- expressions:
			- "req.url.path == '/api/whatsapp/webhook' || req.url.path == '/webhooks/whatsapp'"
		actions:
			- type: remove-headers
				config:
					headers:
						- x-planner-ngrok-auth
			- type: add-headers
				config:
					headers:
						x-planner-ngrok-auth: "REPLACE_WITH_THE_SAME_RANDOM_MARKER"
	- actions:
			- type: deny
				config:
					status_code: 404
```

Start ngrok with the policy:

```powershell
ngrok http 8787 --traffic-policy-file ngrok/whatsapp-traffic-policy.yml
```

Use the HTTPS forwarding address shown by ngrok as `WHATSAPP_PUBLIC_WEBHOOK_URL` and in Meta's webhook setup. The exact callback is `https://<your-ngrok-domain>/api/whatsapp/webhook`. The backend disables Uvicorn proxy-header rewriting and checks the actual local socket peer. Forwarded requests must include the marker injected by the ngrok policy; the app never accepts `X-Forwarded-For` alone as proof. The policy denies all paths other than the two webhook routes, and the backend independently rejects forwarded requests to `/api/whatsapp/status` and other private APIs. Keep the backend and ngrok process running while receiving messages. A free ngrok URL can change between runs, so update `.env` and Meta's callback whenever it changes.

In Meta's app dashboard, configure the exact HTTPS URL and the same verify token, complete webhook verification, and subscribe to the `messages` field. Incoming POSTs are authenticated using `X-Hub-Signature-256` and the raw body with your Meta App Secret. Repeated message IDs are deduplicated in a SQLite state file under the current user's app data directory. `WHATSAPP_STATE_DB` can override that location.

The webhook handles common natural-language task creation, task lists, completion, deadline and priority changes, calendar viewing, and calendar creation with follow-up questions for missing date/time. All task writes use the existing `main.py` JSON task manager. Calendar writes use the existing Google Calendar service and are reported only after it confirms success. Gemini is used for schedule/planning questions; internal reasoning is not sent to users. Ambiguous task matches and unclear dates ask for clarification instead of mutating data.

The Settings status is local configuration only. It cannot confirm Meta approval, webhook subscription, number registration, or message delivery. The Messages API response means Meta accepted a send request, not that it was delivered. If the machine or tunnel is offline, Meta retries webhooks; keep the app and tunnel online. Do not expose a Quick Tunnel or other unrestricted proxy for this purpose.

### WhatsApp development and deployment checklist

1. Copy `.env.example` to `.env`; configure the Gemini and Meta values. Keep `WHATSAPP_ENABLED=false` until credentials and the sender allowlist are in place.
2. Set `WHATSAPP_NGROK_PROXY_TOKEN`, create the local Traffic Policy file with the same marker, then start the desktop app/backend and run ngrok with `--traffic-policy-file`.
3. Set `WHATSAPP_PUBLIC_WEBHOOK_URL` to ngrok's HTTPS origin, restart the backend, and configure Meta's callback at `/api/whatsapp/webhook` using the same verify token.
4. Subscribe the app to `messages`, allowlist your sender in international number format, then test only after you have real Meta credentials. Settings shows local readiness, not Meta's external approval or activation state.

### WhatsApp troubleshooting

- `Not ready`: check the named missing configuration keys in Settings and confirm `WHATSAPP_ENABLED=true`; no secret values are shown there.
- Meta verification fails: check the exact HTTPS webhook path, the configured verify token, and that `WHATSAPP_ENABLED=true`.
- Ngrok requests get `403`: start ngrok with the Traffic Policy, ensure it removes and then injects `x-planner-ngrok-auth`, and verify that the policy marker exactly matches `WHATSAPP_NGROK_PROXY_TOKEN`. Never use forwarded-IP headers alone as authorization.
- Incoming webhook rejected: check Meta's webhook logs, ensure the app secret is current, confirm the phone-number ID, and verify that the sender is in the allowlist.
- No reply: check that the backend and tunnel are running, Gemini and Google Calendar are configured for the requested operation, and the user is within Meta's customer-service messaging window or an approved template is available. The integration does not send unsolicited messages.
- Never work around HTTPS, signature validation, or the allowlist to troubleshoot.

## Start the backend

In a terminal:

```powershell
.\.venv\Scripts\python api.py
```

The API binds to `127.0.0.1:8787` only.

## Start the frontend

In a second terminal:

```powershell
npm install --prefix frontend
npm --prefix frontend run dev
```

The Vite frontend runs at `http://127.0.0.1:1420`.

## Launch the desktop app

```powershell
npm run tauri:dev
```

## Build the desktop application

```powershell
npm run frontend:build
npm run tauri:build
```

Tauri's Windows `.ico`, macOS `.icns`, Linux PNG, and platform-specific assets are generated from `frontend/src-tauri/branding/personal-ai-agent-icon.svg`. Regenerate them after editing the source with:

```powershell
npm --prefix frontend run tauri -- icon src-tauri/branding/personal-ai-agent-icon.svg --output src-tauri/icons
```

The source is a branded approximation using the serif P badge present in this repository; no separate website logo source was available to copy. The Tauri bundle configuration references the generated platform assets, including `icons/icon.ico` for Windows packaging.

Installer resources include application binaries and icons only; `.env`, OAuth credentials/tokens, tasks, and planner data are not bundled.

## Run the tests

```powershell
python -m unittest test_api.py test_whatsapp.py
```

## Troubleshooting

- If the chat returns a Gemini configuration error, ensure `.env` exists and contains a valid `GEMINI_API_KEY`.
- If Google Calendar is unavailable, check that `credentials.json` is in the app data directory and reconnect in the app; OAuth tokens are stored there automatically.
- If the backend is not reachable, ensure the Python API is running on `127.0.0.1:8787` and the frontend is pointing to that loopback address.
- If the frontend cannot connect to the API, confirm the backend is started and no other process is using port `8787`.
- If the desktop app reports that its backend executable is missing, reinstall a freshly built package; Tauri installs the sidecar as `api.exe` next to the app executable.
- If Gemini diagnostics return HTTP 504, the configured model was reached but the provider timed out; retry later without changing the configured model.
- Gmail TLS certificate verification must remain enabled. Check the system clock, network inspection software, and the packaged CA bundle if a secure connection fails.
- Gmail sync skips a message that was deleted after Gmail listed it; a 404 for another API request is still reported as a sync failure.

## Known constraints

- The application is local-first and does not store credentials in the frontend.
- WhatsApp remains disconnected until Meta credentials, HTTPS ingress, webhook verification, and sender authorization are configured and externally tested.
- Actual calendar operations go through the Google Calendar API only when the local OAuth flow is configured and available.
- The app does not overwrite existing app-data files during legacy migration. Credential and token files are local-only and must not be committed.
