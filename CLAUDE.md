# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project Overview

**Google Takeout Batch Downloader** — a small Python package (`pyproject.toml`,
`src/` layout) that batch-downloads a Google Takeout export split across many
numbered zip files. Google Takeout requires a fresh, short-lived authenticated
download URL, and the session backing it expires periodically.

Installed as a package, it exposes two console scripts: `configure-secrets`
and `download-takeout` (see "Layout" below for where the source actually
lives).

Workflow (see README.md's "Workflow" section for the full walkthrough):

1. `configure-secrets` sets up `secrets.json` (output directory, file
   count, download delay).
2. **The user manually captures an authenticated download request** and
   pastes it directly into `download-takeout`'s prompt (DevTools "Copy as
   cURL" on the Takeout download request). There is no automated login step
   — see "Why capture is manual" below. Nothing is written to disk for
   this — see "Session refresh" below.
3. `download-takeout` replays that captured request with an incrementing
   file index (`requests`), streaming each Takeout zip into a local staging
   directory in the system temp folder first, verifying
   size against `Content-Length` and checksum against the `x-goog-hash`
   response header (see "CRC32C verification" below), then handing the
   verified file to a background `MoveWorker` thread that `shutil.move`s it
   into the configured `output_directory`. It takes **no command-line
   arguments** — everything comes from `secrets.json` and the pasted curl
   capture.
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
     token-consuming) file from Google. A completed `*.part` file (in the
     staging directory, under a stable per-index name) matching the
     expected size is detected and reused on the next run instead of
     re-downloading.
   - **Why the move is backgrounded:** the move into `output_directory` is
     the slow, network-bound half of each file (the download into the local
     staging directory is the other half); running it on a `MoveWorker`
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
4. When the session goes stale (no session yet, or a non-200/HTML response
   mid-download), `download-takeout` pauses and blocks on `input()`,
   collecting a freshly pasted curl command directly — see "Session
   refresh" below.

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

**A follow-up automation attempt (browser-launch + `rapt` auto-extraction via
CDP/Playwright) was investigated in depth and abandoned — don't re-attempt
this without reading the reasoning first:**
- Launching a real Chrome binary as a plain `subprocess.Popen` (not
  Playwright's/Selenium's own `launch()`, which sets
  `navigator.webdriver=true`) and attaching via `connect_over_cdp()` *does*
  sign in normally — that part is safe and works.
- Auto-extracting a fresh `rapt` from that browser does not: `rapt` is not
  present anywhere in the page (not in download links' hrefs, not in the
  DOM) — it's appended by Google only in response to an actual human-driven
  click/passkey challenge, on timing tied to Google's own reauth grace
  window, which this project has no way to observe or force. A
  Playwright-driven synthetic click just follows the link's static href
  (no `rapt` ever appears) — the same category of anti-automation
  protection as the Selenium rejection above, just triggered by click
  *authenticity* rather than sign-in automation. Polling the page for a
  human-triggered `rapt` to transiently appear was also tried and abandoned
  as unreliably timed.
- The part that *is* a durable improvement, and is what's actually
  implemented: `refresh_download_token()` prompts for the curl paste
  directly instead of requiring a `curl.txt` file edit — see "Session
  refresh" below.

### Session refresh — pasted directly, not via `curl.txt`

`refresh_download_token()` (in `download_takeout.py`) prints a short prompt
and reads a multi-line curl command via repeated `input()` calls (submitted
with a blank line), parses it with the same `parse_curl()` used for the
optional legacy `curl.txt` bootstrap, and returns an `AuthState` — headers,
cookies, `rapt`, `job_id` as one `typing.NamedTuple` — applied straight to
the `requests.Session` via `apply_auth_state()`. Nothing is written to disk:
the previous design wrote a fresh `curl.txt` on every refresh, which meant a
full cookie jar sitting in a plaintext file for the run's duration; pasting
directly avoids that with no functional downside, since `curl.txt` was only
ever a hop between the browser and the session object anyway.

`curl.txt` on disk is now purely an **optional legacy bootstrap**: if one
exists at startup, `load_curl_state()` reads it (informational
`describe_curl_age()` staleness warning included); if not,
`refresh_download_token()` prompts for a paste immediately. Refreshes mid-run
never write one.

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

## Layout

A packaged project (`pyproject.toml`, `src/` layout), not flat scripts:

```
src/pygoogletakeoutdownloader/
    __init__.py
    download_takeout.py
    configure_secrets.py
tests/
    test_download_takeout.py
    test_configure_secrets.py
```

Installed (`pip install -e .`) with two console-script entry points
(`[project.scripts]` in `pyproject.toml`): `download-takeout` →
`pygoogletakeoutdownloader.download_takeout:cli`, and `configure-secrets` →
`pygoogletakeoutdownloader.configure_secrets:main`. `cli()` wraps `main()`
with the top-level `KeyboardInterrupt` handling — kept separate so that
handling applies whether invoked via the installed command or
`python src/pygoogletakeoutdownloader/download_takeout.py` directly (only
the `if __name__ == "__main__":` block would otherwise get it).

## Scripts

| File | Purpose |
|---|---|
| `download_takeout.py` | The batch downloader — see Workflow above. Reads `secrets.json`, applies an `AuthState` (headers/cookies/`rapt`/`job_id` — from a pasted curl paste or the optional legacy `curl.txt`) to a `requests.Session` via `apply_auth_state()`, scans `output_directory` for already-completed indices (`scan_completed_indices`), loops from 0 skipping those, builds download URLs (`create_url`), streams each file to a temp file, verifies size and CRC32C (`parse_expected_crc32c`/`compute_file_crc32c`), then hands it to a background `MoveWorker` (moves into `output_directory`, overlapping with the next download). `refresh_download_token()` prompts for a pasted curl command directly — see "Session refresh" above. `patch_config_field()` does a narrow read-modify-write of one `secrets.json` field (used by `MoveWorker` for `last_downloaded_index`), rather than re-dumping a possibly-stale in-memory config snapshot that could revert a concurrent edit. |
| `configure_secrets.py` | Interactive wizard (`SecretsValidator`). Loads/creates `secrets.json`, validates and prompts for `output_directory`/`download_delay`/`max_files`. No credential storage — see "Removed: credential storage" below. |
| `test_download_takeout.py` | `unittest` tests for `create_url()`, `parse_curl()`, `AuthState`, `extract_rapt()`/`extract_job_id()`, `patch_config_field()`, `parse_expected_crc32c()`, `compute_file_crc32c()`, `scan_completed_indices()`. |
| `test_configure_secrets.py` | `pytest` tests for `SecretsValidator`'s config validation/defaults. |

**Removed:** `token_retriever.py` and `secure_token_retriever.py` (Selenium
login scripts) and `test_secure_token_retriever.py`, along with the
`selenium`/`webdriver-manager`/`pytest-selenium` dependencies — see "Why
capture is manual" above.

**Removed: credential storage.** `credentials.py` (shared
keyring-with-plaintext-fallback helpers) and `configure_secrets.py`'s
email/password/`two_factor_secret` prompting/storage (plus its
`--migrate-to-keyring` flag) were deleted outright, along with
`test_credentials.py`. These were already fully dead code — nothing had read
them back for any functional purpose since the Selenium login step that used
to consume them was removed (this used to be flagged here as an unresolved
"Open question"; it's now resolved by deletion). A follow-up idea — keyring-
persisting the *pasted curl's* parsed `AuthState` for crash/restart
resilience within `rapt`'s own short validity window — was considered and
declined too: the benefit is narrow (it doesn't reduce how often a fresh
paste is needed, only survives a restart within the same few-minute window),
and reintroducing `credentials.py` for that alone wasn't judged worth it.

## Config / state files (all gitignored)

- **`secrets.json`** — created by `configure-secrets`. Structure:
  `google_takeout.{max_files, output_directory, download_delay,
  max_pending_moves}`, `authentication.{last_downloaded_index}`.
  `max_pending_moves` (default 2) caps how many fully-downloaded-but-not-
  yet-moved files can queue up in `TEMP_DIR` before the download loop
  blocks waiting for `MoveWorker` to catch up — see "Backpressure" below.
- **`curl.txt`** — optional legacy bootstrap only; not written by
  `refresh_download_token()` anymore — see "Session refresh" above.

## Dependencies

Declared in `pyproject.toml` (setuptools `src/`-layout build): `requests`,
`crc32c` (CRC32C verification — see above), `tqdm` (per-file progress bar).
Dev/test extras (`pip install -e ".[dev]"`): `pytest`, `coverage`. No
Chrome/Chromium/chromedriver needed. `keyring`/`secretstorage`/`pyotp`/
`structlog`/`urllib3` were all removed — the first three were only used by
the now-deleted credential storage; the last two were already-unused,
pre-existing dependencies.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

### Fixed in the backpressure pass (this pass)

- **Sustained download rate faster than the destination's sustained write
  rate let an unbounded backlog of fully-downloaded-but-not-yet-moved files
  pile up in `TEMP_DIR`, with no ceiling.** Confirmed against a real run on
  a remote server writing to a direct USB-attached HDD (~6MB/s sustained
  download vs. ~2MB/s sustained write): `MoveWorker`'s queue was
  unbounded (`queue.Queue()`) and `submit()` never blocked, so the main
  loop just kept downloading as fast as the network allowed regardless of
  whether the mover had caught up. Combined with the bug below, this meant
  a single transient move failure could strand dozens of already-downloaded
  files. Fixed: the queue is now bounded to `max_pending_moves` (default
  2, configurable in `secrets.json`) — `submit()`'s existing `queue.put()`
  already blocks once full, so a slow destination now naturally throttles
  the download loop down to its own sustained pace instead of racing ahead
  of it. `_run()`'s loop always calls `queue.get()` (even in the
  post-failure skip branch), so this can't deadlock.
- **One transient `OSError` moving a file permanently abandoned every
  subsequent queued move for the rest of the run**, even though those files
  were already fully downloaded and verified — plausible on a
  directly-attached HDD under sustained large sequential writes (controller
  stall, brief disconnect, journal flush hangup), not necessarily a dead
  destination. Fixed: `MoveWorker._run()` now retries a failing move a few
  times (`MOVE_RETRY_DELAYS`, short increasing backoff) before falling back
  to the existing stop-everything-else behavior, which is still correct for
  a genuinely unreachable destination.
- `check_disk_space(outdir, ...)` before queuing a move now checks for
  `max_pending_moves` files' worth of free space, not just the current one,
  since that's the real worst-case backlog the destination may need to
  absorb at once.

### Fixed in the session-refresh / packaging pass (this pass)

- **`MoveWorker` used to silently revert concurrent edits to
  `secrets.json`.** It held a config dict snapshot from process start and
  re-dumped the *entire* thing on every successful move; any edit landing on
  disk after the run started (a second invocation's own write, a hand edit)
  got clobbered by the next move. Confirmed in practice. Fixed via
  `patch_config_field()` — reads fresh from disk immediately before writing
  just the one field each caller owns (`last_downloaded_index` for
  `MoveWorker`).
- **Console noise on a failed download.** A non-200 or HTML response used to
  dump the full response body (often a wall of Google's minified JS/JSON
  page state) straight to the console via `describe_error_response()`. That
  detail now only goes to the debug log; the console gets one short,
  actionable line.
- Restructured from flat scripts into a `pyproject.toml`/`src`-layout
  package with `download-takeout`/`configure-secrets` console scripts — see
  "Layout" above. `requirements.txt` is gone.
- Removed dead credential storage (`credentials.py`,
  `configure_secrets.py`'s email/password/2FA handling) — see "Removed:
  credential storage" above.

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
- **Downloads now stage in a local directory before moving
  to `output_directory`** (originally a repo-local `temp_download/`, later
  moved to a stable subdirectory under the system temp folder — see
  Workflow step 3 above), so the slow/expensive part (streaming from
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
  both the local staging directory before writing and `output_directory`
  before queuing the move, so a full disk is caught upfront with a clear
  message instead of surfacing mid-transfer as an `OSError`.
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
- **Local staging moved out of the repo, into the system temp dir.** The
  staging directory (`TEMP_DIR` in `download_takeout.py`) used to be
  `<repo>/temp_download/`; it's now `tempfile.gettempdir() /
  'pygoogletakeoutdownloader'` (e.g. `/tmp/pygoogletakeoutdownloader` on
  Linux). Kept as a stable, fixed-name directory (not a fresh
  `tempfile.mkdtemp()` per run) specifically so the existing crash-resume
  behavior — a `.part` file that finished downloading but never got moved
  is detected and reused, re-verified via CRC32C, on the next run — still
  works.
