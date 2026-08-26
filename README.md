# Google Takeout Batch Downloader

> **Fork notice:** this is [gotjen](https://github.com/gotjen)'s fork of
> [cschladetsch/PyGoogleTakeoutDownloader](https://github.com/cschladetsch/PyGoogleTakeoutDownloader).

## Overview

A comprehensive, secure Python solution for automating the download of large Google Takeout exports across multiple files.

## Features

- Batch download of multiple Takeout files
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

# Install the package (editable, so source edits take effect immediately)
pip install -e .
```

This installs the package and its dependencies, plus two commands:
`configure-secrets` and `download-takeout`.

## Workflow

Google actively blocks automated sign-in (see "Why capture is manual" below),
so there's no automated login step — the workflow is: configure once, capture
a session from your own browser, then let the downloader run until that
session goes stale, at which point it pauses and asks you to recapture.

### 1. Configure

```bash
configure-secrets
```

Creates/validates `secrets.json` and interactively prompts for anything
missing: `output_directory`, `download_delay`, and `max_files` (how many
indexed files your Takeout export was split into — check the Takeout page
for this).

### 2. Run the downloader

```bash
download-takeout
```

No command-line flags — it reads `secrets.json`, and prompts you to paste a
captured download session the first time it's needed (see below). On startup
it scans `output_directory` for files it's already
downloaded and resumes by filling in whatever's missing, from index 0
through `google_takeout.max_files` — so if a file gets skipped for any
reason, the next run re-fetches exactly that one rather than trusting a
single "last completed" counter (which was, in practice, once wrong after a
mid-batch session refresh). It waits `google_takeout.download_delay`
seconds between files.

Each file is downloaded into a local staging directory in your system's temp
folder (a stable, fixed-name subdirectory under `tempfile.gettempdir()`,
e.g. `/tmp/pygoogletakeoutdownloader` on Linux) first, verified against
`Content-Length` and against the CRC32C checksum Google's server returns
(`x-goog-hash` header), and only then moved into your configured
`output_directory`. This matters if that directory is a network/cloud-mounted
drive (e.g. rclone) — the slow, expensive download never touches it
directly, only the final move does, so a flaky mount only costs a retried
move, not a redownload. If a run is interrupted after the download but
before the move, re-running detects the completed file in the staging
directory and reuses it (re-verifying its checksum) instead of downloading
again.

That move runs on a background thread, so it overlaps with the next file's
download instead of blocking it — files are still moved strictly in order.
Before writing or moving a file, the script also checks that both the
staging directory and `output_directory` have enough free space, failing
fast with a clear message rather than partway through a large transfer.

### When the session goes stale

The first time a valid session is needed (startup, or a request comes back
non-200 or HTML — Google's way of saying the session died), `download-takeout`
pauses and prompts you to capture and paste a fresh one:

1. In a normal browser, sign into the Google account your Takeout export
   belongs to, then go to `https://takeout.google.com/settings/takeout`.
2. Click **Download** (or open an existing export's Download link).
3. Open DevTools (F12) → **Network** tab, find the request whose URL starts
   with `download?...` under `takeout.google.com`, right-click it, and choose
   **Copy → Copy as cURL**.
4. Paste it into the running `download-takeout` prompt (it's multi-line —
   paste it all, then press Enter on an empty line to submit). Nothing is
   written to disk — it's parsed and applied straight to the current run.

This will keep happening every ~10 minutes or so of real run time: the
`rapt` token Google issues is time-limited regardless of activity, not just
idle-timed out, so expect to repeat this capture-and-paste step roughly
every 3 files across a large export.

### Why capture is manual

A Selenium-driven login used to attempt this automatically. It's gone now:
Google's bot detection rejected it outright, landing on Google's
`/v3/signin/rejected` page right after the email step — before a password
field ever appeared — regardless of headless/non-headless mode or selector
fixes. That's confirmed behavior from a real run against this project, not a
theoretical concern, and no amount of selector maintenance would fix it.

### Logging
- Logs print to the console (stderr); redirect if you want them in a file,
  e.g. `download-takeout 2> takeout_download.log`

## Troubleshooting

- Check network connectivity and disk space.
- If the session goes stale, `download-takeout` will prompt you to paste a
  fresh capture — see Workflow above.
- Automated (browser-driven) sign-in is not attempted at all anymore; don't
  expect it to "just log in."

## Testing

### Run Tests
```bash
# Activate virtual environment
source venv/bin/activate

# Install test dependencies (pytest, coverage)
pip install -e ".[dev]"

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

