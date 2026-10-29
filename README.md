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
- `tasks.json` and `token.json` – local user data preserved in place
- `credentials.json` – Google Calendar OAuth credentials file

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
GEMINI_MODEL=gemini-2.5-flash
```

The backend reads the key from the local `.env` file or the active environment. Do not expose or commit the real key. The frontend never receives the API key.

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

## Run the tests

```powershell
python -m unittest test_api.py
```

## Troubleshooting

- If the chat returns a Gemini configuration error, ensure `.env` exists and contains a valid `GEMINI_API_KEY`.
- If Google Calendar is unavailable, check that `credentials.json` and `token.json` are present and not corrupted.
- If the backend is not reachable, ensure the Python API is running on `127.0.0.1:8787` and the frontend is pointing to that loopback address.
- If the frontend cannot connect to the API, confirm the backend is started and no other process is using port `8787`.

## Known constraints

- The application is local-first and does not store credentials in the frontend.
- Actual calendar operations go through the Google Calendar API only when the local OAuth flow is configured and available.
- The app does not modify or overwrite `credentials.json`, `token.json`, `tasks.json`, or `tasks_backup.json` during setup or testing.
