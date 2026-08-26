# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project Overview

**Google Takeout Batch Downloader** — a small collection of standalone Python
scripts (no package/framework) that batch-download a Google Takeout export
split across many numbered zip files. Google Takeout requires a fresh,
short-lived authenticated download URL, and the session backing it expires
periodically.

Workflow (see README.md's "Workflow" section for the full walkthrough):

1. `configure_secrets.py` sets up `secrets.json` (output directory, file
   count, download delay).
2. **The user manually captures an authenticated download request** into
   `curl.txt`, via their own browser's DevTools ("Copy as cURL" on the
   Takeout download request). There is no automated login step — see
   "Why capture is manual" below.
3. `download_takeout.py` replays that captured request with an incrementing
   file index (`requests`), streaming each Takeout zip into the local,
   gitignored `temp_download/` directory (inside the repo) first, verifying
   size against `Content-Length` and checksum against the `x-goog-hash`
   response header (see "CRC32C verification" below), then handing the
   verified file to a background `MoveWorker` thread that `shutil.move`s it
   into the configured `output_directory`. It takes **no command-line
   arguments** — everything comes from `secrets.json`/`curl.txt`.
   - **Resume is scan-based, not counter-based:** on startup, `main()` calls
     `scan_completed_indices(outdir)` to find which `...-{index:03d}.zip`
     files already exist, then always loops from index 0, skipping (with no
     network request) any index already present. `last_downloaded_index` is
     still written to `secrets.json` for informational purposes but is no
     longer read for resume — see "Fixed" below for why that counter turned
     out not to be trustworthy on its own.
   - **Why stage locally first:** `output_directory` is commonly a
     network/cloud-mounted destination (e.g. an `rclone` remote), which can
     throw transient `OSError`s under a sustained many-GB write. Downloading
     to local disk first means that failure mode only ever costs a retry of
     the (cheap) move, never a re-download of the (expensive,
     token-consuming) file from Google. A completed `temp_download/*.part`
     file matching the expected size is detected and reused on the next run
     instead of re-downloading.
   - **Why the move is backgrounded:** the move into `output_directory` is
     the slow, network-bound half of each file (the download into local
     `temp_download/` is the other half); running it on a `MoveWorker`
     thread lets the next file's download start immediately instead of
     blocking on it. The main loop checks `MoveWorker.error()` at the top of
     each iteration and after the loop, and always joins the worker (even on
     an early exit) before returning — it is deliberately not a daemon
     thread, since letting the process exit mid-move could leave a
     half-copied file at the destination. Concurrent *downloads* were
     considered and rejected: several simultaneous requests under one
     `rapt`/session look far more bot-like than the deliberately-throttled
     (`download_delay`) sequential pattern this project already relies on to
     avoid the bot detection described below.
4. When the session goes stale (missing/unparseable `curl.txt`, or a
   non-200/HTML response mid-download), `download_takeout.py` pauses, prints
   the manual-recapture steps, and blocks on `input()` until the user
   confirms `curl.txt` is updated.

### Why capture is manual — Selenium-based login was removed

A Selenium-driven login script (`secure_token_retriever.py`, and before it
`token_retriever.py`) used to attempt this automatically. Both are gone now,
along with the `selenium`/`webdriver-manager` dependencies: **Google's bot
detection rejected the automated sign-in outright**, landing on
`https://accounts.google.com/v3/signin/rejected?...` — Google's dedicated
bot-rejection page — right after the email step, before a password field ever
appeared. This was confirmed by an actual run in this environment, not a
theoretical concern, and held regardless of headless/non-headless mode or
selector fixes. No amount of selector maintenance fixes it; it would take
deliberate automation-evasion techniques (not pursued here) to get further,
so the project relies on manual capture instead. One upside: a real browser's
"Copy as cURL" naturally includes cookies and a `rapt=` param, which the old
Selenium script's homemade `curl` string (built from `<meta>` tag values)
never reliably did — so manual capture is actually more robust for
`download_takeout.py`'s `parse_curl()`, which requires both.

### CRC32C verification

The actual file bytes come from `takeout-download.usercontent.google.com`
(the target of a redirect from `takeout.google.com/settings/takeout/download`
that `requests` follows transparently), and that response carries an
`x-goog-hash: crc32c=<base64>` header — confirmed against a real captured
response in this environment. `parse_expected_crc32c()` decodes it (GCS
convention: base64 of the big-endian 4-byte CRC32C/Castagnoli — *not* the
polynomial `zlib.crc32` uses, hence the `crc32c` PyPI dependency) and
`main()` compares it against a running checksum computed while streaming
each file, or via `compute_file_crc32c()` for a reused local file. A
mismatch is treated exactly like the existing size-mismatch case: the file
is deleted and the run halts, so the next run's outdir scan (above) picks
it back up. `md5=` may also appear in the header for some responses but
isn't used — `crc32c` is always present in what's been observed so far.

## Scripts

| File | Purpose |
|---|---|
| `credentials.py` | Shared credential storage/retrieval helpers (`is_keyring_available()`, `get_credential()`, `set_credential()`). Tries the OS keyring first, falls back to a plaintext `secrets.json` field (with a warning) only when keyring is unavailable, locked, or empty; `set_credential()` verifies writes with a read-back. Used by `configure_secrets.py`. |
| `configure_secrets.py` | Interactive wizard (`SecretsValidator`). Loads/creates `secrets.json`, validates fields (via `credentials.get_credential`, so a keyring-backed value still validates even when blank on disk), prompts for missing values, and stores email/password/`two_factor_secret` via `credentials.set_credential`. `save_config()` blanks any field successfully stored in keyring before writing to disk. Run `python configure_secrets.py --migrate-to-keyring` to move existing plaintext credentials into keyring. **Note:** nothing currently reads these credentials back for a login step — see "Open question" below. |
| `download_takeout.py` | The batch downloader — see Workflow above. Reads `secrets.json` + `curl.txt`, parses headers/cookies/`rapt` token from the curl string via regex (`parse_curl`), scans `output_directory` for already-completed indices (`scan_completed_indices`), loops from 0 skipping those, builds download URLs (`create_url`), streams each file to a temp file, verifies size and CRC32C (`parse_expected_crc32c`/`compute_file_crc32c`), then hands it to a background `MoveWorker` (moves into `output_directory`, overlapping with the next download). `refresh_download_token()` prints manual-recapture instructions and blocks on `input()` — no subprocess/Selenium involved. |
| `test_download_takeout.py` | `unittest` tests for `create_url()`, `parse_curl()`, `parse_expected_crc32c()`, `compute_file_crc32c()`, `scan_completed_indices()`. |
| `test_credentials.py` | `pytest` tests for `credentials.py`'s keyring-first/plaintext-fallback behavior. |
| `test_configure_secrets.py` | `pytest` tests for `SecretsValidator`, including the keyring migration path and regression tests for the two bugs listed below. |

**Removed:** `token_retriever.py` and `secure_token_retriever.py` (Selenium
login scripts) and `test_secure_token_retriever.py`, along with the
`selenium`/`webdriver-manager`/`pytest-selenium` dependencies — see "Why
capture is manual" above.

## Config / state files (all gitignored)

- **`secrets.json`** — created by `configure_secrets.py`. Structure:
  `google_takeout.{email, password, two_factor_secret, max_files,
  output_directory, download_delay}`, `authentication.{job_id,
  last_downloaded_index, last_token_refresh}`, `proxy.*`, `logging.*`.
- **`curl.txt`** — the captured authenticated download request, captured
  manually via browser DevTools (see Workflow above); consumed by
  `download_takeout.py`.
- **`takeout_download.log`** — run log (format configured in
  `download_takeout.py`'s `main()`).

## Dependencies

Declared in `requirements.txt` (no `setup.py`/`pyproject.toml`): `requests`,
`crc32c` (CRC32C verification — see above), `keyring`, `secretstorage`
(Linux keyring backend), `pyotp` (currently unused — see below), `structlog`
(currently unused, pre-existing), `urllib3`, plus `pytest`/`coverage` for
testing. No Chrome/Chromium/chromedriver needed anymore. Per the README:
create a venv, then `pip install -r requirements.txt`.

## Testing

```bash
pytest
```

## Open question — credential storage without a consumer

`configure_secrets.py` still prompts for and stores a Google
email/password/`two_factor_secret` (via `credentials.py`, preferring
keyring). Since the Selenium login step that used to consume those was
removed, **nothing in the codebase reads them back for any functional
purpose anymore** — they're stored but inert. This hasn't been resolved
either way (keep for a possible future non-Selenium login approach vs. strip
credential storage out entirely) — flag it rather than assuming when picking
this up.

## Known issues / gotchas

- **`pyotp`/`structlog` are unused dependencies.** `pyotp` was only ever
  referenced (as a `# TODO`, never implemented) in the now-deleted
  `secure_token_retriever.py`; `structlog` was never imported anywhere in the
  repo, pre-dating this session's changes. Neither is load-bearing.
- **`keyring` is listed in `requirements.txt` but may not be installed on
  every machine** — check with `pip show keyring` (or
  `credentials.is_keyring_available()`); `credentials.get_credential`/
  `set_credential` degrade to the plaintext-fallback / no-op path
  automatically either way.

### Fixed in the keyring-retrieval refactor (still relevant — `configure_secrets.py` is still in use)

- `configure_secrets.py`'s email prompt loop used to spin forever once
  keyring storage succeeded (it checked
  `self.config['google_takeout']['email']`, which `_store_credential()` never
  updated on the keyring-success path). Fixed: `_store_credential()` now
  always keeps the in-memory value current, and validation/loop conditions
  go through `credentials.get_credential()`.
- `two_factor_secret` used to silently never persist to `secrets.json` on the
  plaintext-fallback path (the old `_store_credential()` fallback branch only
  special-cased `email`/`password`). Fixed by generalizing the fallback to
  any `google_takeout` key.

### Fixed after switching to manual `curl.txt` capture

- **Every real download 500'd — `job_id` was never actually sourced from
  `curl.txt`.** `create_url()`'s `j=` parameter came from
  `config['authentication']['job_id']` in `secrets.json`, which the deleted
  Selenium script used to populate; manual capture never touched that field,
  so it silently defaulted to the literal string `'unknown'` and every
  request went out as `...&j=unknown&...`, which Google's server rejects.
  Confirmed against a real manually-captured `curl.txt` in this environment.
  Fixed: `parse_curl()` now extracts `job_id` from the curl command's own
  `j=` query param (raising `ValueError` if absent, same as it already does
  for `rapt`), and `main()` uses that instead of the stale config field.
- **Relatedly, a mid-run token refresh never actually reloaded `curl.txt`.**
  On a non-200/HTML response, `refresh_download_token()` walked the user
  through recapturing `curl.txt`, but the loop then just `continue`d reusing
  the *original* `rapt`/`job_id`/session cookies — so a recapture mid-run
  couldn't have worked either, even with `job_id` fixed. Fixed via a new
  `load_curl_state(session)` helper (re-parses `curl.txt` and re-applies
  headers/cookies to the `requests.Session` in place), now called after
  every successful `refresh_download_token()`, not just the initial load.
- **Failure diagnostics used to discard the response body.** A failed
  download only ever printed `Error: Status {code}`. Added
  `describe_error_response()`, which surfaces status + reason + content-type
  + a truncated response-body snippet — Google's error responses (including
  the 500 above) usually explain themselves in the body far better than the
  status code alone.
- **An `OSError` writing the output file crashed the whole run instead of
  failing just that file.** `output_directory` can be a network/cloud-mounted
  remote (e.g. `rclone`) — streaming a many-GB file through that continuously
  is exactly where a transient backend hiccup surfaces as a plain
  `OSError: [Errno 5] Input/output error`, which wasn't caught anywhere (only
  `requests.Timeout`/`requests.RequestException` were). Worse, the cleanup
  `tmpfile.unlink()` calls weren't defensive either, so a failing cleanup on
  the same flaky mount could itself raise and obscure the original error.
  Fixed: a dedicated `except OSError` around the per-file write/verify/rename
  block prints a clear disk/mount-specific message and returns cleanly
  (re-running resumes at the same index, since `last_downloaded_index` is
  only advanced after success); added `_safe_unlink()` so cleanup failures
  are logged, not raised. Also bumped `iter_content`'s chunk size from 8 KiB
  to 4 MiB — at 8 KiB a single 50+ GB file is millions of Python-level
  iterations for no benefit.
- **Downloads now stage in a local `temp_download/` directory before moving
  to `output_directory`**, so the slow/expensive part (streaming from
  Google) never touches a remote-mounted destination directly — only the
  final `shutil.move()` does, which is cheap to retry without re-downloading
  if the destination hiccups. A completed local file matching the expected
  size is detected and reused across runs instead of re-downloaded.
- **The move into `output_directory` now runs on a background `MoveWorker`
  thread** instead of blocking the main loop, so it overlaps with the next
  file's download. A move failure is recorded (`MoveWorker.error()`) rather
  than raised across threads; the main loop checks it each iteration and
  after the loop, converts it to the same exit-1 behavior the old synchronous
  `except OSError` block had, and always joins the worker (via
  `finally: move_worker.close()`) before returning so the process never exits
  mid-move. Multiple *concurrent downloads* (as opposed to backgrounding the
  move) were considered and deliberately not implemented — see Workflow
  step 3 above.
- Also added `check_disk_space()` (via `shutil.disk_usage`), checked against
  both `temp_download/` before writing and `output_directory` before
  queuing the move, so a full disk is caught upfront with a clear message
  instead of surfacing mid-transfer as an `OSError`.
- **A stale-session token refresh silently skipped the failed file instead
  of retrying it, and the loss was permanent.** Confirmed against a real
  run: file 6 hit the HTML-auth-failure path, `refresh_download_token()`
  succeeded, but the code did `continue` inside a
  `for i in range(start, max_files):` loop — which advances to `i+1`, not a
  retry of `i`. File 6 was abandoned; once file 7 then succeeded,
  `MoveWorker` advanced `last_downloaded_index` to 8, permanently hiding
  the gap from every future run (outdir ended up with `..., 04, 05, 07,
  ...` — no 06). Fixed by converting the loop to manually-indexed
  `while i < max_files`, incrementing `i` only after a real success; both
  refresh-then-`continue` paths now retry the same `i`.
- **Resume no longer trusts `last_downloaded_index` as the sole source of
  truth** — the bug above demonstrated it can't be. `main()` now calls
  `scan_completed_indices(outdir)` at startup and always loops from 0,
  skipping (without a network request) any index already present, so a
  resume backfills exactly what's missing regardless of what the stored
  counter says.
- **Added CRC32C verification** against Google's `x-goog-hash` response
  header (see "CRC32C verification" above) — size matching alone can't
  catch a same-size corruption, and this closes that gap using data Google
  already sends on every response.
