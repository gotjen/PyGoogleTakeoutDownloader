#!/usr/bin/env python3

import requests
import os
import time
import re
from pathlib import Path
from datetime import datetime
import json
import logging

def refresh_download_token():
    """
    Prompt for a manually recaptured curl.txt.

    Google's automated sign-in detection rejects a Selenium-driven login
    outright (the /v3/signin/rejected page) before it ever reaches a
    password field, so there is no automated way to refresh the token.
    Instead, pause and walk the user through capturing a fresh curl
    command from their own logged-in browser.

    :return: True once the user confirms curl.txt has been updated and it
        exists; False if they abort (e.g. via EOF/Ctrl+C -> caught by caller)
    """
    curl_path = os.path.abspath('curl.txt')
    print("\nYour Google Takeout download session is missing or stale.")
    print("Google blocks automated (Selenium-driven) sign-in, so this has to")
    print("be captured manually from a real, already-logged-in browser:")
    print("  1. In Chrome/Chromium, make sure you're signed into the right")
    print("     Google account, then go to:")
    print("     https://takeout.google.com/settings/takeout")
    print("  2. Click 'Download' (or wait for an existing export's Download link).")
    print("  3. Open DevTools (F12) -> Network tab, find the request whose URL")
    print("     starts with 'download?...' under takeout.google.com, right-click")
    print("     it, and choose 'Copy' -> 'Copy as cURL'.")
    print(f"  4. Paste it into {curl_path}, replacing the file's contents.")
    try:
        input("Press Enter once curl.txt has been updated (Ctrl+C to abort)... ")
    except (EOFError, KeyboardInterrupt):
        logging.error("Token refresh aborted before curl.txt was updated")
        return False

    if not os.path.exists('curl.txt'):
        logging.error("curl.txt still not found after manual refresh prompt")
        return False

    logging.info("Continuing with manually recaptured curl.txt")
    return True

def create_url(index, job_id, rapt):
    """Create download URL with exact working format."""
    return (f"https://takeout.google.com/settings/takeout/download?"
            f"i={index}&"
            f"j={job_id}&"
            f"download=true&"
            f"rapt={rapt}")

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

    rapt_match = re.search(r'rapt=([^&\s\']+)', curl_text)
    if not rapt_match:
        raise ValueError("No rapt token found")

    # The job id (j=...) must come from the captured curl command, not a
    # stale config value — it identifies which Takeout export this is and
    # changes across exports/sessions. A wrong/missing job id produces a
    # request Google's server rejects (seen in practice as a 500).
    job_id_match = re.search(r'[?&]j=([^&\s\']+)', curl_text)
    if not job_id_match:
        raise ValueError("No job id (j=...) found in curl command")

    return headers, cookies, rapt_match.group(1), job_id_match.group(1)

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

def load_curl_state(session):
    """
    Read curl.txt, parse it, and apply its headers/cookies to `session`
    in place.

    :return: (rapt, job_id)
    """
    with open('curl.txt') as f:
        headers, cookies, rapt, job_id = parse_curl(f.read())
    session.headers.update(headers)
    session.cookies.update(cookies)
    return rapt, job_id

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

    # Read curl command
    if not os.path.exists('curl.txt'):
        logging.error("curl.txt not found. Attempting to retrieve new token.")
        if not refresh_download_token():
            logging.error("Failed to retrieve new download token")
            return 1

    session = requests.Session()
    session.timeout = 30  # 30 second timeout

    try:
        rapt, job_id = load_curl_state(session)
    except (ValueError, IOError) as e:
        logging.error(f"Error parsing curl.txt: {e}")
        if not refresh_download_token():
            logging.error("Failed to retrieve new download token after parsing error")
            return 1

        # Retry parsing after token refresh
        try:
            rapt, job_id = load_curl_state(session)
        except Exception as e:
            logging.error(f"Persistent error parsing curl.txt: {e}")
            return 1

    # Create output directory
    outdir = Path(config['google_takeout'].get('output_directory', '/mnt/f/GoogleTakeout'))
    outdir.mkdir(parents=True, exist_ok=True)

    # Find last downloaded file
    start = config['authentication'].get('last_downloaded_index', 0)
    max_files = config['google_takeout'].get('max_files', 277)

    print(f"Starting from index {start}")

    download_delay = config['google_takeout'].get('download_delay', 5)

    for i in range(start, max_files):
        print(f"\nDownloading file {i}...")
        
        url = create_url(i, job_id, rapt)
        try:
            response = session.get(url, stream=True)
            
            if response.status_code == 404:
                print("File not found - archive may not be ready")
                return 1

            if response.status_code != 200:
                description = describe_error_response(response)
                print(f"Error: {description}")
                logging.error(f"Download request failed: {description}")
                # Attempt to refresh token
                if not refresh_download_token():
                    print("Failed to refresh download token")
                    return 1
                try:
                    rapt, job_id = load_curl_state(session)
                except (ValueError, IOError) as e:
                    print(f"Error parsing recaptured curl.txt: {e}")
                    return 1
                continue

            if 'html' in response.headers.get('content-type', ''):
                description = describe_error_response(response)
                print(f"Error: Got HTML instead of file (auth failed). {description}")
                logging.error(f"Got HTML instead of file: {description}")
                # Attempt to refresh token
                if not refresh_download_token():
                    print("Failed to refresh download token")
                    return 1
                try:
                    rapt, job_id = load_curl_state(session)
                except (ValueError, IOError) as e:
                    print(f"Error parsing recaptured curl.txt: {e}")
                    return 1
                continue

            # Create unique temp filename
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            outfile = outdir / f"takeout-{timestamp}Z-{i:03d}.zip"
            tmpfile = outdir / f"tmp_{timestamp}_{i:03d}.zip"
            
            # Get expected size
            total_size = int(response.headers.get('content-length', 0))
            if total_size:
                print(f"Size: {total_size:,} bytes")
            
            print(f"Saving to {outfile.name}")
            try:
                with open(tmpfile, 'wb') as f:
                    # 4 MiB chunks: at the default 8 KiB, a single large
                    # (tens-of-GB) Takeout file means millions of Python-level
                    # iterations for no benefit.
                    for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                        if chunk:
                            f.write(chunk)

                # Verify size if we got content-length
                if total_size and tmpfile.stat().st_size != total_size:
                    print("Error: Size mismatch")
                    _safe_unlink(tmpfile)
                    return 1

                tmpfile.rename(outfile)

                # Update last downloaded index
                config['authentication']['last_downloaded_index'] = i + 1
                with open('secrets.json', 'w') as f:
                    json.dump(config, f, indent=4)

            except OSError as e:
                # A local disk error or, e.g., an rclone/FUSE-backed output
                # directory hiccuping mid-write both surface as a plain
                # OSError here — not something this script can fix, but it
                # shouldn't crash the whole run either. last_downloaded_index
                # was not advanced, so simply re-running resumes at file i.
                print(f"Error: I/O error writing to {outdir}: {e}")
                print("This is a disk/output-mount problem, not an auth problem —")
                print(f"check that {outdir} is healthy, then re-run to resume at file {i}.")
                logging.error(f"I/O error writing file {i} to {outdir}: {e}")
                _safe_unlink(tmpfile)
                return 1
            except:
                _safe_unlink(tmpfile)
                raise
                
        except requests.Timeout:
            print("Error: Request timed out")
            return 1
        except requests.RequestException as e:
            print(f"Error: {e}")
            return 1
            
        print(f"Waiting {download_delay} seconds...")
        time.sleep(download_delay)
    
    return 0

if __name__ == "__main__":
    exit(main())

# Path: download_takeout.py
