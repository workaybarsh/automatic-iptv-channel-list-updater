# Automatic IPTV Channel List Updater

A small tool that repairs Turkish IPTV/M3U playlists **without changing channel
order or metadata**. Dead stream URLs are replaced with working ones from the
[iptv-org](https://github.com/iptv-org/iptv) database.

> This repository contains **only the tool**. There is no bundled playlist or
> stream URL. The user supplies their own M3U file.

## Download

Download the latest zip from the
[Releases](https://github.com/workaybarsh/automatic-iptv-channel-list-updater/releases/latest)
page. Extract it, put your playlist next to the tool as `update-channels.m3u`,
and double-click `UPDATE-PLAYLIST.bat`.

## What it does

1. Finds the playlist in its own folder whose filename contains `update`.
2. Tests every channel URL in that playlist.
3. For dead channels, fetches candidate links from the iptv-org API and tests them.
4. Replaces only the **URL line**; `#EXTINF`, channel name, logo, group,
   `tvg-id` and channel order stay exactly the same.
5. Writes a timestamped backup to `backups/` and a CSV report to `reports/`.

By default it never deletes a channel. If no alternative is found, the channel
is left untouched and reported. Use `--remove-dead` to remove such channels.

## Matching safety

- **Exact `tvg-id` first:** `ShowTV.tr@SD` → `ShowTV.tr`.
- Country-coded IDs prevent cross-country confusion (`ShowTV.tr` ≠ `ShowTV.pr`).
- If the channel has multiple feeds, the **same `@feed`** (SD/HD) is preferred.
- No fuzzy, substring or “similar name” matching.
- Name matching is **off by default**. With `--name-match`, it only works:
  - inside the same country,
  - with an exact normalized name,
  - and only if that name maps to a single channel; ambiguous names are skipped.
- Automatic aliases are not created; only the manual `ALIASES` table is used.

## Quick start (Windows)

1. Extract the release zip into a folder.
2. Put your playlist next to the tool. The filename must contain `update`:
   - `update-channels.m3u`
   - `my-update-list.m3u`
3. Double-click `UPDATE-PLAYLIST.bat`.
4. On the first run, the `requests` package is installed automatically if needed.
5. For later updates, double-click the same `.bat` file again.

Double-click **`UPDATE-PLAYLIST.bat`**, not the playlist.

If several filenames contain `update`, the tool asks once and saves the choice
in `playlist.txt`. If none match, it refuses to touch anything.

### Python

```bash
pip install requests
python playlist_updater.py
```

## Source

- `https://iptv-org.github.io/api/streams.json`
- `https://iptv-org.github.io/api/channels.json`
- `https://iptv-org.github.io/iptv/countries/tr.m3u`

Source data is cached under `%LOCALAPPDATA%\iptv-playlist-updater\cache`.

## Options

| Option | Description |
|---|---|
| `--dry-run` | Do not write any file, only report |
| `--scan-only` | Scan only; do not change/add links |
| `--only TEXT` | Only channels whose name/ID contains this text |
| `--limit N` | First N channels (for testing) |
| `--workers N` | Number of parallel channels (default 8) |
| `--timeout S` | Read timeout in seconds (default 12) |
| `--retries N` | Retries for failed requests (default 2) |
| `--confirm N` | Extra attempts before replacing a failed original link (default 3) |
| `--deep` | Also download and verify the first video segment |
| `--siblings` / `--no-siblings` | Try quality variants (default: on) |
| `--max-candidates N` | Maximum candidate links per channel (default 10) |
| `--name-match` | Try exact name matching inside the same country (default: off) |
| `--refresh-different` | Also update working channels whose source URL changed |
| `--remove-dead` | Remove channels with no working alternative |
| `--offline` | Use the last cache instead of downloading source data |
| `--no-header` | Do not change the `#Updated:` timestamp |
| `--no-backup` | Do not create a backup |
| `--report FILE` | Custom report path |

## Development

```bash
python -m py_compile playlist_updater.py
python tests/test_matching.py
```

## Legal notice

This tool does not host, rebroadcast or redistribute any stream. It only tests
URLs in the user's own playlist and looks for alternatives in public data
sources. All stream URLs belong to their respective owners. Users are solely
responsible for complying with applicable laws and terms of service. Do not
publicly distribute links to paid, geo-blocked or unauthorized streams.

## License

MIT — see [LICENSE](LICENSE).
