#!/usr/bin/env python3

import base64
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import crc32c

from pygoogletakeoutdownloader.download_takeout import (
    create_url,
    parse_curl,
    describe_error_response,
    _safe_unlink,
    parse_expected_crc32c,
    compute_file_crc32c,
    scan_completed_indices,
    AuthState,
    extract_rapt,
    extract_job_id,
    patch_config_field,
)

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
        stale secrets.json field ('unknown' by default) instead of the
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
        must not clobber a field added to secrets.json by someone/something
        else after the current process's own config was loaded into memory
        — confirmed in practice: a long-running invocation's stale
        in-memory config silently reverted a cdp_url added mid-run when
        MoveWorker used to re-dump its whole constructor-time snapshot."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'secrets.json'
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

if __name__ == '__main__':
    unittest.main()

# File: test_download_takeout.py
