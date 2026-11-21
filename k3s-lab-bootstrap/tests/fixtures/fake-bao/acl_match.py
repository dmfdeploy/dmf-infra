#!/usr/bin/env python3
"""OpenBao's ACL path matching, for the fake bao and for the tests of a policy file.

The rules follow OpenBao's policy documentation and vault/acl.go:

- A request path is the path as the server sees it. A list request reaches the server with a
  trailing slash (`bao list auth/token/roles` asks for `auth/token/roles/`), and that slash
  is part of the path. A read has no slash added.
- A stanza path with no wildcard matches only that exact path.
- `+` as a whole segment matches exactly one non-empty segment.
- `*` as the last character of a stanza path is a glob: it matches any text from there on,
  empty text included. A `*` anywhere else is an ordinary character.
- A `deny` among the capabilities of the matching stanza refuses everything.
- When several stanzas match, the most specific one decides: an exact path before a path with
  `+`, a path with `+` before a glob, then the longer path. The policies this repository
  checks never overlap, and their tests say so.

Command line, for the fake:  acl_match.py POLICY_FILE OPERATION PATH
The exit status is 0 when the policy grants the operation on the path.
"""
import re
import sys

STANZA = re.compile(r'path\s+"([^"]+)"\s*\{([^}]*)\}')
CAPABILITIES = re.compile(r"capabilities\s*=\s*\[([^\]]*)\]")


def parse_policy(text):
    """The stanzas of an HCL policy as a list of (path, capabilities), in file order."""
    stanzas = []
    for path, body in STANZA.findall(text):
        found = CAPABILITIES.search(body)
        stanzas.append((path, re.findall(r'"(\w+)"', found.group(1)) if found else []))
    return stanzas


def request_path(operation, path):
    """The path the server matches: a list request ends in a slash."""
    if operation == "list" and not path.endswith("/"):
        return path + "/"
    return path


def matches(pattern, path):
    glob = pattern.endswith("*")
    segments = (pattern[:-1] if glob else pattern).split("/")
    parts = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "+":
            parts.append("[^/]+")
        else:
            parts.append(re.escape(segment))
        if last and glob:
            parts[-1] += ".*"
    return re.fullmatch("/".join(parts), path) is not None


def specificity(pattern):
    return (not pattern.endswith("*"), "+" not in pattern.split("/"), len(pattern))


def capabilities_on(stanzas, operation, path):
    """The capabilities of the stanza that decides for this request, or [] when none matches."""
    path = request_path(operation, path)
    found = [(pattern, caps) for pattern, caps in stanzas if matches(pattern, path)]
    if not found:
        return []
    return max(found, key=lambda item: specificity(item[0]))[1]


def allows(stanzas, operation, path):
    caps = capabilities_on(stanzas, operation, path)
    return "deny" not in caps and operation in caps


if __name__ == "__main__":
    policy_file, operation, path = sys.argv[1:4]
    with open(policy_file) as handle:
        sys.exit(0 if allows(parse_policy(handle.read()), operation, path) else 1)
