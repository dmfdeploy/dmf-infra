"""Static check: a literal container image in a tracked file carries `@sha256:<digest>`.

Covers:

* literal values under image-named keys (`image`, `*_image`, `images`) and the list items
  under them, indented or not. Such a value must end in `@sha256:<64 hex digits>`, with or without a tag.
  A value that mixes literal text and a template, such as `nginx:{{ tag }}`, fails when it
  has no `@sha256:` digest. A folded or
  block value (`>`, `|`) is read as one text.
* Helm `repository:` blocks. Each needs a `digest: sha256:<64 hex digits>` beside it,
  with or without a `tag:`.
* Only the exact (file, value) pairs in ALLOWED pass without a digest.

Does not prove:

* images that a chart supplies from its own defaults, which this repository does not name
  (dmfdeploy/dmfdeploy#1003),
* references stored under other names, such as `backup_container` or `source_repo`, and
* what a template variable holds. A value made only of templates, such as `"{{ img }}"`,
  passes. Review covers both.

Scope: files that `git ls-files` lists under k3s-lab-bootstrap with the suffix `.yml`,
`.yaml`, `.j2`, `.sh` or `.py`. `charts/` (vendored Helm charts), `ee/` (the AWX
execution environment, dmfdeploy/dmfdeploy#975) and `tests/` are out of scope.

Set IMAGE_SCAN_ROOT to scan another checkout; it must be a git work tree.
"""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(os.environ.get("IMAGE_SCAN_ROOT") or Path(__file__).resolve().parent.parent)
SKIP_DIRS = {"charts", "ee", "tests"}
SUFFIXES = {".yml", ".yaml", ".j2", ".sh", ".py"}

# Exact (file relative to k3s-lab-bootstrap, value) pairs that pass without a digest, each with
# the reason, taken from how its consumer uses the value.
RUNNER = "roles/stack/operator/ansible-runner/defaults/main.yml"
AWX = "roles/stack/operator/awx-integration/defaults/main.yml"
ALLOWED = {
    (RUNNER, "registry.dmf.example.com/dmf/awx-ee:{{ awx_ee_image_tag }}"):
        "execution environment image (dmfdeploy/dmfdeploy#975); the tag is awx_ee_image_tag, set in vars/awx-ee.yml",
    (AWX, "zot.zot.svc.cluster.local:5000/dmf/awx-ee"):
        "repository only; awx_ee_catalog_ref (awx-integration/defaults/main.yml) appends awx_ee_image_tag; EE image, dmfdeploy/dmfdeploy#975",
    (AWX, "zot.zot.svc.cluster.local:5000/dmf/nmos-cpp-registry"):
        "repository only; the release tag is nmos_cpp_registry_tag and no digest is carried",
    (AWX, "zot.zot.svc.cluster.local:5000/dmf/nmos-cpp-node"):
        "repository only; the release tag is nmos_cpp_node_tag and no digest is carried",
    (AWX, "zot.zot.svc.cluster.local:5000/dmf/nmos-crosspoint"):
        "mirror of a third-party image; its index digest is nmos_crosspoint_digest, which the chart appends",
    ("roles/stack/operator/cms/templates/values.yml.j2",
     "{{ cms_image_repository | default(dmf_registry_host_effective ~ '/dmf-cms') | to_json }}"):
        "Helm repository only; the tag is cms_image_tag, the release in the dmf-cms VERSION file",
}

TEMPLATE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}")
IMAGE_KEY = re.compile(r"\s*(?:- )?(image|[\w-]*_image|images)\s*:\s*(.*?)\s*$")
FOLDED = re.compile(r"[>|][+-]?\d*")
DIGEST_END = re.compile(r"@sha256:[0-9a-f]{64}$")
SHA = re.compile(r"sha256:[0-9a-f]{64}$")
LIBRENMS_TAG = re.compile(r"[\w][\w.-]*@sha256:[0-9a-f]{64}")


def indent(line):
    return len(line) - len(line.lstrip())


def key_value(line):
    match = re.match(r"""\s*(?:- )?([\w-]+):\s*["']?([^\s"'#]*)["']?\s*(?:#.*)?$""", line)
    return match.groups() if match else (None, None)


def dash(line):
    return line.lstrip().startswith("- ")


def key_column(line):
    return indent(line) + (2 if dash(line) else 0)


def digest_beside(lines, i):
    """The `digest:` value in the YAML mapping that holds line i, or ''. A `- ` line opens a mapping."""
    base, lo, hi = key_column(lines[i]), i, i
    while lo > 0 and not dash(lines[lo]):
        lo -= 1
        if lines[lo].strip() and indent(lines[lo]) < base and not (dash(lines[lo]) and key_column(lines[lo]) == base):
            lo += 1
            break
    while hi + 1 < len(lines) and (not lines[hi + 1].strip() or indent(lines[hi + 1]) >= base):
        hi += 1
    found = ""
    for t in lines[lo:hi + 1]:
        key, value = key_value(t)
        found = value if key == "digest" and key_column(t) == base else found
    return found


def scan_text(rel, text):
    """Return `(line number, problem)` for each image value in one file that fails."""
    lines, out = text.splitlines(), []
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        repo = re.match(r"\s*(?:- )?repository:\s*(.*?)\s*$", line)
        if repo and (rel, repo.group(1)) not in ALLOWED and not SHA.match(digest_beside(lines, i)):
            out.append((i + 1, f"repository {repo.group(1)!r} has no digest: sha256 beside it"))
        key = IMAGE_KEY.match(line)
        if not key:
            continue
        values = [key.group(2)]
        for more in lines[i + 1:]:
            if more.strip() and indent(more) < indent(line) + (0 if dash(more) and not key.group(2) else 1):
                break
            if FOLDED.fullmatch(key.group(2)):
                values[0] += " " + more.strip()
            elif not key.group(2) and dash(more) and not re.match(r"- [\w-]+:(\s|$)", more.strip()):
                values.append(more.strip()[2:])
        for value in values:
            value = value.strip().strip("\"'").strip()
            if FOLDED.fullmatch(value):
                continue
            literal = TEMPLATE.sub(" ", value)
            if "{{" in literal or "{%" in literal:
                out.append((i + 1, "unclosed template, cannot read the image"))
            elif re.search(r"[A-Za-z0-9]", literal) and not DIGEST_END.search(value) and (rel, value) not in ALLOWED:
                out.append((i + 1, f"{value} has no @sha256 digest"))
    return out


def scan(root, files=None):
    """Return `path:line: problem` for every failing reference in the tracked files."""
    if files is None:
        files = [f for f in subprocess.run(["git", "ls-files", "-z", "--", "."], cwd=root, capture_output=True,
                                           check=True, text=True).stdout.split("\0") if f]
    out = []
    for rel in files:
        path = Path(rel)
        if path.suffix in SUFFIXES and not SKIP_DIRS & set(path.parts) and (root / rel).is_file():
            out += [f"{rel}:{n}: {p}" for n, p in scan_text(rel, (root / rel).read_text(errors="replace"))]
    return out


class ImageReferencesPinned(unittest.TestCase):
    def test_tracked_files(self):
        bad = scan(ROOT)
        self.assertEqual(bad, [], "Pin as name:tag@sha256:<index digest> (docs/hardening.md, 'Image pins'):\n"
                         + "\n".join(bad))


class Fixtures(unittest.TestCase):
    """Each case goes through scan() on a temporary tree with the tracked-file list injected."""

    D = "@sha256:" + "a" * 64
    HELM = "a:\n  image:\n    registry: docker.io\n    repository: bitnami/valkey\n"

    def run_scan(self, text, rel="roles/r/defaults/main.yml"):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
            (Path(tmp) / rel).write_text(text)
            return scan(Path(tmp), [rel])

    def test_caught(self):
        cases = {
            "latest": "x_image: amazon/aws-cli:latest\n",
            "tag only": 'image: "postgres:16"\n',
            "no tag": "image: nginx\n",
            "templated tag": 'image: "nginx:{{ tag }}"\n',
            "image list": "images:\n  - nginx:1.27\n",
            "indentless image list": "images:\n- nginx:1.27\n",
            "list under an _image key": "backup_image:\n  - nginx:1.27\n",
            "repository in a list item": "s:\n  - repository: nginx\n    tag: latest\n",
            "digest in the next list item": "s:\n  - repository: nginx\n  - digest: sha256:" + "a" * 64 + "\n",
            "digest in the previous list item": "s:\n  - digest: sha256:" + "a" * 64 + "\n  - repository: nginx\n",
            "folded": "x_image: >-\n  nginx:1.27\n",
            "block": "image: |\n  nginx:1.27\n",
            "template, then name and tag": 'image: "{{ registry }}/nginx:1.27"\n',
            "template around the tag": 'image: "{{ registry }}/nginx:1.27{{ suffix }}"\n',
            "unclosed template": "image: {{ a |\n",
            "comment with a token": "image: nginx:1.27 # quay.io/ansible/awx-ee:1\n",
            "list item": "images:\n  - amazon/aws-cli:latest\n",
            "helm, no tag, no digest": self.HELM,
            "helm, templated repository": "a:\n  image:\n    repository: {{ r }}\n    tag: {{ t }}\n",
            "helm, tag, no digest": self.HELM + "    tag: latest\n",
            "helm, bad digest": self.HELM + "    digest: sha256:abc\n",
            "helm, digest in another block": self.HELM + "b:\n  digest: sha256:" + "a" * 64 + "\n",
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertTrue(self.run_scan(text), name)

    def test_accepted(self):
        cases = {
            "digest": f"x_image: postgres:16{self.D}\n",
            "pinned, no tag": f"image: nginx{self.D}\n",
            "pinned, templated tag": f'image: "nginx:{{{{ tag }}}}{self.D}"\n',
            "pinned image list": f"images:\n  - nginx:1.27{self.D}\n",
            "pinned indentless list": f"images:\n- nginx:1.27{self.D}\n",
            "pinned _image list": f"backup_image:\n  - nginx:1.27{self.D}\n",
            "list mapping item": "images:\n  - name: x\n",
            "digest first in list item": "s:\n  - digest: sha256:" + "a" * 64 + "\n    repository: nginx\n",
            "digest after in list item": "s:\n  - repository: nginx\n    digest: sha256:" + "a" * 64 + "\n",
            "templates only": 'image: "{{ img }}"\n',
            "template pair": 'image: "{{ repo }}:{{ tag }}"\n',
            "folded, template and digest": f"x_image: >-\n  {{{{ host }}}}/dmf/python{self.D}\n",
            "comment line": "# image: nginx:latest\n",
            "empty key": "image:\n",
            "not an image line": "time: 06:54\nhost: db:5432\n",
            "helm with digest": self.HELM + "    tag: latest\n    digest: sha256:" + "a" * 64 + "\n",
            "helm digest, no tag": self.HELM + "    digest: sha256:" + "a" * 64 + "\n",
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertEqual(self.run_scan(text), [], name)

    def test_exact_exceptions(self):
        rel, ref = "roles/r/a.yml", "registry.example/dmf/awx-ee:0.1.1"
        ALLOWED[(rel, ref)] = 'fixture'
        try:
            self.assertEqual(self.run_scan(f"image: {ref}\n", rel), [])
            self.assertTrue(self.run_scan(f"image: {ref}\n", "roles/r/b.yml"), "other file")
            self.assertTrue(self.run_scan(f"image: {ref}x\n", rel), "other reference")
        finally:
            ALLOWED.pop((rel, ref))

    def test_valkey_block_in_the_real_template(self):
        """Removing the tag and the digest from the real NetBox values fails."""
        rel = "roles/stack/operator/netbox/templates/values.yml.j2"
        text = (ROOT / rel).read_text()
        self.assertEqual(scan_text(rel, text), [])
        stripped = re.sub(r"^\s*(tag|digest):.*\n", "", text, flags=re.M)
        self.assertNotEqual(stripped, text)
        self.assertTrue(scan_text(rel, stripped))

    def test_librenms_default_tag_carries_a_digest(self):
        """Chart 7.1.0 has no digest key, so the digest rides in `librenms_image_tag`."""
        rel = "roles/modules/infra-monitoring/librenms/defaults/main.yml"
        match = re.search(r'^librenms_image_tag:\s*"([^"]*)"', (ROOT / rel).read_text(), re.M)
        self.assertTrue(match, "librenms_image_tag not found")
        for bad in ("25.12.0", "25.12.0@sha256:abc", "25.12.0@sha256:" + "g" * 64, "@sha256:" + "a" * 64):
            self.assertIsNone(LIBRENMS_TAG.fullmatch(bad), bad)
        self.assertTrue(LIBRENMS_TAG.fullmatch(match.group(1)), match.group(1))

    def test_scope(self):
        bad = "image: nginx:latest\n"
        for rel in ("charts/x/values.yaml", "ee/a.yml", "tests/a.yml", "README.md"):
            self.assertEqual(self.run_scan(bad, rel), [], rel)

    def test_ignored_files_are_not_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)
            git("init", "-q")
            (root / ".gitignore").write_text(".ansible/\n")
            (root / ".ansible/c").mkdir(parents=True)
            (root / ".ansible/c/a.yml").write_text("image: nginx:latest\n")
            git("add", "-A")
            self.assertEqual(scan(root), [])
            (root / "bad.yml").write_text("image: nginx:latest\n")
            git("add", "bad.yml")
            self.assertTrue(scan(root))


if __name__ == "__main__":
    unittest.main()
