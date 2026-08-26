#!/usr/bin/env python3

import os
import json
import sys
import logging

class ConfigValidator:
    def __init__(self, config_path='config.json'):
        """
        Initialize configuration validator and manager

        :param config_path: Path to the configuration file
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
                "max_files": 277,
                "output_directory": "/mnt/f/GoogleTakeout",
                "download_delay": 5
            },
            "authentication": {
                "last_downloaded_index": 0
            }
        }

    def prompt_for_missing_info(self):
        """
        Interactively prompt for missing or invalid configuration
        """
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

        # Max files
        while True:
            max_files = input(f"Enter number of indexed files in the export (current: {self.config['google_takeout']['max_files']}): ").strip()
            if not max_files:
                break

            try:
                max_files_value = int(max_files)
                if max_files_value > 0:
                    self.config['google_takeout']['max_files'] = max_files_value
                    break
                else:
                    print("Max files must be a positive integer.")
            except ValueError:
                print("Invalid input. Please enter a number.")

        # Save updated configuration
        self.save_config()

    def save_config(self):
        """
        Save updated configuration to file.
        """
        try:
            with open(self.config_path, 'w') as f:
                json.dump(self.config, f, indent=4)
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

        # Output directory validation
        output_dir = self.config['google_takeout']['output_directory']
        if not os.path.isdir(output_dir) and not os.path.exists(output_dir):
            errors.append(f"Invalid output directory: {output_dir}")

        # Download delay validation
        delay = self.config['google_takeout']['download_delay']
        if not isinstance(delay, int) or delay <= 0:
            errors.append("Download delay must be a positive integer")

        # Max files validation
        max_files = self.config['google_takeout']['max_files']
        if not isinstance(max_files, int) or max_files <= 0:
            errors.append("Max files must be a positive integer")

        # Display errors and prompt for correction
        if errors:
            print("Configuration Errors:")
            for error in errors:
                print(f"- {error}")
            return False

        return True

def main():
    """
    Main entry point for the configuration wizard
    """
    print("Google Takeout Download Configuration Wizard")
    print("-------------------------------------------")

    validator = ConfigValidator()

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

# Path: configure.py
