# Google Takeout Batch Downloader

> **Fork notice:** this is [gotjen](https://github.com/gotjen)'s fork of
> [cschladetsch/PyGoogleTakeoutDownloader](https://github.com/cschladetsch/PyGoogleTakeoutDownloader).

## Overview

A comprehensive, secure Python solution for automating the download of large Google Takeout exports across multiple files.

## Features

- Batch download of multiple Takeout files
- Secure credential management
- Detailed logging and error handling
- Resumable downloads

## System Requirements

### Hardware & Software
- Python 3.8+
- A web browser (Chrome, Firefox, etc.) to manually capture the download
  request — see Workflow below
- Active Google Takeout export

### Required System Packages
```bash
sudo apt update
sudo apt install -y \
    python3-venv \
    python3-pip
```

## Installation

### 1. Clone Repository
```bash
git clone https://github.com/gotjen/PyGoogleTakeoutDownloader.git
cd PyGoogleTakeoutDownloader
```

### 2. Create Virtual Environment
```bash
# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# Upgrade pip
pip install --upgrade pip

# Install dependencies
pip install -r requirements.txt
```

## Workflow

Google actively blocks automated sign-in (see "Why capture is manual" below),
so there's no automated login step — the workflow is: configure once, capture
a session from your own browser, then let the downloader run until that
session goes stale, at which point it pauses and asks you to recapture.

### 1. Configure

```bash
python configure_secrets.py
```

Creates/validates `secrets.json` and interactively prompts for anything
missing: `output_directory`, `download_delay`, `max_files` (how many indexed
files your Takeout export was split into — check the Takeout page for this),
and optionally an email/password (see "About the stored credentials" below —
they aren't currently used by any download step).

### 2. Capture a download session into `curl.txt`

1. In a normal browser, sign into the Google account your Takeout export
   belongs to, then go to `https://takeout.google.com/settings/takeout`.
2. Click **Download** (or open an existing export's Download link).
3. Open DevTools (F12) → **Network** tab, find the request whose URL starts
   with `download?...` under `takeout.google.com`, right-click it, and choose
   **Copy → Copy as cURL**.
4. Paste it into `curl.txt` in this repo's root, replacing the file's
   contents.

### 3. Run the downloader

```bash
python download_takeout.py
```

No command-line flags — it reads everything from `secrets.json` and
`curl.txt`. It downloads files sequentially starting at
`authentication.last_downloaded_index` (so simply re-running resumes where it
left off) through `google_takeout.max_files`, waiting
`google_takeout.download_delay` seconds between files, and updates
`last_downloaded_index` in `secrets.json` after each successful file.

Each file is downloaded into a local, gitignored `temp_download/` directory
in the repo first, verified against `Content-Length`, and only then moved
into your configured `output_directory`. This matters if that directory is a
network/cloud-mounted drive (e.g. rclone) — the slow, expensive download
never touches it directly, only the final move does, so a flaky mount only
costs a retried move, not a redownload. If a run is interrupted after the
download but before the move, re-running detects the completed file in
`temp_download/` and reuses it instead of downloading again.

That move runs on a background thread, so it overlaps with the next file's
download instead of blocking it — files are still moved, and
`last_downloaded_index` still updated, strictly in order. Before writing or
moving a file, the script also checks that both `temp_download/` and
`output_directory` have enough free space, failing fast with a clear message
rather than partway through a large transfer.

### 4. When the session goes stale

If `curl.txt` is missing/unparseable, or a request comes back non-200 or HTML
(Google's way of saying the session died), `download_takeout.py` pauses,
prints the capture steps from step 2 again, and waits on `Enter` for you to
confirm `curl.txt` has been refreshed before it continues the same run.

### Why capture is manual

A Selenium-driven login used to attempt this automatically. It's gone now:
Google's bot detection rejected it outright, landing on Google's
`/v3/signin/rejected` page right after the email step — before a password
field ever appeared — regardless of headless/non-headless mode or selector
fixes. That's confirmed behavior from a real run against this project, not a
theoretical concern, and no amount of selector maintenance would fix it.

### About the stored credentials

`configure_secrets.py` can still prompt for and store a Google
email/password/two-factor secret (via `credentials.py`, preferring the OS
keyring). Nothing in the current codebase actually reads them for a login
step anymore, since that's now entirely manual — they're currently just
config, not something the downloader depends on.

### Migrating existing plaintext credentials to keyring
```bash
# Move any plaintext email/password/two_factor_secret out of secrets.json
# and into the OS keyring
python configure_secrets.py --migrate-to-keyring
```

### Logging
- Logs saved to `takeout_download.log`
- Configurable log levels in `secrets.json`

## Troubleshooting

- Check network connectivity and disk space.
- If `curl.txt` is stale/missing, `download_takeout.py` will prompt you
  through recapturing it — see Workflow above.
- Automated (browser-driven) sign-in is not attempted at all anymore; don't
  expect it to "just log in."

## Testing

### Run Tests
```bash
# Activate virtual environment
source venv/bin/activate

# Run tests
pytest
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

## License

Distributed under the MIT License. See `LICENSE` for details.

## Credits

Forked from [cschladetsch/PyGoogleTakeoutDownloader](https://github.com/cschladetsch/PyGoogleTakeoutDownloader).
See that repo's history for original authorship prior to this fork.

## Disclaimer

This tool is not affiliated with Google. Use responsibly and respect Google's terms of service.

