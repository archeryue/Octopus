# Future Features

## Telegram Bot Integration

**Priority**: Medium
**Status**: Planned

### Problem

The web UI requires a browser, which is not always convenient on mobile. A Telegram bot provides a lightweight, always-available interface to interact with Octopus sessions from any device.

### Goal

Connect to Octopus sessions and interact with Claude through Telegram — send messages, receive responses, and manage sessions without opening a browser.

### User Flow

1. Start a chat with the Octopus Telegram bot
2. Authenticate (e.g. `/login <token>` or a one-time link)
3. `/sessions` — list active sessions
4. `/use <session_id>` — attach to a session
5. Send messages directly — bot forwards to Claude and streams back the response
6. `/new <name>` — create a new session
7. Receive push notifications for long-running task completions

### Key Features

- **Session management**: list, create, delete, switch sessions via bot commands
- **Message relay**: send prompts and receive Claude's responses in chat
- **Streaming output**: use Telegram's edit-message API to simulate streaming
- **Approval handling**: when Claude needs tool approval, bot sends an inline keyboard (Approve / Deny)
- **Notifications**: push a message when a background task completes or Claude is waiting for input

### Proposed Approach

- Use `python-telegram-bot` (async, well-maintained) library
- Add a `TelegramBridge` service in `server/` that connects to `SessionManager`
- Bot authenticates users via the same token mechanism as the web UI
- Subscribe to session events via the existing broadcast/WebSocket system
- Config: `TELEGRAM_BOT_TOKEN` env var to enable the integration

### Architecture

```
Telegram <-> Bot Process <-> Octopus API <-> SessionManager <-> Claude SDK
                               (reuse existing REST + WS endpoints)
```

---

## Public Deployment

**Priority**: High
**Status**: Planned

### Goal

Deploy Octopus on a remote machine (VPS/cloud) so you can control Claude Code from anywhere — phone, tablet, another laptop — over the internet.

### Prerequisites

The remote machine needs:
- Python 3.12+, Node.js/Bun (for building frontend)
- Claude Code CLI installed and authenticated (`claude` must work)
- A public IP or domain name

### Phase 1: Harden for Public Exposure

**Auth token** — The default `changeme` token is not safe for the internet.

- [ ] Generate a strong random token: `python -c "import secrets; print(secrets.token_urlsafe(32))"`
- [ ] Set it via `OCTOPUS_AUTH_TOKEN` env var or `.env` file
- [ ] Consider adding rate limiting on `/ws` and `/api/sessions` to prevent abuse

**HTTPS** — Browsers block `ws://` on HTTPS pages, and tokens are visible in plaintext over HTTP.

- Option A: **Cloudflare Tunnel** (easiest, no port forwarding needed)
  ```bash
  cloudflared tunnel --url http://localhost:8000
  ```
  Gives you a `https://*.trycloudflare.com` URL instantly. WebSocket upgrades work automatically (`wss://`). Zero config on the server side — our frontend already derives `ws://` vs `wss://` from `window.location.protocol`.

- Option B: **Reverse proxy with Let's Encrypt**
  ```nginx
  server {
      listen 443 ssl;
      server_name octopus.yourdomain.com;
      ssl_certificate /etc/letsencrypt/live/.../fullchain.pem;
      ssl_certificate_key /etc/letsencrypt/live/.../privkey.pem;

      location / {
          proxy_pass http://127.0.0.1:8000;
          proxy_http_version 1.1;
          proxy_set_header Upgrade $http_upgrade;
          proxy_set_header Connection "upgrade";
          proxy_set_header Host $host;
      }
  }
  ```
  Use `certbot` for free SSL certs. The `Upgrade` headers are critical for WebSocket.

- Option C: **Tailscale/WireGuard** (private network, no public exposure)
  Install Tailscale on both machines. Access Octopus via the Tailscale IP. No HTTPS needed since traffic is encrypted at the network layer.

### Phase 2: Process Management

**Keep Octopus running** after SSH disconnect:

- Option A: **systemd** (recommended for Linux VPS)
  ```ini
  # /etc/systemd/system/octopus.service
  [Unit]
  Description=Octopus - Remote Claude Code Controller
  After=network.target

  [Service]
  Type=simple
  User=deploy
  WorkingDirectory=/home/deploy/Octopus
  Environment=OCTOPUS_AUTH_TOKEN=your-secret-token
  ExecStart=/home/deploy/Octopus/.venv/bin/uvicorn server.main:app --host 0.0.0.0 --port 8000
  Restart=on-failure
  RestartSec=5

  [Install]
  WantedBy=multi-user.target
  ```
  ```bash
  sudo systemctl enable octopus
  sudo systemctl start octopus
  ```

- Option B: **tmux/screen** (quick and dirty)
  ```bash
  tmux new -s octopus
  cd ~/Octopus && octopus serve
  # Ctrl-B D to detach
  ```

### Phase 3: Deployment Script

A one-command deploy workflow:

```bash
# deploy.sh — run on the remote machine
#!/bin/bash
set -e

cd ~/Octopus
git pull origin main

# Backend
.venv/bin/pip install -e .

# Frontend
cd web && bun install && bun run build && cd ..

# Restart
sudo systemctl restart octopus
echo "Deployed at $(git log --oneline -1)"
```

### Phase 4: Security Checklist

Before going public:

- [ ] **Strong auth token** — not `changeme`
- [ ] **HTTPS** — via tunnel, reverse proxy, or VPN
- [ ] **Firewall** — only expose ports 443 (HTTPS) and 22 (SSH), NOT 8000 directly
- [ ] **Claude Code permissions** — Octopus runs with `bypassPermissions`; the remote machine should have limited filesystem access or run in a container
- [ ] **SQLite backup** — `octopus.db` holds all sessions; add a cron job to back it up
- [ ] **Log rotation** — uvicorn logs can grow; use systemd journal or logrotate

### Phase 5: Optional Enhancements

- [ ] **Docker image** — `Dockerfile` that bundles Python, Claude CLI, built frontend. Single `docker run` to deploy
- [ ] **Multi-user auth** — replace single token with user accounts (JWT or session cookies)
- [ ] **Sandboxed execution** — run Claude Code in a Docker container per session to isolate filesystem access
- [ ] **Health monitoring** — add `/health` checks to uptime monitoring (UptimeRobot, etc.)

### Quick Start (Cloudflare Tunnel)

Fastest path from zero to public:

```bash
# On your remote machine with Claude Code installed:
git clone <repo> && cd Octopus
python -m venv .venv && .venv/bin/pip install -e .
cd web && bun install && bun run build && cd ..

# Set a real token
export OCTOPUS_AUTH_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
echo "Your token: $OCTOPUS_AUTH_TOKEN"

# Start server + tunnel
octopus serve &
cloudflared tunnel --url http://localhost:8000

# Open the printed https:// URL on your phone, paste the token, done.
```
