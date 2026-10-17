# AI and Maker Event Manager

[日本語](README.md)

A personal, Japanese-language web app for AI development, creative AI, electronics, exhibitions, and maker meetups in and around Tokyo. It runs on Python, Waitress, and SQLite. Daily collection does not require an LLM.

## Features

- Collect candidates from official TECH PLAY RSS and Luma Tokyo iCal feeds.
- Keep Japanese-language support unconfirmed until explicit evidence exists.
- Separate organizers with recorded supporting evidence from unreviewed candidates.
- Hide events that conflict with work and travel time from the default discovery view.
- Filter for weekends and Japanese public holidays, including multi-day exhibitions.
- Record “not interested” separately from “not attending,” with undo and timestamped history.
- Preserve personal status, notes, and verification separately from collected facts.
- Track official schedule changes and explicit cancellations without treating disappearance as cancellation.
- Browser history, shareable filter URLs, JSON export, and SQLite backups.

Applications and cancellations happen on official event websites. Changing a status never submits or cancels an application. The stored value for “not attending” remains `見送り` for compatibility. Attendance decisions do not imply interest preferences.

## Run locally

Validated with Python 3.12.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python server.py serve --port 8798
```

Open `http://127.0.0.1:8798/`. Local development accepts loopback connections only. No personal events or notes are included. After collection, review candidates in the unconfirmed-organizer filter.

```sh
.venv/bin/python server.py daily
```

Use your operating system's scheduler for daily execution. Collection uses a concurrency lock and preserves existing records on source failures.

## Remote access

For phone access, configure HTTPS and owner authentication. Cloudflare Tunnel and Access are supported. Copy `access.example.json` to `data/access.json` and fill in your hostname, team, application audience, and allowed email addresses.

```sh
.venv/bin/python server.py serve --port 8798 --access-config data/access.json
```

Point the Tunnel to `http://127.0.0.1:8798` and configure an owner-only Access policy for the hostname. The application validates JWT signatures, issuer, audience, expiry, and email even on loopback. Never tunnel the unauthenticated local development mode to the internet.

Workday defaults are an 18:00 finish, 45 minutes of travel, and a 15-minute buffer. Travel time can be adjusted per event. Holidays use the Cabinet Office CSV; unavailable years remain unconfirmed. The app is unavailable while its server is stopped.

## Sources and limits

- [TECH PLAY official RSS](https://techplay.jp/information/236)
- [Luma official iCal](https://help.luma.com/p/ical-syncing)
- [connpass API](https://help.connpass.com/api/): approval and a key are required. Skipped without `EVENTS_CONNPASS_API_KEY`.
- [Doorkeeper API](https://www.doorkeeper.jp/developer/api): optional. Skipped without `EVENTS_DOORKEEPER_API_TOKEN`.
- Peatix: manual official-URL entry.

A published organizer name alone is not approval. Review evidence is event-specific, and `organizer-evidence.json` starts empty. Unsupported deadlines and cancellation information remain unconfirmed. No scraping or private API crawling is used.

## Data and checks

Data lives in `data/events.sqlite3`. Collected facts, personal state, interest preferences, organizer reviews, and history are stored separately. Daily execution keeps 14 local backups. Copies on the same disk do not protect against disk failure. Runtime data and deployment settings are excluded from Git.

```sh
.venv/bin/python -m unittest discover -s . -p 'test*.py'
.venv/bin/python server.py check
.venv/bin/python server.py export
```

Google Calendar writes are not implemented.

## License

[MIT License](LICENSE). Dependencies and event services retain their respective licenses and terms.
