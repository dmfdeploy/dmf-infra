#!/usr/bin/env python3
"""Fail closed until a safe Forgejo OIDC subject migration is implemented."""

import argparse
import sys


def validate_subject_mode(mode):
    if mode != "user_username":
        raise ValueError(
            "Forgejo OIDC subject changes are blocked until a supported, "
            "version-specific migration for existing identity bindings is implemented and verified"
        )
    return mode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode")
    args = parser.parse_args(argv)
    try:
        validate_subject_mode(args.mode)
    except ValueError as exc:
        print(f"Forgejo OIDC subject mode rejected: {exc}", file=sys.stderr)
        return 1
    print("Forgejo OIDC subject mode accepted: preserving username-backed identities.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
