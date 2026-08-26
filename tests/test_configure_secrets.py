#!/usr/bin/env python3

import json
from unittest.mock import patch

import pytest

from configure_secrets import SecretsValidator


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / 'secrets.json'
    path.write_text(json.dumps({
        'google_takeout': {
            'email': 'plaintext@example.com',
            'password': 'plaintext-password',
            'two_factor_secret': 'PLAINTEXT_SECRET',
            'max_files': 277,
            'output_directory': str(tmp_path),
            'download_delay': 5,
        },
        'authentication': {'job_id': '', 'last_downloaded_index': 0, 'last_token_refresh': None},
        'proxy': {'use_proxy': False},
        'logging': {'log_file': 'takeout_download.log', 'log_level': 'INFO'},
    }))
    return path


@patch('configure_secrets.credentials.set_credential', return_value=True)
def test_migrate_moves_plaintext_to_keyring_and_blanks_disk(mock_set, config_path):
    validator = SecretsValidator(config_path=str(config_path))
    result = validator.migrate_plaintext_to_keyring()

    assert result is True
    assert {c.args[0] for c in mock_set.call_args_list} == {
        'email', 'password', 'two_factor_secret'
    }

    on_disk = json.loads(config_path.read_text())
    assert on_disk['google_takeout']['email'] == ''
    assert on_disk['google_takeout']['password'] == ''
    assert on_disk['google_takeout']['two_factor_secret'] == ''


@patch('configure_secrets.credentials.set_credential', return_value=False)
def test_migrate_leaves_plaintext_when_keyring_write_fails(mock_set, config_path):
    validator = SecretsValidator(config_path=str(config_path))
    result = validator.migrate_plaintext_to_keyring()

    assert result is False
    on_disk = json.loads(config_path.read_text())
    assert on_disk['google_takeout']['email'] == 'plaintext@example.com'


@patch('configure_secrets.credentials.is_keyring_available', return_value=False)
def test_migrate_no_op_when_keyring_unavailable(mock_available, config_path):
    validator = SecretsValidator(config_path=str(config_path))
    result = validator.migrate_plaintext_to_keyring()

    assert result is False
    on_disk = json.loads(config_path.read_text())
    assert on_disk['google_takeout']['email'] == 'plaintext@example.com'


@patch('configure_secrets.credentials.set_credential', return_value=True)
@patch('configure_secrets.credentials.get_credential')
def test_store_credential_keeps_loop_condition_satisfied_on_keyring_success(
    mock_get, mock_set, config_path
):
    """
    Regression test: _store_credential() used to only update self.config on
    the plaintext-fallback path, so prompt_for_missing_info()'s email loop
    never saw a valid email once keyring storage succeeded and looped
    forever. It must now see the just-entered value immediately.
    """
    validator = SecretsValidator(config_path=str(config_path))
    validator._store_credential('google_takeout', 'email', 'new@example.com')

    assert validator.config['google_takeout']['email'] == 'new@example.com'


@patch('configure_secrets.credentials.set_credential', return_value=False)
def test_store_credential_persists_two_factor_secret_on_plaintext_fallback(mock_set, config_path):
    """
    Regression test: the old fallback branch only special-cased email and
    password, silently dropping two_factor_secret.
    """
    validator = SecretsValidator(config_path=str(config_path))
    validator._store_credential('google_takeout', 'two_factor_secret', 'NEWSECRET')

    on_disk = json.loads(config_path.read_text())
    assert on_disk['google_takeout']['two_factor_secret'] == 'NEWSECRET'

# Path: test_configure_secrets.py
