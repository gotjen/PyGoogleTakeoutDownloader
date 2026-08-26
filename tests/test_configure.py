#!/usr/bin/env python3

import json

import pytest

from pygoogletakeoutdownloader.configure import ConfigValidator


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({
        'google_takeout': {
            'max_files': 277,
            'output_directory': str(tmp_path),
            'download_delay': 5,
        },
        'authentication': {'last_downloaded_index': 0},
    }))
    return path


def test_validate_config_passes_for_valid_config(config_path):
    validator = ConfigValidator(config_path=str(config_path))
    assert validator.validate_config() is True


def test_validate_config_flags_invalid_output_directory(tmp_path, config_path):
    validator = ConfigValidator(config_path=str(config_path))
    validator.config['google_takeout']['output_directory'] = str(tmp_path / 'does-not-exist' / 'nested')
    assert validator.validate_config() is False


def test_validate_config_flags_non_positive_download_delay(config_path):
    validator = ConfigValidator(config_path=str(config_path))
    validator.config['google_takeout']['download_delay'] = 0
    assert validator.validate_config() is False


def test_validate_config_flags_non_positive_max_files(config_path):
    validator = ConfigValidator(config_path=str(config_path))
    validator.config['google_takeout']['max_files'] = -1
    assert validator.validate_config() is False


def test_save_config_writes_config_as_is(config_path):
    validator = ConfigValidator(config_path=str(config_path))
    validator.config['google_takeout']['download_delay'] = 10
    validator.save_config()

    on_disk = json.loads(config_path.read_text())
    assert on_disk['google_takeout']['download_delay'] == 10


def test_default_config_created_when_file_missing(tmp_path):
    missing_path = tmp_path / 'config.json'
    validator = ConfigValidator(config_path=str(missing_path))

    assert validator.config['google_takeout']['max_files'] == 277
    assert 'output_directory' in validator.config['google_takeout']

# Path: test_configure.py
