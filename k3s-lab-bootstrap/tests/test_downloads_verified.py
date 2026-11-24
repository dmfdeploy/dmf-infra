"""Static check: every download in a playbook or role is verified before it is used.

These forms fail:

* `get_url` without a checksum of the form `<algorithm>:<value>` where the algorithm is sha256, sha384 or sha512. An empty value, or one
  with no algorithm, keeps whatever the server sent.
* `uri` with `dest`, and `unarchive` with a URL as `src`. Neither verifies what it
  writes, so the file must come through a `get_url` that has a checksum.
* `kubernetes.core.k8s` (or `k8s`) whose `src` is a URL of any scheme, or a template that
  neither starts with a literal `/` nor is `{{ name.path }}` for a `name` that a `tempfile`
  task in the same file registers. The module fetches a URL and applies it with no check,
  and a template can hide a URL, so download with a checksummed `get_url` and apply the
  local file. The check reads `src` only; a `definition` built from a URL lookup is not scanned.
* A `curl` or `wget` command that writes a file, redirects into one, runs inside command
  substitution, or pipes into a shell. The file or script is used with no chance to
  compare it with a known value first.

Fix a failure by downloading with `get_url` and a pinned `checksum: "sha256:<value>"`,
then running the file. Helm charts are third-party sources and are skipped.
"""
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"charts", "node_modules", "fixtures", "tests"}
MODULE = re.compile(r"^(?:ansible\.(?:builtin|legacy)\.|kubernetes\.core\.)?(get_url|unarchive|uri|k8s)$")
# Only SHA-2 hashes of 256 bits or more. md5, sha1 and sha224 are too weak to trust a download.
CHECKSUM = re.compile(r"^(?:sha256|sha384|sha512):.+$", re.I)
# A download tool inside a command, with the ways it can leave bytes on disk or run them.
TOOL = r"(?<![\w./-])(?:curl|wget)\b"
UNVERIFIED_TOOL_USE = [
    ("download piped into a shell", re.compile(TOOL + r"[^\n|]*\|\s*(?:sudo\s+)?(?:ba|da)?sh\b")),
    ("download run through command substitution", re.compile(r"(?:\$\(|<\(|`)\s*" + TOOL)),
    ("download written to a file", re.compile(TOOL + r"[^\n|;&]*\s(?:-[A-Za-z]*[oO]\b|--output\b|--remote-name\b|--output-dir\b)")),
    ("download redirected to a file", re.compile(TOOL + r"[^\n|;&<]*[^\d&<>]>>?\s*(?!/dev/null|&)\S")),
    ("wget saves a file by default", re.compile(r"(?<![\w./-])wget\s+\S(?![^\n|;&]*\s-[A-Za-z]*O\s*-)(?![^\n|;&]*--output-document[= ]-)")),
]
# Each entry is a file and the exact `src` that `k8s` may fetch or template without a check.
# These two tasks apply an upstream manifest without a checksum and must move to the checked
# path: a checksummed `get_url`, then `k8s` on the local file. Add nothing else.
ALLOWED_K8S_SRC = {
    ("playbooks/330-longhorn.yml", "{{ longhorn_manifest_url }}"),
    ("roles/base/metallb/tasks/main.yml", "{{ metallb_manifest_url }}"),
}
URL_SCHEME = re.compile(r"^\s*[a-z][a-z0-9+.-]*://", re.I)
REGISTERED_PATH = re.compile(r"^\{\{\s*(\w+)\.path\s*\}\}$")
TEMPFILE = re.compile(r"^(?:ansible\.builtin\.)?tempfile$")
# Commands that fetch without installing anything. Each entry is a file and the complete
# command line (continuation lines joined, whitespace collapsed), and allows exactly that line.
# Any other change to the line, such as a second command, fails the check.
ALLOWED = {
    # Queries the source registry's catalog, a list of names that the script only reads. The
    # images it then copies are verified by their registry digests.
    (
        "roles/base/zot-mirror/templates/zot-mirror-script.yml.j2",
        'CODE="$(curl -sS --max-time 60 -K "${CURL_CFG}" -D "${SCRATCH}/catalog.headers" '
        '-o "${SCRATCH}/catalog.body" -w \'%{http_code}\' '
        '"${SOURCE_URL}/v2/_catalog?n=${CATALOG_PAGE_SIZE}")"',
    ),
}


def tempfile_registers(node):
    """Names that a `tempfile` task in the document registers; their `.path` is a local file."""
    names = set()
    if isinstance(node, list):
        for item in node:
            names |= tempfile_registers(item)
    elif isinstance(node, dict):
        if any(TEMPFILE.match(str(key)) for key in node) and "register" in node:
            names.add(str(node["register"]))
        for value in node.values():
            names |= tempfile_registers(value)
    return names


def k8s_src_unchecked(src, name, temp_names):
    if (name, src) in ALLOWED_K8S_SRC:
        return False
    if URL_SCHEME.match(src):
        return True
    if "{{" not in src:
        return False
    registered = REGISTERED_PATH.match(src.strip())
    return not (src.startswith("/") or (registered and registered.group(1) in temp_names))


def module_problems(node, name="", temp_names=frozenset()):
    """Yield a message for each download task in a parsed YAML document."""
    if isinstance(node, list):
        for item in node:
            yield from module_problems(item, name, temp_names)
    elif isinstance(node, dict):
        for key, value in node.items():
            found = MODULE.match(str(key))
            args = value if isinstance(value, dict) else {}
            kind = found.group(1) if found else None
            if kind == "get_url" and not CHECKSUM.match(str(args.get("checksum", "")).strip()):
                yield f"get_url of {args.get('url')} has no checksum of the form 'sha256:<value>'"
            elif kind == "uri" and "dest" in args:
                yield f"uri of {args.get('url')} writes a file; use get_url with a checksum"
            elif kind == "unarchive" and re.match(r"https?://", str(args.get("src", ""))):
                yield f"unarchive reads {args['src']} directly, with no checksum"
            elif kind == "k8s" and k8s_src_unchecked(str(args.get("src", "")), name, temp_names):
                yield f"k8s applies {args['src']}, which may be a URL, with no checksum; download it with get_url first"
            else:
                yield from module_problems(value, name, temp_names)


def problems_in(text, is_yaml, name=""):
    found = []
    if is_yaml:
        for document in yaml.safe_load_all(text):
            found.extend(module_problems(document, name, tempfile_registers(document)))
    # Join continuation lines and drop comments, then scan each command line.
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for number, line in enumerate(re.sub(r"\\\n\s*", " ", code).splitlines(), 1):
        if (name, " ".join(line.split())) in ALLOWED:
            continue
        for label, pattern in UNVERIFIED_TOOL_USE:
            if pattern.search(line):
                found.append(f"{label}: {line.strip()[:80]}")
    return found


class DownloadsAreVerified(unittest.TestCase):
    def test_every_download_is_verified(self):
        failures = []
        for path in sorted(ROOT.rglob("*")):
            relative = path.relative_to(ROOT)
            if not path.is_file() or SKIP_DIRS & set(relative.parts):
                continue
            if path.suffix not in {".yml", ".yaml", ".j2", ".sh"}:
                continue
            failures.extend(
                f"{relative}: {p}"
                for p in problems_in(path.read_text(encoding="utf-8"), path.suffix in {".yml", ".yaml"}, str(relative))
            )
        self.assertEqual([], failures)

    def test_planted_downloads_are_caught(self):
        def task(module, body):
            return f"- name: x\n  {module}:\n{body}"

        planted = {
            "get_url without checksum": task("ansible.builtin.get_url", "    url: https://e.test/a\n    dest: /tmp/a\n"),
            "get_url with an empty checksum": task("ansible.builtin.get_url", "    url: https://e.test/a\n    dest: /tmp/a\n    checksum: ''\n"),
            "get_url with no algorithm": task("ansible.builtin.get_url", "    url: https://e.test/a\n    dest: /tmp/a\n    checksum: 00ab\n"),
            "get_url with only an algorithm": task("ansible.builtin.get_url", "    url: https://e.test/a\n    dest: /tmp/a\n    checksum: 'sha256:'\n"),
            "get_url with md5": task("ansible.builtin.get_url", "    url: https://e.test/a\n    dest: /tmp/a\n    checksum: 'md5:00ab'\n"),
            "get_url with sha1": task("ansible.builtin.get_url", "    url: https://e.test/a\n    dest: /tmp/a\n    checksum: 'sha1:00ab'\n"),
            "get_url with sha224": task("ansible.builtin.get_url", "    url: https://e.test/a\n    dest: /tmp/a\n    checksum: 'sha224:00ab'\n"),
            "get_url with an unknown algorithm": task("ansible.builtin.get_url", "    url: https://e.test/a\n    dest: /tmp/a\n    checksum: 'crc32:00ab'\n"),
            "uri with dest": task("ansible.builtin.uri", "    url: https://e.test/a\n    dest: /tmp/a\n"),
            "k8s applying a URL": task("kubernetes.core.k8s", "    src: https://e.test/a.yaml\n    state: present\n"),
            "short k8s applying a URL": task("k8s", "    src: http://e.test/a.yaml\n"),
            "k8s applying a templated URL variable": task("kubernetes.core.k8s", "    src: \"{{ manifest_url }}\"\n"),
            "k8s applying ftp": task("kubernetes.core.k8s", "    src: ftp://e.test/a.yaml\n"),
            "k8s applying uppercase HTTPS": task("kubernetes.core.k8s", "    src: HTTPS://e.test/a.yaml\n"),
            "k8s applying a path from a plain mapping": (
                "- name: v\n  vars:\n    manifest:\n      path: https://e.test/a.yaml\n  kubernetes.core.k8s:\n    src: \"{{ manifest.path }}\"\n"
            ),
            "k8s applying a URL after a template": task("kubernetes.core.k8s", "    src: \"{{ base }}/a.yaml\"\n"),
            "k8s applying a template around a URL": task("kubernetes.core.k8s", "    src: \"https://e.test/{{ v }}/a.yaml\"\n"),
            "unarchive from a URL": task("ansible.builtin.unarchive", "    src: https://e.test/a.tgz\n    dest: /tmp\n    remote_src: true\n"),
            "curl piped into sh": task("ansible.builtin.shell", "    cmd: curl -fsSL https://e.test/i.sh | sh\n"),
            "wget piped into sudo bash": task("ansible.builtin.shell", "    cmd: wget -qO- https://e.test/i.sh | sudo bash\n"),
            "curl to a file, then sh": task("ansible.builtin.shell", "    cmd: curl -fsSLo /tmp/x https://e.test/x && sh /tmp/x\n"),
            "curl --output in command": task("ansible.builtin.command", "    cmd: curl --output /tmp/x https://e.test/x\n"),
            "curl redirected in raw": task("ansible.builtin.raw", "    cmd: curl https://e.test/x > /tmp/x\n"),
            "wget to a file": task("ansible.builtin.command", "    cmd: wget https://e.test/x\n"),
            "two get_url, one unverified": (
                task("ansible.builtin.get_url", "    url: https://e.test/a\n    dest: /tmp/a\n    checksum: sha256:00\n")
                + task("ansible.builtin.get_url", "    url: https://e.test/b\n    dest: /tmp/b\n")
            ),
            "shell run from command substitution": task("ansible.builtin.shell", "    cmd: sh -c \"$(curl -fsSL https://e.test/i.sh)\"\n"),
        }
        for name, text in planted.items():
            with self.subTest(name):
                self.assertTrue(problems_in(text, True), name)

    def test_allowed_line_is_exact(self):
        name, line = next(iter(ALLOWED))
        self.assertEqual([], problems_in(line + "\n", False, name))
        self.assertTrue(problems_in(line + " && curl -o /tmp/x https://e.test/x\n", False, name))
        self.assertTrue(problems_in(line.replace("catalog.body", "other.body") + "\n", False, name))
        self.assertTrue(problems_in(line + "\n", False, "other/file.j2"))

    def test_k8s_url_allowance_is_scoped_to_its_file(self):
        for name, src in sorted(ALLOWED_K8S_SRC):
            text = f"- name: x\n  kubernetes.core.k8s:\n    src: \"{src}\"\n"
            self.assertEqual([], problems_in(text, True, name))
            self.assertTrue(problems_in(text, True, "roles/other/tasks/main.yml"))
            self.assertTrue(problems_in(text, True))

    def test_verified_download_passes(self):
        text = (
            "- name: x\n  ansible.builtin.get_url:\n    url: https://e.test/a\n    dest: /tmp/a\n"
            "    checksum: sha256:00\n"
            "- name: y\n  ansible.builtin.get_url:\n    url: https://e.test/b\n    dest: /tmp/b\n"
            "    checksum: \"sha256:{{ b_sha256 }}\"\n"
            "- name: z\n  ansible.builtin.command: curl -s https://e.test/health\n"
            "- name: k\n  kubernetes.core.k8s:\n    src: /tmp/a.yaml\n    state: present\n"
            "- name: p\n  kubernetes.core.k8s:\n    src: \"/tmp/a-{{ ns }}.yaml\"\n    state: present\n"
            "- name: f\n  ansible.builtin.tempfile:\n    state: file\n  register: _file\n"
            "- name: t\n  kubernetes.core.k8s:\n    src: \"{{ _file.path }}\"\n    state: present\n"
        )
        self.assertEqual([], problems_in(text, True))


if __name__ == "__main__":
    unittest.main()
