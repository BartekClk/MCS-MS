# MCS-MS — Minecraft Server Management System

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![Minecraft](https://img.shields.io/badge/Minecraft-Modded%20%2F%20Packwiz-62B47A?style=flat-square)
![Platform](https://img.shields.io/badge/Platform-Linux%20(screen)-FCC624?style=flat-square&logo=linux&logoColor=black)
![Integrations](https://img.shields.io/badge/Integrations-Google%20Drive%20%7C%20Discord%20%7C%20GitHub-5865F2?style=flat-square)

An automated management, backup, and safe modpack deployment (**Packwiz**) system for Linux-hosted Minecraft servers. Built in pure Python with zero external web frameworks, full `.env` configuration, and production server protection via an isolated **Canary** staging environment.

---

## Key Features

* **Canary-Gated Updates (`updater.py`):**
  * Gracefully shuts down the main server with player countdown broadcasts via RCON.
  * Synchronizes the modpack using `packwiz-installer-bootstrap` on an isolated **Canary** instance.
  * Automated boot verification: monitors `logs/latest.log` for fatal errors (`FATAL`, `Mod Loading has failed`, `Exception in server tick loop`), detects newly generated files in `crash-reports/`, and verifies server readiness via RCON (`list`).
  * Promotes the update to the main server **only** after the Canary instance passes all health checks.
* **Dynamic MOTD with Pack Version:**
  * Injects a customizable template from `templates/motd_template.txt` into the main server's `server.properties` after a successful Canary verification.
  * Automatically substitutes the latest GitHub Releases tag (`%pack_version%`) and preserves centering/indentation by escaping leading spaces for Java `.properties` parsers.
* **Google Drive Backups with RCON Coordination:**
  * Performs safe world snapshots (`save-off` → `save-all flush` → `.tar.gz` archiving → `save-on`) as well as full server directory backups (`--backup-all`).
  * Uploads archives to Google Drive using chunked transfers with automated retention cleanup for old backups.
* **Dual-Channel Discord Notifications (`discord_webhook.py`):**
  * **Player Channel:** Posts a formatted embed containing the GitHub Release changelog and download link (`templates/discord_template.json`) once the updated main server is back online.
  * **Admin Channel:** Sends a technical execution report after every CLI run (status, duration, exit code) and automatically attaches the latest `.log` file on failure (`templates/discord_admin_template.json`).
* **Persistent HTTP Daemon & Scheduler (`daemon.py`):**
  * Built-in multi-threaded HTTP server (`ThreadingHTTPServer`) listening for GitHub webhooks alongside a daily backup scheduler (`DAILY_BACKUP_TIME`).
  * Unified **FIFO** task queue ensuring scheduled backups and GitHub-triggered updates never execute concurrently.

---

## `--run` Pipeline Flow

```text
[Trigger: CLI / GitHub Webhook]
              │
              ▼
   1. World backup to Google Drive (RCON: save-off ➔ save-all flush ➔ tar.gz ➔ save-on)
              │
              ▼
   2. Fetch latest release metadata from GitHub Releases API
              │
              ▼
   3. Graceful shutdown of MAIN server (RCON countdown + stop)
              │
              ▼
   4. Packwiz update on CANARY ➔ Boot CANARY ➔ Verify logs, crash-reports & RCON
              │
         ┌────┴────┐
      [FAIL]    [PASS]
         │         │
         ▼         ▼
   Halt pipeline 5. Packwiz update on MAIN + inject MOTD (%pack_version%)
   (MAIN left      │
   untouched)      ▼
                6. Boot MAIN server + verify RCON readiness
                   │
                   ▼
                7. Save release state + Discord player announcement + admin report
```

---

## Project Structure

```text
MCS-MS/
├── MCSMS.py                        # Main CLI entry point and pipeline orchestrator
├── daemon.py                       # HTTP webhook server, FIFO queue, and daily backup scheduler
├── updater.py                      # Packwiz update logic, Canary validation, and MOTD injection
├── check_files.py                  # Strict validation of .env variables, paths, and ports
├── rcon_connection.py              # RCON client and countdown script runner (including --shutdown-test)
├── github_release.py               # GitHub REST API client for release tracking and state persistence
├── discord_webhook.py              # Discord webhook handler (player embeds + admin log reports)
├── logger.py                       # Centralized console and file logger
├── rcon/
│   └── shutdown_countdown.txt      # Sequence of RCON commands sent prior to server shutdown
├── templates/
│   ├── motd_template.txt           # MOTD template supporting the %pack_version% placeholder
│   ├── discord_template.json       # Editable Discord embed template for player announcements
│   └── discord_admin_template.json # Editable Discord embed template for technical admin reports
├── .env-template                   # Environment configuration template
└── .env                            # Active configuration (ignored by Git)
```

---

## Installation & Configuration

### 1. Prerequisites
* Python **3.10+**
* `screen` utility installed on Linux
* Java (matching your Minecraft server version) and `packwiz-installer-bootstrap.jar`
* RCON enabled (`enable-rcon=true`) in `server.properties` for both the main and Canary server instances

### 2. Environment Setup

```bash
git clone [https://github.com/BartekClk/MCS-MS.git](https://github.com/BartekClk/MCS-MS.git)
cd MCS-MS

python3 -m venv venv
source venv/bin/activate
pip install python-dotenv google-api-python-client google-auth-oauthlib google-auth-httplib2
```

### 3. `.env` & Google Drive Configuration

1. Copy the configuration template and fill in your server paths, RCON credentials, Discord webhook URLs, and daemon secret:
   ```bash
   cp .env-template .env
   ```
2. Place your OAuth2 client credentials file (`credentials.json`) inside `./Google_OAuth_Files/` (`token.json` will be generated automatically on the first backup run).
3. Customize notification embeds and MOTD formatting inside `./templates/` without modifying any Python code.

---

## Usage (CLI)

All operations are invoked through the main `MCSMS.py` entry point:

| Flag | Description |
| :--- | :--- |
| `python MCSMS.py --run` | Full maintenance pipeline: world backup ➔ Canary test ➔ Main update ➔ MOTD ➔ Discord. |
| `python MCSMS.py --backup` | Backs up only the world directory to Google Drive with RCON save coordination. |
| `python MCSMS.py --backup-all` | Performs a full backup of the entire main server directory (`SERVER_DIR`). |
| `python MCSMS.py --shutdown-test main` | Tests the RCON shutdown countdown on `main` or `canary` without stopping the server (`stop` is blocked). |
| `python MCSMS.py --daemon` | Starts the persistent HTTP webhook listener and daily backup scheduler. |
| `python MCSMS.py --env /path/to/.env ...` | Specifies a custom `.env` configuration file path. |

---

## HTTP Daemon & GitHub Actions Integration

Running `python MCSMS.py --daemon` exposes two endpoints on the address configured via `DAEMON_HOST:DAEMON_PORT`:

* **`GET /health`** — Returns the current task queue status, active task, scheduled daily backup time, and last execution result.
* **`POST /webhook`** — Accepts triggers from GitHub (authenticated via `Authorization: Bearer <DAEMON_WEBHOOK_SECRET>` or HMAC signature `X-Hub-Signature-256`), queues a `--run` task, and immediately responds with `202 Accepted`.

### Example GitHub Actions Workflow (`.github/workflows/deploy-server.yml`)

```yaml
name: Trigger MCS-MS Server Update

on:
  release:
    types: [published]
  workflow_dispatch:

jobs:
  trigger-mcsms:
    if: github.event_name == 'workflow_dispatch' || github.event.release.prerelease == false
    runs-on: ubuntu-latest
    steps:
      - name: Send update trigger to MCS-MS daemon
        env:
          WEBHOOK_URL: ${{ secrets.MCSMS_WEBHOOK_URL }}
          WEBHOOK_SECRET: ${{ secrets.MCSMS_WEBHOOK_SECRET }}
        run: |
          if [ -z "$WEBHOOK_URL" ] \vert{}\vert{} [ -z "$WEBHOOK_SECRET" ]; then
            echo "Error: Missing webhook secrets."
            exit 1
          fi

          curl --fail-with-body -sS -X POST "$WEBHOOK_URL" \
            -H "Authorization: Bearer $WEBHOOK_SECRET" \
            -H "Content-Type: application/json" \
            -d '{"action": "published"}'
```