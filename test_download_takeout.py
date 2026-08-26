#!/usr/bin/env python3

import unittest
from pathlib import Path
from unittest.mock import MagicMock
from download_takeout import create_url, parse_curl, describe_error_response, _safe_unlink

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

if __name__ == '__main__':
    unittest.main()

# File: test_download_takeout.py
