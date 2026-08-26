#!/usr/bin/env python3

import os
import json
import getpass
import sys
import re
import logging

import credentials

class SecretsValidator:
    def __init__(self, config_path='secrets.json'):
        """
        Initialize secrets validator and configuration manager
        
        :param config_path: Path to secrets configuration file
        """
        # Configure logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(sys.stdout)
            ]
        )
        self.logger = logging.getLogger(__name__)

        self.config_path = config_path
        self.config = self._load_config()
        # Keys successfully written to keyring this run; save_config()
        # blanks these on disk instead of writing them out as plaintext.
        self._keyring_backed = set()

    def _load_config(self):
        """
        Load existing configuration or create default
        
        :return: Configuration dictionary
        """
        try:
            with open(self.config_path, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return self._create_default_config()
        except json.JSONDecodeError:
            self.logger.error(f"{self.config_path} is not a valid JSON file.")
            sys.exit(1)

    def _create_default_config(self):
        """
        Create a default configuration structure
        
        :return: Default configuration dictionary
        """
        return {
            "google_takeout": {
                "email": "",
                "password": "",
                "two_factor_secret": "",
                "max_files": 277,
                "output_directory": "/mnt/f/GoogleTakeout",
                "download_delay": 5
            },
            "authentication": {
                "job_id": "",
                "last_downloaded_index": 0,
                "last_token_refresh": None
            },
            "proxy": {
                "use_proxy": False,
                "proxy_type": "",
                "proxy_host": "",
                "proxy_port": None,
                "proxy_username": "",
                "proxy_password": ""
            },
            "logging": {
                "log_file": "takeout_download.log",
                "log_level": "INFO"
            }
        }

    def _validate_email(self, email):
        """
        Validate email format
        
        :param email: Email address to validate
        :return: Boolean indicating email validity
        """
        email_regex = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        return re.match(email_regex, email) is not None

    def _store_credential(self, service, username, value):
        """
        Store a credential, preferring the OS keyring.

        Always keeps the just-entered value in self.config in memory for
        the rest of this run — regardless of where it's authoritatively
        stored — so validation logic (e.g. the email prompt loop) has
        something to check. save_config() is responsible for blanking any
        keyring-backed field before it hits disk.

        :param service: Keyring service name
        :param username: Credential key (email/password/two_factor_secret)
        :param value: Credential value
        """
        stored_in_keyring = credentials.set_credential(
            username, value, service=service, logger=self.logger
        )

        if service == 'google_takeout':
            self.config.setdefault('google_takeout', {})[username] = value

        if stored_in_keyring:
            self.logger.info(f"Credential stored securely for {username}")
            self._keyring_backed.add(username)
            self.save_config()
            return True

        # Fallback to configuration file (less secure)
        try:
            self.save_config()
            self.logger.warning("Credential stored in configuration file (not recommended)")
            return False
        except Exception as e:
            self.logger.error(f"Failed to store credential: {e}")
            return False

    def prompt_for_missing_info(self):
        """
        Interactively prompt for missing or invalid configuration
        """
        # Email validation and input. Check keyring first (via
        # credentials.get_credential) so an already-migrated email doesn't
        # trigger a re-prompt just because it's blank in self.config.
        while not self._validate_email(
            credentials.get_credential('email', self.config, logger=self.logger) or ''
        ):
            email = input("Enter your Google account email: ").strip()
            if self._validate_email(email):
                # Attempt to store email securely
                self._store_credential('google_takeout', 'email', email)
            else:
                print("Invalid email format. Please try again.")

        # Password input (always prompt securely)
        password = getpass.getpass("Enter your Google account password: ")
        if password:
            # Attempt to store password securely
            self._store_credential('google_takeout', 'password', password)

        # Two-factor secret (optional)
        two_factor = input("Enter two-factor secret (optional, press Enter to skip): ").strip()
        if two_factor:
            # Attempt to store two-factor secret
            self._store_credential('google_takeout', 'two_factor_secret', two_factor)

        # Output directory validation
        while True:
            output_dir = input(f"Enter output directory (current: {self.config['google_takeout']['output_directory']}): ").strip()
            if not output_dir:
                break
            
            if os.path.isdir(output_dir) or not os.path.exists(output_dir):
                self.config['google_takeout']['output_directory'] = output_dir
                break
            else:
                print("Invalid directory. Please provide a valid path.")

        # Download delay
        while True:
            delay = input(f"Enter download delay in seconds (current: {self.config['google_takeout']['download_delay']}): ").strip()
            if not delay:
                break
            
            try:
                delay_value = int(delay)
                if delay_value > 0:
                    self.config['google_takeout']['download_delay'] = delay_value
                    break
                else:
                    print("Delay must be a positive integer.")
            except ValueError:
                print("Invalid input. Please enter a number.")

        # Save updated configuration
        self.save_config()

    def save_config(self):
        """
        Save updated configuration to file.

        Any credential successfully stored in the keyring this run is
        blanked out in the on-disk copy — self.config keeps the real
        in-memory value for the rest of this run, but the file never gets
        a plaintext copy of a keyring-backed secret.
        """
        to_write = json.loads(json.dumps(self.config))
        google_takeout = to_write.get('google_takeout', {})
        for key in self._keyring_backed:
            if key in google_takeout:
                google_takeout[key] = ''

        try:
            with open(self.config_path, 'w') as f:
                json.dump(to_write, f, indent=4)
            self.logger.info(f"Configuration saved to {self.config_path}")
        except Exception as e:
            self.logger.error(f"Error saving configuration: {e}")
            sys.exit(1)

    def validate_config(self):
        """
        Comprehensive configuration validation
        
        :return: Boolean indicating configuration validity
        """
        errors = []

        # Email validation — check keyring first, since a migrated/keyring-
        # backed email is blanked out in self.config['google_takeout']['email'].
        email = credentials.get_credential('email', self.config, logger=self.logger)
        if not self._validate_email(email or ''):
            errors.append("Invalid email address")

        # Output directory validation
        output_dir = self.config['google_takeout']['output_directory']
        if not os.path.isdir(output_dir) and not os.path.exists(output_dir):
            errors.append(f"Invalid output directory: {output_dir}")

        # Download delay validation
        delay = self.config['google_takeout']['download_delay']
        if not isinstance(delay, int) or delay <= 0:
            errors.append("Download delay must be a positive integer")

        # Display errors and prompt for correction
        if errors:
            print("Configuration Errors:")
            for error in errors:
                print(f"- {error}")
            return False

        return True

    def migrate_plaintext_to_keyring(self):
        """
        One-time migration: move any plaintext email/password/
        two_factor_secret still sitting in secrets.json into the OS
        keyring, blanking each field on disk once its keyring write is
        verified. Fields keyring can't accept are left as plaintext.

        :return: True if every migratable field was moved, False otherwise
        """
        if not credentials.is_keyring_available():
            self.logger.error(
                "Keyring is not available on this system — nothing to migrate."
            )
            return False

        google_takeout = self.config.get('google_takeout', {})
        migrated, skipped = [], []

        for key in ('email', 'password', 'two_factor_secret'):
            value = google_takeout.get(key)
            if not value:
                continue
            if credentials.set_credential(key, value, logger=self.logger):
                self._keyring_backed.add(key)
                migrated.append(key)
            else:
                skipped.append(key)

        if migrated:
            self.save_config()
            self.logger.info(
                f"Migrated to keyring and cleared from secrets.json: {', '.join(migrated)}"
            )
        if skipped:
            self.logger.warning(f"Could not migrate (left as plaintext): {', '.join(skipped)}")
        if not migrated and not skipped:
            self.logger.info("No plaintext credentials found to migrate.")

        return bool(migrated) and not skipped

def main():
    """
    Main entry point for secrets configuration
    """
    print("Google Takeout Download Configuration Wizard")
    print("-------------------------------------------")

    if '--migrate-to-keyring' in sys.argv:
        validator = SecretsValidator()
        validator.migrate_plaintext_to_keyring()
        return

    # Check for keyring availability
    if not credentials.is_keyring_available():
        print("\nWARNING: Keyring module not available.")
        print("Credentials will be stored in the configuration file.")
        print("This is NOT recommended for security reasons.\n")

    validator = SecretsValidator()

    # Validate existing configuration
    if not validator.validate_config():
        print("\nConfiguration needs updating.")
        validator.prompt_for_missing_info()
    else:
        print("\nConfiguration is valid.")
        choice = input("Would you like to update the configuration? (y/N): ").strip().lower()
        if choice == 'y':
            validator.prompt_for_missing_info()

if __name__ == "__main__":
    main()

# Path: configure_secrets.py
