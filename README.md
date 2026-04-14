# WOMS — Work Order Management System

Demo application for **Seatrade 2026** showing bidirectional sync between **SafetyCulture Actions** and a simulated **IssueTracker** work order system. Built for the Royal Caribbean use case.

## What it does

- SafetyCulture actions automatically create IssueTracker work orders (and vice versa)
- Status, priority, title, and due date changes sync bidirectionally in near-real-time
- Live dashboard shows both systems side-by-side with a sync activity log

## Quick Start (Docker — recommended)

**Prerequisites:** [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running.

**1. Clone the repo**

```bash
git clone https://github.com/SafetyCulture/WOMS.git
cd WOMS
```

**2. Set your SafetyCulture API token**

```bash
# Create a .env file with your token
echo "SC_API_TOKEN=your_token_here" > .env
```

> To get an API token: SafetyCulture web app → your profile menu → "Integrations" → API → generate a token.

**3. Start the app**

```bash
docker compose up --build
```

**4. Open the dashboard**

Go to **http://localhost:8000** in your browser.

That's it. The app will start syncing with SafetyCulture immediately.

## Quick Start (without Docker)

**Prerequisites:** Python 3.9+

```bash
git clone https://github.com/SafetyCulture/WOMS.git
cd WOMS
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file:

```
SC_API_TOKEN=your_token_here
```

Start the server:

```bash
uvicorn app:app --reload --port 8000
```

Open **http://localhost:8000**.

## Demo Walkthrough

1. **Show existing SC actions** — the left panel loads all current actions from SafetyCulture
2. **Create a work order in IssueTracker** — click "+ Work Order", fill in details. Watch it appear in SafetyCulture within seconds
3. **Create an action in SafetyCulture** — either via the app or the SC mobile/web app. It appears in IssueTracker on the next sync cycle (10s)
4. **Change status in IssueTracker** — click a work order, change status to "Completed". Watch the sync log show the update flowing to SC
5. **Change priority in SC** — update priority on the SC side. The IssueTracker work order updates on the next poll

## Configuration

All config is via environment variables (or `.env` file):

| Variable | Default | Description |
|----------|---------|-------------|
| `SC_API_TOKEN` | *(required)* | SafetyCulture API bearer token |
| `SC_API_BASE` | `https://api.safetyculture.io` | SC API base URL |
| `SYNC_INTERVAL_SECONDS` | `10` | How often to poll SC for changes |

## Architecture

```
┌──────────────┐       ┌──────────────────┐       ┌──────────────┐
│ SafetyCulture │◄─────►│   WOMS Server    │◄─────►│   IssueTracker  │
│   (live API)  │       │  (FastAPI/Python) │       │  (SQLite DB) │
└──────────────┘       └──────────────────┘       └──────────────┘
                              │
                              ▼
                       ┌──────────────┐
                       │  Dashboard   │
                       │  (Browser)   │
                       └──────────────┘
```

- **Sync engine** polls the SC `/feed/actions` endpoint every N seconds
- New/changed SC actions create or update IssueTracker work orders
- Changes made in IssueTracker push back to SC via individual update endpoints
- SSE (Server-Sent Events) push live updates to the browser dashboard

## Troubleshooting

- **"Demo Mode" badge** — your API token isn't set or is invalid. Check `.env`
- **400 errors in sync log** — usually a field format issue. Check the server terminal for details
- **Port 8000 in use** — change the port: `uvicorn app:app --port 3000` or update `docker-compose.yml`
