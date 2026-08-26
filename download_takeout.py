#!/usr/bin/env python3

import requests
import os
import shutil
import threading
import queue
import time
import re
import base64
import struct
import crc32c
import tempfile
from tqdm import tqdm
from pathlib import Path
from datetime import datetime
from typing import NamedTuple
import json
import logging

# Files are downloaded here first, then moved to the configured
# output_directory once complete. Staging in the OS temp dir (a stable,
# fixed-name subdirectory, not a fresh mkdtemp() per run) means the slow,
# expensive part — streaming a many-GB file from Google — never touches a
# potentially flaky network/FUSE-mounted destination; only the final move
# does, and that's cheap to retry without re-downloading. Keeping the name
# stable also means a completed-but-unmoved .part file from a prior run is
# still found and reused (see the tmpfile.exists() check below) instead of
# re-downloaded.
TEMP_DIR = Path(tempfile.gettempdir()) / 'pygoogletakeoutdownloader'

def refresh_download_token():
    """
    Refresh the session by walking the user through a manual DevTools
    recapture, pasted directly into this prompt — nothing is written to
    curl.txt, so no cookie jar is left sitting on disk between refreshes.

    Google's automated *sign-in* detection rejects a Selenium/Playwright-
    driven browser outright (the /v3/signin/rejected page) before it ever
    reaches a password field, so there's no automated way to do this step
    — the human captures it themselves, in whatever browser they already
    have the Takeout page open in.

    :return: the AuthState parsed from what the user pastes; None if
        refresh failed/was aborted (e.g. via EOF/Ctrl+C -> caught by
        caller). Nothing is written to disk here — the captured cookies
        live only in memory for the life of this process, applied
        straight to the requests.Session (see main()'s apply_auth_state()
        calls) instead of round-tripping through a curl.txt file, so
        nothing session-sensitive is left behind once the run ends.
    """
    print("\nPaste a fresh 'Copy as cURL' capture below "
          "(blank line to submit, Ctrl+C to abort):")

    while True:
        lines = []
        try:
            while True:
                line = input()
                if line.strip() == '':
                    break
                lines.append(line)
        except (EOFError, KeyboardInterrupt):
            logging.error("Token refresh aborted before a curl command was entered")
            return None

        curl_text = '\n'.join(lines)
        if not curl_text.strip():
            print("Nothing entered — paste the curl command, or Ctrl+C to abort.")
            continue

        try:
            auth = parse_curl(curl_text)
        except ValueError as e:
            print(f"Could not parse that as a Takeout curl command ({e}) — try again, or Ctrl+C to abort.")
            continue

        logging.info("Continuing with manually entered curl command")
        return auth

def create_url(index, job_id, rapt):
    """Create download URL with exact working format."""
    return (f"https://takeout.google.com/settings/takeout/download?"
            f"i={index}&"
            f"j={job_id}&"
            f"download=true&"
            f"rapt={rapt}")

class AuthState(NamedTuple):
    """
    The four pieces of session state a download request needs — produced
    by parse_curl() from either an on-disk curl.txt (initial bootstrap,
    if one exists) or a curl command pasted directly into
    refresh_download_token()'s prompt (every refresh after that).
    """
    headers: dict
    cookies: dict
    rapt: str
    job_id: str

RAPT_RE = re.compile(r'rapt=([^&\s\']+)')
JOB_ID_RE = re.compile(r'[?&]j=([^&\s\']+)')

def extract_rapt(text):
    match = RAPT_RE.search(text)
    if not match:
        raise ValueError("No rapt token found")
    return match.group(1)

def extract_job_id(text):
    # The job id (j=...) must come from the captured curl command, not a
    # stale config value — it identifies which Takeout export this is and
    # changes across exports/sessions. A wrong/missing job id produces a
    # request Google's server rejects (seen in practice as a 500).
    match = JOB_ID_RE.search(text)
    if not match:
        raise ValueError("No job id (j=...) found")
    return match.group(1)

def parse_curl(curl_text):
    """Extract auth info from curl command."""
    if not 'takeout.google.com' in curl_text:
        raise ValueError("Not a Google Takeout curl command")

    headers = {}
    for match in re.finditer(r"-H '([^:]+): ([^']+)'", curl_text):
        name, value = match.groups()
        headers[name] = value

    cookies = {}
    cookie_match = re.search(r"-b '([^']+)'", curl_text)
    if cookie_match:
        for pair in cookie_match.group(1).split('; '):
            if '=' in pair:
                name, value = pair.split('=', 1)
                cookies[name] = value

    rapt = extract_rapt(curl_text)
    job_id = extract_job_id(curl_text)

    return AuthState(headers, cookies, rapt, job_id)

def describe_error_response(response, max_body_chars=500):
    """
    Build a diagnostic string for a failed download response.

    Google's actual error responses (e.g. a 500, or a sign-in-required HTML
    page) usually carry a short explanation in the body that's far more
    useful for debugging than the bare status code alone.
    """
    content_type = response.headers.get('content-type', 'unknown')
    try:
        body = response.text
    except Exception as e:
        body = f'<unable to read response body: {e}>'

    snippet = ' '.join(body.split())[:max_body_chars]
    if len(body) > max_body_chars:
        snippet += '...'

    return (
        f"HTTP {response.status_code} {response.reason} "
        f"(content-type: {content_type})\n"
        f"  Body: {snippet or '<empty>'}"
    )

OUTFILE_INDEX_RE = re.compile(r'-(\d{3})\.zip$')

def scan_completed_indices(outdir):
    """
    Return the set of file indices already present in outdir, parsed from
    filenames matching outfile's `...-{index:03d}.zip` naming scheme.

    Used to detect gaps in the numbered series — e.g. a file a past bug
    silently skipped, or one manually removed — so a resume can find exactly
    what's missing instead of trusting last_downloaded_index alone, which
    has already been observed to advance past a skipped file.
    """
    indices = set()
    for path in outdir.glob('*.zip'):
        m = OUTFILE_INDEX_RE.search(path.name)
        if m:
            indices.add(int(m.group(1)))
    return indices

def parse_expected_crc32c(x_goog_hash_header):
    """
    Extract the CRC32C checksum from an `x-goog-hash` response header
    (e.g. "crc32c=DC95Hw==,md5=..."), returning it as an int, or None if
    the header is missing or carries no crc32c entry.

    Google's takeout-download.usercontent.google.com responses (the target
    of the redirect from takeout.google.com/settings/takeout/download,
    which `requests` follows transparently) carry this header — confirmed
    against a real captured response. It's the GCS convention: base64 of
    the big-endian 4-byte CRC32C (Castagnoli, not zlib.crc32's polynomial).
    """
    if not x_goog_hash_header:
        return None
    for part in x_goog_hash_header.split(','):
        part = part.strip()
        if part.startswith('crc32c='):
            try:
                return struct.unpack('>I', base64.b64decode(part[len('crc32c='):]))[0]
            except (ValueError, struct.error):
                return None
    return None

def compute_file_crc32c(path, chunk_size=4 * 1024 * 1024):
    """CRC32C of an existing file on disk, read in the same chunk size used
    for downloading (see main()'s streaming loop)."""
    crc = 0
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                return crc
            crc = crc32c.crc32c(chunk, crc)

def check_disk_space(path, required_bytes, label):
    """
    Verify at least `required_bytes` is free on the filesystem backing `path`.

    Uses shutil.disk_usage (the `df`-equivalent) rather than shelling out to
    `df`. TEMP_DIR (OS temp dir) and output_directory are commonly on
    different filesystems (see module docstring), and a many-GB Takeout file is
    expensive enough to fetch that running out of space mid-stream/mid-move
    should be caught upfront rather than discovered as an OSError partway
    through.
    """
    free = shutil.disk_usage(path).free
    if free < required_bytes:
        print(
            f"Error: not enough free space at {label} ({path}): "
            f"{free:,} bytes free, need {required_bytes:,} bytes"
        )
        logging.error(
            f"Insufficient disk space at {label} ({path}): "
            f"{free:,} free < {required_bytes:,} required"
        )
        return False
    return True

def _safe_unlink(path):
    """
    Best-effort cleanup of a partial download. On a flaky output mount
    (e.g. an rclone/FUSE remote), the unlink itself can raise OSError — that
    shouldn't crash the script or mask whatever error triggered the cleanup.
    """
    try:
        if path.exists():
            path.unlink()
    except OSError as e:
        logging.warning(f"Could not remove partial file {path}: {e}")

def patch_config_field(config_path, section, key, value):
    """
    Read-modify-write a single config field on disk, instead of
    overwriting the whole file from a possibly-stale in-memory snapshot.

    A long-running download_takeout.py invocation holds its config dict
    in memory for the whole run; if MoveWorker (or anything else) later
    dumps that entire snapshot back to secrets.json, it silently reverts
    any edit made to the file after the run started — e.g. a cdp_url
    added mid-run, or a second invocation's own in-memory copy. Confirmed
    in practice: MoveWorker used to do exactly that. Reading fresh from
    disk immediately before the write keeps this narrow to the one field
    each caller actually owns.
    """
    with open(config_path, 'r') as f:
        on_disk = json.load(f)
    on_disk.setdefault(section, {})[key] = value
    with open(config_path, 'w') as f:
        json.dump(on_disk, f, indent=4)

class MoveWorker:
    """
    Background thread that moves verified downloads from TEMP_DIR into
    output_directory and persists last_downloaded_index, so that (often
    slow, network-mounted) move overlaps with the next file's download
    instead of blocking the main loop on it.

    Not a daemon thread: the caller must always call close() (e.g. in a
    finally block) before exiting, since killing the process mid-move could
    leave a half-copied file at the destination.
    """

    def __init__(self, config_path):
        self._queue = queue.Queue()
        self._config_path = config_path
        self._error = None
        self._thread = threading.Thread(target=self._run)
        self._thread.start()

    def submit(self, index, tmpfile, outfile):
        self._queue.put((index, tmpfile, outfile))

    def _run(self):
        while True:
            job = self._queue.get()
            if job is None:
                return
            index, tmpfile, outfile = job
            # Once one move has failed, skip the rest rather than moving
            # files out of order or past a failure last_downloaded_index
            # can't reflect anyway. They stay safely in TEMP_DIR.
            if self._error is None:
                try:
                    # shutil.move (not Path.rename) since TEMP_DIR and
                    # outdir are commonly on different filesystems —
                    # rename() can't cross that boundary, move() falls back
                    # to copy+delete.
                    shutil.move(str(tmpfile), str(outfile))
                    patch_config_field(self._config_path, 'authentication', 'last_downloaded_index', index + 1)
                    logging.info(f"Moved file {index} to {outfile}")
                except OSError as e:
                    logging.error(f"I/O error moving file {index} to {outfile}: {e}")
                    self._error = (index, tmpfile, outfile, e)

    def error(self):
        """Return (index, tmpfile, outfile, exception) of the first failed
        move, or None if none has failed (yet)."""
        return self._error

    def close(self):
        """Wait for all queued moves (or the failure point) to finish."""
        self._queue.put(None)
        self._thread.join()

# Heuristic only — Google doesn't publish how long a `rapt` token stays
# valid. This just makes a stale capture visible up front (e.g. when
# resuming a run much later) instead of only discovered after a failed
# download makes the script pause anyway.
CURL_STALENESS_WARNING_SECONDS = 10 * 60

def describe_curl_age():
    """Print curl.txt's age, warning if it looks old enough to be stale."""
    try:
        age_seconds = time.time() - os.path.getmtime('curl.txt')
    except OSError:
        return

    print(f"Using curl.txt captured {age_seconds / 60:.0f} minute(s) ago.")
    if age_seconds > CURL_STALENESS_WARNING_SECONDS:
        print(
            f"Warning: that's over {CURL_STALENESS_WARNING_SECONDS // 60} "
            "minutes old — the session may already be stale. Consider "
            "recapturing curl.txt now rather than waiting for a failure."
        )

def apply_auth_state(session, auth):
    """
    Apply an AuthState's headers/cookies to `session` in place, regardless
    of whether it came from an on-disk curl.txt or a pasted curl command.

    :return: (rapt, job_id)
    """
    session.headers.update(auth.headers)
    session.cookies.update(auth.cookies)
    return auth.rapt, auth.job_id

def load_curl_state(session):
    """
    Read curl.txt (the initial bootstrap file, if one exists — refreshes
    no longer write one, see refresh_download_token()), parse it, and
    apply it to `session`.

    :return: (rapt, job_id)
    """
    with open('curl.txt') as f:
        auth = parse_curl(f.read())
    return apply_auth_state(session, auth)

def main():
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

    # Read secrets configuration
    try:
        with open('secrets.json', 'r') as f:
            config = json.load(f)
    except FileNotFoundError:
        logging.error("secrets.json not found")
        return 1

    session = requests.Session()
    session.timeout = 30  # 30 second timeout

    # curl.txt is only an optional initial-bootstrap convenience now —
    # refresh_download_token() prompts for a pasted curl command directly
    # and never writes one to disk (see its docstring for why).
    if os.path.exists('curl.txt'):
        # Only worth flagging for a pre-existing capture — one just
        # produced by refresh_download_token() below is obviously not
        # stale, and isn't written to a file to begin with.
        describe_curl_age()
        try:
            rapt, job_id = load_curl_state(session)
        except (ValueError, IOError) as e:
            logging.error(f"Error parsing curl.txt: {e}")
            auth = refresh_download_token()
            if auth is None:
                logging.error("Failed to retrieve new download token after parsing error")
                return 1
            rapt, job_id = apply_auth_state(session, auth)
    else:
        logging.info("curl.txt not found; prompting for an initial curl capture.")
        auth = refresh_download_token()
        if auth is None:
            logging.error("Failed to retrieve new download token")
            return 1
        rapt, job_id = apply_auth_state(session, auth)

    # Create output directory and local staging directory
    outdir = Path(config['google_takeout'].get('output_directory', '/mnt/f/GoogleTakeout'))
    outdir.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    # Resume point comes entirely from what's actually in outdir, not from
    # last_downloaded_index: a past bug demonstrated that counter can
    # advance past a file that was silently skipped, permanently hiding the
    # gap from every future run if trusted as the sole source of truth.
    # Simplest fix: always start at 0 and let the per-index check below
    # skip whatever's already present — no separate "first gap" bookkeeping
    # to keep in sync.
    existing_indices = scan_completed_indices(outdir)
    start = 0
    max_files = config['google_takeout'].get('max_files', 277)

    print(f"Found {len(existing_indices)} existing file(s) in {outdir}; "
          f"filling in whatever's missing up to index {max_files - 1}.")

    download_delay = config['google_takeout'].get('download_delay', 5)

    # Moves run on a background thread so the (often slow, network-mounted)
    # copy into output_directory overlaps with the next file's download
    # instead of blocking it. exit_code (rather than early `return`s) lets
    # every exit path still fall through to the `finally` that joins it.
    move_worker = MoveWorker('secrets.json')
    exit_code = 0
    i = start
    try:
        while i < max_files:
            worker_error = move_worker.error()
            if worker_error:
                exit_code = 1
                break

            if i in existing_indices:
                # A gap-fill pass can walk back into indices already
                # present past the first gap (e.g. resuming into 6 when 7+
                # already exist) — skip straight through without a request.
                print(f"File {i} already present in {outdir}, skipping")
                i += 1
                continue

            print(f"\nDownloading file {i}...")

            url = create_url(i, job_id, rapt)
            try:
                response = session.get(url, stream=True)

                if response.status_code == 404:
                    print("File not found - archive may not be ready")
                    exit_code = 1
                    break

                if response.status_code != 200:
                    # Full response body (often a wall of Google's minified
                    # JS/JSON page state) is rarely useful to a human here —
                    # keep it at debug level only; the console gets a short,
                    # actionable line instead.
                    logging.debug(f"Download request failed: {describe_error_response(response)}")
                    print(f"Download request failed (HTTP {response.status_code} {response.reason}) — refreshing session.")
                    logging.error(f"Download request failed with HTTP {response.status_code}")
                    # Attempt to refresh token
                    auth = refresh_download_token()
                    if auth is None:
                        print("Failed to refresh download token")
                        exit_code = 1
                        break
                    rapt, job_id = apply_auth_state(session, auth)
                    continue  # retry the SAME i with the refreshed token

                if 'html' in response.headers.get('content-type', ''):
                    # Same reasoning as above: this is Google's sign-in page
                    # markup, not a useful diagnostic for a human — the
                    # short message below already says everything actionable.
                    logging.debug(f"Got HTML instead of file: {describe_error_response(response)}")
                    print("Session expired (got a sign-in page instead of the file) — refreshing session.")
                    logging.error("Got HTML instead of file (session expired)")
                    # Attempt to refresh token
                    auth = refresh_download_token()
                    if auth is None:
                        print("Failed to refresh download token")
                        exit_code = 1
                        break
                    rapt, job_id = apply_auth_state(session, auth)
                    continue  # retry the SAME i with the refreshed token

                # outfile's name carries the download timestamp for
                # traceability; tmpfile's name is stable (index-only) so a
                # completed local download can be recognized and reused
                # across runs.
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                outfile = outdir / f"takeout-{timestamp}Z-{i:03d}.zip"
                tmpfile = TEMP_DIR / f"takeout-{i:03d}.zip.part"

                # Get expected size and checksum
                total_size = int(response.headers.get('content-length', 0))
                if total_size:
                    print(f"Size: {total_size:,} bytes")
                expected_crc32c = parse_expected_crc32c(response.headers.get('x-goog-hash'))

                if total_size and tmpfile.exists() and tmpfile.stat().st_size == total_size:
                    # Already fully downloaded locally — likely a previous
                    # run got through the download but failed moving it to
                    # outdir. Don't re-download; just close this response
                    # and move on.
                    print(f"Found complete local copy at {tmpfile}, skipping re-download")
                    response.close()
                    if expected_crc32c is not None and compute_file_crc32c(tmpfile) != expected_crc32c:
                        print("Error: CRC32C mismatch on existing local copy")
                        _safe_unlink(tmpfile)
                        exit_code = 1
                        break
                else:
                    if total_size and not check_disk_space(TEMP_DIR, total_size, "temp download dir"):
                        response.close()
                        exit_code = 1
                        break

                    print(f"Saving to {tmpfile}")
                    try:
                        crc = 0
                        with open(tmpfile, 'wb') as f, tqdm(
                            total=total_size or None,
                            unit='B',
                            unit_scale=True,
                            unit_divisor=1024,
                            desc=f"File {i:03d}",
                            colour='green',
                        ) as pbar:
                            # 4 MiB chunks: at the default 8 KiB, a single
                            # large (tens-of-GB) Takeout file means millions
                            # of Python-level iterations for no benefit.
                            for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                                if chunk:
                                    f.write(chunk)
                                    crc = crc32c.crc32c(chunk, crc)
                                    pbar.update(len(chunk))

                        # Verify size if we got content-length
                        if total_size and tmpfile.stat().st_size != total_size:
                            print("Error: Size mismatch")
                            _safe_unlink(tmpfile)
                            exit_code = 1
                            break

                        if expected_crc32c is not None and crc != expected_crc32c:
                            print("Error: CRC32C mismatch")
                            _safe_unlink(tmpfile)
                            exit_code = 1
                            break

                    except OSError as e:
                        # A genuinely local disk problem (out of space,
                        # etc) — TEMP_DIR lives in the OS temp dir, not on a
                        # network mount, so this is unlikely but still
                        # shouldn't crash the script.
                        print(f"Error: I/O error writing local temp file {tmpfile}: {e}")
                        logging.error(f"I/O error writing temp file for {i}: {e}")
                        _safe_unlink(tmpfile)
                        exit_code = 1
                        break
                    except:
                        _safe_unlink(tmpfile)
                        raise

                move_size = total_size or tmpfile.stat().st_size
                if move_size and not check_disk_space(outdir, move_size, "output directory"):
                    exit_code = 1
                    break

                print(f"Queued move to {outfile}")
                move_worker.submit(i, tmpfile, outfile)

            except requests.Timeout:
                print("Error: Request timed out")
                exit_code = 1
                break
            except requests.RequestException as e:
                print(f"Error: {e}")
                exit_code = 1
                break

            print(f"Waiting {download_delay} seconds...")
            time.sleep(download_delay)
            # Only reached after a real success — every retry/refresh path
            # above hits `continue` before this, and every fatal path hits
            # `break`. This is what makes retrying file i (rather than
            # silently skipping to i+1, as the old `for i in range(...)`
            # loop did on a token-refresh `continue`) actually work.
            i += 1
    except KeyboardInterrupt:
        # A clean pause, not a crash: last_downloaded_index only ever
        # reflects fully-moved files (MoveWorker updates it), so re-running
        # simply resumes at the same index — nothing extra to persist here.
        print(f"\nInterrupted — pausing before file {i}.")
        print("Re-run to resume; already-completed files are unaffected.")
        logging.warning(f"Interrupted by user during file {i}")
        exit_code = 130  # conventional 128+SIGINT exit code
    finally:
        # Always join the worker, even on an early exit above: it isn't a
        # daemon thread, since letting the process die mid-move could leave
        # a half-copied file sitting at outfile. This also means a Ctrl+C
        # here waits for an in-flight move to finish rather than aborting it
        # partway through.
        move_worker.close()

    if exit_code == 0:
        worker_error = move_worker.error()
        if worker_error:
            index, tmpfile, outfile, e = worker_error
            # E.g. an rclone/FUSE-backed output_directory hiccuping mid-
            # copy. Not something this script can fix, but it shouldn't
            # crash the run: the fully-downloaded file is still sitting
            # safely in TEMP_DIR, so re-running retries just the move, not
            # the expensive download.
            print(f"Error: I/O error moving file {index} to {outfile}: {e}")
            print(f"The downloaded file is safe at {tmpfile} —")
            print(f"check that {outdir} is healthy, then re-run to retry the move.")
            exit_code = 1

    return exit_code

if __name__ == "__main__":
    try:
        exit(main())
    except KeyboardInterrupt:
        # Catches an interrupt during setup, before main()'s own
        # try/except KeyboardInterrupt (and its MoveWorker) even exist.
        print("\nInterrupted before startup completed.")
        exit(130)

# Path: download_takeout.py
