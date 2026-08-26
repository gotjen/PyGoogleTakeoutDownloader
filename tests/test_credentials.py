#!/usr/bin/env python3

import logging
from unittest.mock import patch

import pytest

from pygoogletakeoutdownloader import credentials


@pytest.fixture
def config():
    return {'google_takeout': {'email': 'plaintext@example.com'}}


def test_is_keyring_available_true_when_module_present():
    assert credentials.is_keyring_available() is True


def test_is_keyring_available_false_when_module_absent():
    with patch.object(credentials, 'keyring', None):
        assert credentials.is_keyring_available() is False


@patch('pygoogletakeoutdownloader.credentials.keyring.get_password')
def test_get_credential_prefers_keyring(mock_get_password, config):
    mock_get_password.return_value = 'from-keyring'
    assert credentials.get_credential('email', config) == 'from-keyring'


@patch('pygoogletakeoutdownloader.credentials.keyring.get_password')
def test_get_credential_falls_back_to_config_when_keyring_empty(mock_get_password, config):
    mock_get_password.return_value = None
    assert credentials.get_credential('email', config) == 'plaintext@example.com'


@patch('pygoogletakeoutdownloader.credentials.keyring.get_password')
def test_get_credential_falls_back_to_config_when_keyring_raises(mock_get_password, config):
    mock_get_password.side_effect = Exception('locked')
    assert credentials.get_credential('email', config) == 'plaintext@example.com'


def test_get_credential_returns_none_when_absent_everywhere():
    with patch.object(credentials, 'keyring', None):
        assert credentials.get_credential('password', {'google_takeout': {}}) is None


def test_get_credential_warns_on_plaintext_fallback(config, caplog):
    with patch.object(credentials, 'keyring', None):
        with caplog.at_level(logging.WARNING):
            credentials.get_credential('email', config)
    assert any('plaintext' in record.message for record in caplog.records)


@patch('pygoogletakeoutdownloader.credentials.keyring.get_password')
@patch('pygoogletakeoutdownloader.credentials.keyring.set_password')
def test_set_credential_verifies_round_trip(mock_set_password, mock_get_password):
    mock_get_password.return_value = 'secret-value'
    assert credentials.set_credential('password', 'secret-value') is True
    mock_set_password.assert_called_once_with('google_takeout', 'password', 'secret-value')


@patch('pygoogletakeoutdownloader.credentials.keyring.get_password')
@patch('pygoogletakeoutdownloader.credentials.keyring.set_password')
def test_set_credential_fails_when_readback_mismatches(mock_set_password, mock_get_password):
    mock_get_password.return_value = 'something-else'
    assert credentials.set_credential('password', 'secret-value') is False


@patch('pygoogletakeoutdownloader.credentials.keyring.set_password')
def test_set_credential_fails_when_keyring_raises(mock_set_password):
    mock_set_password.side_effect = Exception('no backend')
    assert credentials.set_credential('password', 'secret-value') is False


def test_set_credential_false_when_keyring_absent():
    with patch.object(credentials, 'keyring', None):
        assert credentials.set_credential('password', 'secret-value') is False

# Path: test_credentials.py
