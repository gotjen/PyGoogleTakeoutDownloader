#!/usr/bin/env python3

import base64
import json
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import crc32c

from pygoogletakeoutdownloader.download_takeout import (
    create_url,
    parse_curl,
    describe_error_response,
    _safe_unlink,
    parse_expected_crc32c,
    compute_file_crc32c,
    scan_completed_indices,
    list_completed_files,
    verify_destination,
    AuthState,
    extract_rapt,
    extract_job_id,
    patch_config_field,
    MoveWorker,
    MOVE_RETRY_DELAYS,
)

def _crc_header(data):
    """x-goog-hash-style header for the CRC32C of `data`, for mocking a
    verify_destination() response."""
    return f"crc32c={base64.b64encode(struct.pack('>I', crc32c.crc32c(data))).decode()}"

class TestDownloader(unittest.TestCase):
    def test_working_url_format(self):
        """Test URL exactly matches format from working download."""
        url = create_url(0, "job-123", "test-rapt")
        self.assertEqual(
            url,
            "https://takeout.google.com/settings/takeout/download?i=0&j=job-123&download=true&rapt=test-rapt"
        )

    def test_valid_curl_parsing(self):
        """Test parsing of complete curl command."""
        curl = """curl 'https://takeout.google.com/settings/takeout/download?i=0&j=123&download=true&rapt=test-rapt' \
            -H 'User-Agent: test' \
            -H 'Accept: test' \
            -b 'cookie1=value1; cookie2=value2'"""

        headers, cookies, rapt, job_id = parse_curl(curl)
        self.assertEqual(headers['User-Agent'], 'test')
        self.assertEqual(cookies['cookie1'], 'value1')
        self.assertEqual(rapt, 'test-rapt')
        self.assertEqual(job_id, '123')

    def test_missing_rapt(self):
        """Test curl command without rapt token."""
        curl = """curl 'https://takeout.google.com/' -H 'Accept: test' -b 'cookie=test'"""
        with self.assertRaises(ValueError):
            parse_curl(curl)

    def test_missing_job_id(self):
        """
        Test curl command without a job id (j=...). This is the bug that
        caused every real download to 500: job_id used to be pulled from a
        stale config.json field ('unknown' by default) instead of the
        captured curl command, so it must now be required here too.
        """
        curl = """curl 'https://takeout.google.com/settings/takeout/download?rapt=test' -b 'cookie=test'"""
        with self.assertRaises(ValueError):
            parse_curl(curl)

    def test_partial_cookies(self):
        """Test curl command with incomplete cookies."""
        curl = """curl 'https://takeout.google.com/settings/takeout/download?j=123&rapt=test' -b 'a=1'"""
        headers, cookies, rapt, job_id = parse_curl(curl)
        self.assertEqual(cookies, {'a': '1'})
        self.assertEqual(job_id, '123')

    def test_parse_curl_returns_auth_state(self):
        """parse_curl() returns an AuthState (tuple-unpackable, but also
        accessible by field) — used by both the manual curl.txt path and
        browser_capture.py's CDP auto-capture path."""
        curl = """curl 'https://takeout.google.com/settings/takeout/download?j=123&rapt=test' -b 'a=1'"""
        auth = parse_curl(curl)
        self.assertIsInstance(auth, AuthState)
        self.assertEqual(auth.rapt, 'test')
        self.assertEqual(auth.job_id, '123')
        self.assertEqual(auth.cookies, {'a': '1'})

    def test_extract_rapt_and_job_id_from_page_url(self):
        """browser_capture.py extracts rapt/job_id from a captured page URL
        (e.g. the archive management page after a refresh) using these same
        helpers parse_curl() uses, so both paths stay in sync."""
        url = (
            "https://takeout.google.com/manage/archive/8c5a1cd4-06b7?"
            "user=12345&rapt=AEjHL4abc"
        )
        self.assertEqual(extract_rapt(url), 'AEjHL4abc')

        download_url = "https://takeout.google.com/takeout/download?j=8c5a1cd4-06b7&i=2&rapt=AEjHL4abc"
        self.assertEqual(extract_job_id(download_url), '8c5a1cd4-06b7')

    def test_extract_rapt_missing_raises(self):
        with self.assertRaises(ValueError):
            extract_rapt("https://takeout.google.com/manage/archive/123")

    def test_describe_error_response_includes_status_and_body(self):
        """Failure diagnostics should surface the response body, not just the status code."""
        response = MagicMock()
        response.status_code = 500
        response.reason = 'Internal Server Error'
        response.headers = {'content-type': 'text/html; charset=utf-8'}
        response.text = '<html>  <body>Something went   wrong</body></html>'

        description = describe_error_response(response)
        self.assertIn('500', description)
        self.assertIn('Internal Server Error', description)
        self.assertIn('Something went wrong', description)

    def test_describe_error_response_truncates_long_body(self):
        response = MagicMock()
        response.status_code = 500
        response.reason = 'Internal Server Error'
        response.headers = {'content-type': 'text/plain'}
        response.text = 'x' * 1000

        description = describe_error_response(response, max_body_chars=50)
        self.assertIn('...', description)
        self.assertLessEqual(len(description.split('Body: ')[1]), 54)

    def test_safe_unlink_swallows_oserror(self):
        """
        A partial-download cleanup on a flaky output mount (e.g. rclone/FUSE)
        can itself raise OSError — that must not propagate and mask whatever
        error triggered the cleanup in the first place.
        """
        path = MagicMock()
        path.exists.return_value = True
        path.unlink.side_effect = OSError("I/O error")

        _safe_unlink(path)  # must not raise

    def test_safe_unlink_removes_existing_file(self):
        path = MagicMock()
        path.exists.return_value = True

        _safe_unlink(path)

        path.unlink.assert_called_once()

    def test_safe_unlink_noop_when_missing(self):
        path = MagicMock()
        path.exists.return_value = False

        _safe_unlink(path)

        path.unlink.assert_not_called()

    def test_filename_format(self):
        """Test output filename pattern."""
        from datetime import datetime
        filename = f"takeout-{datetime.now().strftime('%Y%m%d')}T000000Z-042.zip"
        self.assertRegex(filename, r'takeout-\d{8}T\d{6}Z-\d{3}\.zip')

    def test_parse_expected_crc32c_extracts_value(self):
        """Real header format confirmed against a live Takeout download:
        x-goog-hash: crc32c=DC95Hw==. Also handle a trailing md5= entry,
        since GCS-style responses can carry both."""
        raw = struct.pack('>I', 204437791)
        header = f"crc32c={base64.b64encode(raw).decode()},md5=irrelevant=="
        self.assertEqual(parse_expected_crc32c(header), 204437791)

    def test_parse_expected_crc32c_missing_header(self):
        self.assertIsNone(parse_expected_crc32c(None))
        self.assertIsNone(parse_expected_crc32c(''))

    def test_parse_expected_crc32c_no_crc32c_entry(self):
        self.assertIsNone(parse_expected_crc32c('md5=irrelevant=='))

    def test_compute_file_crc32c_matches_known_value(self):
        data = b'hello world' * 10000  # bigger than one internal chunk
        expected = crc32c.crc32c(data)
        with tempfile.NamedTemporaryFile() as f:
            f.write(data)
            f.flush()
            self.assertEqual(compute_file_crc32c(f.name, chunk_size=1024), expected)

    def test_patch_config_field_preserves_concurrent_edits(self):
        """MoveWorker (and refresh_download_token()'s job_id persistence)
        must not clobber a field added to config.json by someone/something
        else after the current process's own config was loaded into memory
        — confirmed in practice: a long-running invocation's stale
        in-memory config silently reverted a cdp_url added mid-run when
        MoveWorker used to re-dump its whole constructor-time snapshot."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps({
                'google_takeout': {'cdp_url': 'http://127.0.0.1:9222'},
                'authentication': {'last_downloaded_index': 5},
            }))

            # Simulates a concurrent edit landing on disk after this
            # process's own (now-stale) in-memory config was loaded.
            patch_config_field(path, 'authentication', 'last_downloaded_index', 6)

            on_disk = json.loads(path.read_text())
        self.assertEqual(on_disk['authentication']['last_downloaded_index'], 6)
        self.assertEqual(on_disk['google_takeout']['cdp_url'], 'http://127.0.0.1:9222')

    def test_scan_completed_indices(self):
        with tempfile.TemporaryDirectory() as tmp:
            outdir = Path(tmp)
            (outdir / 'takeout-20260101_000000Z-000.zip').touch()
            (outdir / 'takeout-20260101_000000Z-007.zip').touch()
            (outdir / 'not-a-takeout-file.txt').touch()
            self.assertEqual(scan_completed_indices(outdir), {0, 7})

    def test_list_completed_files_maps_index_to_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            outdir = Path(tmp)
            f0 = outdir / 'takeout-20260101_000000Z-000.zip'
            f7 = outdir / 'takeout-20260101_000000Z-007.zip'
            f0.touch()
            f7.touch()
            (outdir / 'not-a-takeout-file.txt').touch()
            self.assertEqual(list_completed_files(outdir), {0: f0, 7: f7})

    def test_verify_destination_all_ok(self):
        """A file whose recomputed CRC32C matches Google's header for that
        index should be left alone and reported 'ok'."""
        with tempfile.TemporaryDirectory() as tmp:
            outdir = Path(tmp)
            data = b'hello world'
            path = outdir / 'takeout-20260101_000000Z-000.zip'
            path.write_bytes(data)

            response = MagicMock()
            response.status_code = 200
            response.headers = {'content-type': 'application/zip', 'x-goog-hash': _crc_header(data)}
            session = MagicMock()
            session.get.return_value = response

            status, rapt, job_id = verify_destination(session, outdir, 'rapt', 'job')

            self.assertEqual(status, 'ok')
            self.assertEqual((rapt, job_id), ('rapt', 'job'))
            response.close.assert_called_once()
            self.assertTrue(path.exists())

    def test_verify_destination_mismatch_deletes_file(self):
        """A stored file that no longer matches Google's checksum (e.g.
        silent corruption on a flaky mount) must be deleted so the normal
        resume pass re-fetches it — same remediation used everywhere else
        in this file on a checksum mismatch."""
        with tempfile.TemporaryDirectory() as tmp:
            outdir = Path(tmp)
            path = outdir / 'takeout-20260101_000000Z-000.zip'
            path.write_bytes(b'actual bytes on disk')

            response = MagicMock()
            response.status_code = 200
            response.headers = {'content-type': 'application/zip', 'x-goog-hash': _crc_header(b'different expected bytes')}
            session = MagicMock()
            session.get.return_value = response

            status, _, _ = verify_destination(session, outdir, 'rapt', 'job')

            self.assertEqual(status, 'refetching')
            self.assertFalse(path.exists())

    def test_verify_destination_404_skipped_without_flagging(self):
        """Google 404ing an already-downloaded index (e.g. a different
        export's job_id) isn't corruption — leave the file alone and don't
        report it as a mismatch."""
        with tempfile.TemporaryDirectory() as tmp:
            outdir = Path(tmp)
            path = outdir / 'takeout-20260101_000000Z-000.zip'
            path.write_bytes(b'data')

            response = MagicMock()
            response.status_code = 404
            session = MagicMock()
            session.get.return_value = response

            status, _, _ = verify_destination(session, outdir, 'rapt', 'job')

            self.assertEqual(status, 'ok')
            self.assertTrue(path.exists())

    def test_verify_destination_aborts_when_refresh_fails(self):
        """A stale session that can't be refreshed should stop verification
        rather than looping or crashing."""
        with tempfile.TemporaryDirectory() as tmp:
            outdir = Path(tmp)
            (outdir / 'takeout-20260101_000000Z-000.zip').write_bytes(b'data')

            response = MagicMock()
            response.status_code = 500
            response.headers = {'content-type': 'text/html'}
            session = MagicMock()
            session.get.return_value = response

            with patch('pygoogletakeoutdownloader.download_takeout.refresh_download_token', return_value=None):
                status, _, _ = verify_destination(session, outdir, 'rapt', 'job')

            self.assertEqual(status, 'aborted')

    def _make_config(self, tmp):
        config_path = Path(tmp) / 'config.json'
        config_path.write_text(json.dumps({
            'google_takeout': {},
            'authentication': {'last_downloaded_index': 0},
        }))
        return config_path

    def test_move_worker_backpressure_blocks_submit_when_queue_full(self):
        """A sustained download rate faster than the destination's write
        rate must not be able to build an unbounded backlog: with
        max_pending_moves=1, a third submit() has to wait for the first
        job's (still in-progress) move to finish and free a slot."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = self._make_config(tmp)
            first_move_release = threading.Event()
            move_calls = []

            def fake_move(src, dst):
                move_calls.append(src)
                if len(move_calls) == 1:
                    self.assertTrue(first_move_release.wait(timeout=5))

            with patch('pygoogletakeoutdownloader.download_takeout.shutil.move', side_effect=fake_move):
                worker = MoveWorker(config_path, max_pending_moves=1)
                try:
                    worker.submit(0, 'a', 'a-out')
                    # Worker thread immediately dequeues job 0 and blocks
                    # inside fake_move; the queue is empty again, so this
                    # is accepted without blocking.
                    worker.submit(1, 'b', 'b-out')

                    third_submitted = threading.Event()

                    def submit_third():
                        worker.submit(2, 'c', 'c-out')
                        third_submitted.set()

                    t = threading.Thread(target=submit_third)
                    t.start()
                    # Queue is full (holding job 1) while job 0's move is
                    # still blocked on the event — submit(2) must not
                    # return yet.
                    self.assertFalse(third_submitted.wait(timeout=0.3))

                    first_move_release.set()
                    self.assertTrue(third_submitted.wait(timeout=5))
                    t.join(timeout=5)
                finally:
                    worker.close()

            self.assertEqual(move_calls, ['a', 'b', 'c'])
            self.assertIsNone(worker.error())

    def test_move_worker_retries_transient_failure_then_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = self._make_config(tmp)
            attempts = []

            def flaky_move(src, dst):
                attempts.append(src)
                if len(attempts) < len(MOVE_RETRY_DELAYS):
                    raise OSError("transient hiccup")

            with patch('pygoogletakeoutdownloader.download_takeout.shutil.move', side_effect=flaky_move), \
                 patch('pygoogletakeoutdownloader.download_takeout.time.sleep'):
                worker = MoveWorker(config_path, max_pending_moves=2)
                worker.submit(0, 'a', 'a-out')
                worker.close()

            self.assertIsNone(worker.error())
            self.assertEqual(len(attempts), len(MOVE_RETRY_DELAYS))
            on_disk = json.loads(config_path.read_text())
            self.assertEqual(on_disk['authentication']['last_downloaded_index'], 1)

    def test_move_worker_gives_up_after_exhausting_retries(self):
        """A persistently unreachable destination should still trip the
        existing stop-everything-else path once retries are exhausted, and
        the queue must keep draining (not deadlock) for jobs submitted
        after the error is recorded."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = self._make_config(tmp)
            call_count = 0

            def always_fails(src, dst):
                nonlocal call_count
                call_count += 1
                raise OSError("destination unreachable")

            with patch('pygoogletakeoutdownloader.download_takeout.shutil.move', side_effect=always_fails), \
                 patch('pygoogletakeoutdownloader.download_takeout.time.sleep'):
                worker = MoveWorker(config_path, max_pending_moves=2)
                worker.submit(0, 'a', 'a-out')
                # Must not block even though job 0 will end up failed —
                # _run()'s skip branch still drains the queue.
                worker.submit(1, 'b', 'b-out')
                worker.close()

            self.assertEqual(call_count, 1 + len(MOVE_RETRY_DELAYS))
            error = worker.error()
            self.assertIsNotNone(error)
            self.assertEqual(error[0], 0)
            on_disk = json.loads(config_path.read_text())
            self.assertEqual(on_disk['authentication']['last_downloaded_index'], 0)

if __name__ == '__main__':
    unittest.main()

# File: test_download_takeout.py
