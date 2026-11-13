#!/usr/bin/env python3
"""Store a GitHub App ID and a local PEM in the central repository, without logging the key."""
import argparse
from pathlib import Path
import subprocess

REPOSITORY = 'openharmony-qemu/mirror-sync'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-id', required=True)
    parser.add_argument('--private-key', type=Path, required=True)
    args = parser.parse_args()
    if not args.app_id.isdigit():
        raise ValueError('Use the numeric GitHub App ID')
    key = args.private_key.resolve(strict=True)
    subprocess.run(['openssl', 'rsa', '-in', str(key), '-check', '-noout'],
                   check=True, stdout=subprocess.DEVNULL)
    # gh encrypts the value with the repository public key before uploading it.
    # Neither the private key nor a long-lived personal token enters command argv.
    with key.open('rb') as stream:
        subprocess.run(['gh', 'secret', 'set', 'MIRROR_APP_PRIVATE_KEY', '--repo', REPOSITORY],
                       stdin=stream, check=True)
    subprocess.run(['gh', 'variable', 'set', 'MIRROR_APP_ID', '--repo', REPOSITORY,
                    '--body', args.app_id], check=True)
    print('Configured the App credentials in ' + REPOSITORY + '. Run a production sync to verify installation access.')


if __name__ == '__main__':
    main()
