#!/usr/bin/env python3
"""Fail unless the installed ansible-core is the version in requirements-ansible-core.txt.

CI installs ansible-core from that file, the same file the AWX execution
environment build reads. This checks what ended up installed: the pip
distribution and the first line of `ansible --version` must both name the pin.
"""
import argparse
import importlib.metadata
import pathlib
import re
import subprocess
import sys

PIN_FILE = pathlib.Path(__file__).resolve().parent.parent / "requirements-ansible-core.txt"
PIN = re.compile(r"^ansible-core==([0-9]+\.[0-9]+\.[0-9]+)$")
CLI = re.compile(r"^ansible \[core ([0-9]+\.[0-9]+\.[0-9]+)\]")


def check(pin_text, dist_version, cli_line):
    """Return the reasons the installed core is not the pin; empty when it is."""
    lines = [line.strip() for line in pin_text.splitlines() if line.strip()]
    match = PIN.match(lines[0]) if len(lines) == 1 else None
    if not match:
        return ["the pin file must hold exactly one line, ansible-core==X.Y.Z"]
    pinned, problems = match.group(1), []
    if dist_version != pinned:
        problems.append(f"pip reports ansible-core {dist_version}, the pin is {pinned}")
    cli = CLI.match(cli_line or "")
    if not cli or cli.group(1) != pinned:
        problems.append(f"`ansible --version` reports {cli_line!r}, the pin is {pinned}")
    return problems


def installed():
    try:
        dist = importlib.metadata.version("ansible-core")
    except importlib.metadata.PackageNotFoundError:
        dist = None
    result = subprocess.run(["ansible", "--version"], capture_output=True, text=True, check=False)
    return dist, result.stdout.splitlines()[0] if result.stdout else ""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pin-file", type=pathlib.Path, default=PIN_FILE)
    args = parser.parse_args(argv)
    dist, cli_line = installed()
    problems = check(args.pin_file.read_text(encoding="utf-8"), dist, cli_line)
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    if not problems:
        print(f"ansible-core {dist} matches {args.pin_file.name}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
