"""Static check: template values reach a shell program on stdin, never in its text or arguments.

Two forms are unsafe when a value is a template expression:

* `{{ value | quote }}` inside a single-quoted program (`sh -c '... {{ v | quote }} ...'`).
  The quotes that `quote` adds end the program's string early, so a value that holds `'`
  turns into program text.
* A value passed as an argument after the program (`argv: [sh, -c, 'bao read "$1"', sh,
  "{{ v }}"]`). The command module expands `$NAME` in every argument on ansible-core 2.16
  and later, so a value such as `$HOME/x` changes on the way. The parameter that turns this
  off, `expand_argument_vars`, does not exist on the ansible-core 2.15 of the AWX execution
  environment.

The safe form sends each value on stdin, one per line, and the fixed program reads it:

    command:
      argv: [sh, -c, 'IFS= read -r SECRET_PATH; bao read "$SECRET_PATH"']
      stdin: "{{ path }}\n"

A task checks first that the value holds no newline.

The check reads every YAML and Jinja file in the repository, except Helm charts (Helm's
`quote` is a different function), as text. It finds `sh -c '`, `bash -c '` and
`/bin/sh -c '` programs and reports a `quote` filter inside one. It also finds an `argv`
list that holds `sh` (or `bash`), `-c`, the program and a second `sh`, and reports a
template expression among the arguments after that second `sh`.
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SUFFIXES = {".yml", ".yaml", ".j2"}
SKIP_DIRS = {".git", "charts", "node_modules"}

SHELL_C = re.compile(r"(?<![\w.-])(?:/bin/)?(?:ba|da)?sh\s+-[a-z]*c\s+'")
# A Jinja expression can hold `'`, so the program's end search steps over `{{ ... }}`.
PROGRAM_TEXT = re.compile(r"(?:\{\{.*?\}\}|[^'])*", re.S)
QUOTE_FILTER = re.compile(r"\|\s*quote\b")
SHELL_WORD = re.compile(r"^(\s*)- (?:sh|bash)\s*$")
FLOW_ARGV = re.compile(r"\[\s*(?:/bin/)?(?:ba|da)?sh\s*,\s*-c\s*,\s*('(?:[^']|'')*'|\"(?:[^\"\\]|\\.)*\"|[^,\]]+)\s*,\s*(?:ba|da)?sh\b(.*?)\]", re.S)


def quote_in_single_quoted_programs(text):
    """Return the line number of each program that holds a `quote` filter."""
    hits = []
    for match in SHELL_C.finditer(text):
        program = PROGRAM_TEXT.match(text, match.end()).group(0)
        if QUOTE_FILTER.search(program):
            hits.append(text.count("\n", 0, match.start()) + 1)
    return hits


def template_values_in_argv(text):
    """Return the line number of each template value that follows a `sh -c` program in an argv list."""
    hits = []
    lines = text.splitlines()
    for number, line in enumerate(lines):
        shell = SHELL_WORD.match(line)
        if not shell or number + 1 >= len(lines) or not re.match(r"^\s*- -c\s*$", lines[number + 1]):
            continue
        item = len(shell.group(1))
        after = number + 2
        # The program is the next item. It may span lines.
        after += 1
        while after < len(lines) and not re.match(rf"^ {{{item}}}- ", lines[after]) and lines[after].strip():
            after += 1
        if after >= len(lines) or not SHELL_WORD.match(lines[after]):
            continue
        after += 1
        while after < len(lines) and re.match(rf"^ {{{item}}}- ", lines[after]):
            if "{{" in lines[after]:
                hits.append(after + 1)
            after += 1
    for match in FLOW_ARGV.finditer(text):
        if "{{" in match.group(2):
            hits.append(text.count("\n", 0, match.start()) + 1)
    return hits


PROGRAM_ITEM = re.compile(r"^(\s*)- '(.*IFS= read -r .*)'\s*$")
# A program that reads a body after the framed values: `bao ... -`, `jwt=-` or `$(cat)`.
READS_A_BODY = re.compile(r"\s-(?=\s|$)|jwt=-|\$\(cat\)")


def stdin_segments(lines, start):
    """Return the values that the `stdin:` on lines[start] sends, one entry per line it frames."""
    first = re.match(r"^(\s*)stdin:\s*(.*)$", lines[start])
    value = first.group(2).strip()
    if value[:1] in ("|", ">"):
        segments, current, depth = [], "", 0
        for line in lines[start + 1:]:
            if line.strip() and len(line) - len(line.lstrip()) <= len(first.group(1)):
                break
            if not line.strip() and depth == 0:
                continue
            current += line.strip() + " "
            depth += line.count("{{") - line.count("}}")
            if depth == 0:
                segments.append(current.strip())
                current = ""
        return segments
    segments = value[1:-1].split("\\n")
    return segments[:-1] if segments and segments[-1] == "" else segments


def unframed_reads(text):
    """Return the line number of each program whose reads and framed stdin lines differ.

    A program that reads its values with `IFS= read -r NAME` takes one line of stdin per
    call. A task that builds fewer lines than the program reads, or more than it reads and
    a body, leaves a value in the wrong variable.
    """
    hits = []
    lines = text.splitlines()
    for number, line in enumerate(lines):
        item = PROGRAM_ITEM.match(line)
        if not item or number < 2 or not re.match(r"^\s*- -c\s*$", lines[number - 1]):
            continue
        indent = len(item.group(1))
        stdin = None
        for later in range(number + 1, len(lines)):
            if lines[later].strip() and len(lines[later]) - len(lines[later].lstrip()) < indent - 2:
                break
            if re.match(rf"^ {{{indent - 2}}}stdin:", lines[later]):
                stdin = later
                break
        reads = len(re.findall(r"IFS= read -r \w+", item.group(2)))
        expected = reads + (1 if READS_A_BODY.search(item.group(2)) else 0)
        if stdin is None or len(stdin_segments(lines, stdin)) != expected:
            hits.append(number + 1)
    return hits


SHARED_ROLE = REPO / "k3s-lab-bootstrap/roles/common/openbao-cli/tasks/main.yml"
NEWLINE_VAR = "openbao_cli_nl"


def shared_role_framing_problems(role_text):
    """Return the ways in which the shared role's program and its stdin template disagree.

    The program reads the token and the word count, then one word per count, and leaves the
    rest of stdin to `bao`. The template must send those values in that order: the token, the
    count of `args`, each word of `args` on its own line, then `input`. The number of leading
    reads comes from the program text, not from a constant in this check.
    """
    import yaml

    command = next(t for t in yaml.safe_load(role_text) if "ansible.builtin.command" in t)
    program = command["ansible.builtin.command"]["argv"][-1]
    template = command["vars"]["openbao_cli_stdin"]
    problems = []
    before_loop, _, loop = program.partition("while")
    leading_reads = re.findall(r"IFS= read -r (\w+)", before_loop)
    loop_reads = re.findall(r"IFS= read -r (\w+)", loop)
    if len(loop_reads) != 1:
        problems.append(f"the loop reads {len(loop_reads)} values per word, not 1")
    if "$((" not in loop or "- 1" not in loop:
        problems.append("the loop does not count one word down per read")
    if "exec bao" not in program:
        problems.append("the program does not hand the rest of stdin to bao")
    # Lines of the template, split at the newline variable, outside the expressions.
    segments = [seg.strip() for seg in re.split(r"\{\{\s*" + NEWLINE_VAR + r"\s*\}\}", template)]
    # Token, count, then the words (one expression that joins them with the newline), then input.
    if len(segments) != len(leading_reads) + 2:
        problems.append(f"the template has {len(segments)} parts for {len(leading_reads)} leading reads, the words and the input")
    else:
        sent = segments[: len(leading_reads)]
        if "openbao_cli_token" not in sent[0]:
            problems.append("the first line sent is not the token")
        if "args | length" not in sent[1]:
            problems.append("the second line sent is not the word count")
        words, body = segments[-2], segments[-1]
        if f"join({NEWLINE_VAR})" not in words.replace(" ", ""):
            problems.append("the words are not joined one per line")
        if "openbao_cli_call.input" not in body:
            problems.append("the input is not the last part sent")
    if leading_reads[:1] != ["BAO_TOKEN"]:
        problems.append("the program does not read the token first")
    if command["ansible.builtin.command"].get("stdin_add_newline") is not False:
        problems.append("the module adds a newline to the input")
    return problems


def repo_files():
    for path in sorted(REPO.rglob("*")):
        relative = path.relative_to(REPO)
        if path.suffix in SUFFIXES and path.is_file() and not SKIP_DIRS & set(relative.parts):
            yield relative.as_posix(), path


def scan(detector):
    found = {}
    for relative, path in repo_files():
        hits = detector(path.read_text())
        if hits:
            found[relative] = hits
    return found


class ShellValuesOnStdin(unittest.TestCase):
    def test_the_scan_covers_the_repository(self):
        scanned = [relative for relative, _ in repo_files()]
        self.assertGreater(len(scanned), 100, "the file walk found too few files")
        for relative in (
            "k3s-lab-bootstrap/roles/stack/operator/netbox/tasks/main.yml",
            "k3s-lab-bootstrap/roles/common/openbao-session/tasks/main.yml",
            "k3s-lab-bootstrap/playbooks/verify-openbao-identity-model.yml",
            "k3s-lab-bootstrap/playbooks/verify-bootstrap-convergence.yml",
            "k3s-lab-bootstrap/roles/stack/operator/openbao/tasks/kubernetes-auth-config.yml",
        ):
            self.assertIn(relative, scanned)

    def test_no_quote_filter_inside_a_single_quoted_program(self):
        self.assertEqual(
            scan(quote_in_single_quoted_programs), {},
            "send these values on stdin and read them in the program with `IFS= read -r NAME`",
        )

    def test_no_template_value_follows_a_shell_program_in_argv(self):
        self.assertEqual(
            scan(template_values_in_argv), {},
            "send these values on stdin and read them in the program with `IFS= read -r NAME`",
        )

    def test_every_program_reads_as_many_values_as_its_task_sends(self):
        programs = sum(
            1
            for _, path in repo_files()
            for line in path.read_text().splitlines()
            if PROGRAM_ITEM.match(line)
        )
        # The OpenBao calls moved into the shared role, whose one program is checked by
        # `test_the_shared_role_reads_what_its_stdin_sends`. One hand-written program in
        # this form remains (the break-glass user in the Authentik role), and the check must
        # still find it.
        self.assertGreaterEqual(programs, 1, "the check found too few programs that read their values")
        self.assertEqual(
            scan(unframed_reads), {},
            "the stdin lines of the task must match the `IFS= read -r` calls of the program",
        )

    def test_the_shared_role_reads_what_its_stdin_sends(self):
        self.assertEqual(shared_role_framing_problems(SHARED_ROLE.read_text()), [])

    def test_the_shared_role_check_flags_each_mismatch(self):
        text = SHARED_ROLE.read_text()
        mutations = {
            "a read is dropped": text.replace("IFS= read -r OPENBAO_CLI_COUNT; ", ""),
            "an extra read is added": text.replace("IFS= read -r BAO_TOKEN;", "IFS= read -r BAO_TOKEN; IFS= read -r EXTRA;"),
            "the count is not sent": text.replace("{{ openbao_cli_call.args | length }}{{ openbao_cli_nl }}", ""),
            "the input is not sent": text.replace("{{ openbao_cli_call.input | default('') }}", ""),
            "the words share one line": text.replace("join(openbao_cli_nl)", "join(' ')"),
            "the loop reads two words": text.replace('IFS= read -r OPENBAO_CLI_WORD;', 'IFS= read -r OPENBAO_CLI_WORD; IFS= read -r OTHER;'),
            "the module adds a newline": text.replace("stdin_add_newline: false", "stdin_add_newline: true"),
        }
        for label, mutated in mutations.items():
            with self.subTest(label=label):
                self.assertNotEqual(mutated, text, label)
                self.assertTrue(shared_role_framing_problems(mutated), label)

    def test_detector_counts_reads_against_stdin_lines(self):
        program = "'IFS= read -r BAO_TOKEN; IFS= read -r SECRET_PATH; bao kv put \"$SECRET_PATH\" -'"
        head = "argv:\n  - sh\n  - -c\n  - " + program + "\n"
        good = {
            "token, path and body": head + "stdin: \"{{ t }}\\n{{ p }}\\n{{ b | to_json }}\\n\"\n",
            "block form": head + "stdin: |\n  {{ t }}\n  {{ p }}\n  {{\n    b | to_json\n  }}\n",
            "no body": head.replace(" -'", "'") + "stdin: \"{{ t }}\\n{{ p }}\\n\"\n",
        }
        bad = {
            "a read is missing": head.replace("IFS= read -r SECRET_PATH; ", "") + "stdin: \"{{ t }}\\n{{ p }}\\n{{ b }}\\n\"\n",
            "a stdin line is missing": head + "stdin: \"{{ t }}\\n{{ b | to_json }}\\n\"\n",
            "no stdin": head,
        }
        for label, text in good.items():
            with self.subTest(label=label):
                self.assertEqual(unframed_reads(text), [], label)
        for label, text in bad.items():
            with self.subTest(label=label):
                self.assertTrue(unframed_reads(text), label)

    def test_detector_sees_the_quote_forms_it_guards(self):
        bad = {
            "sh -c over lines": "cmd: >-\n  kubectl exec -- sh -c '\n  bao read {{ p | quote }}'\n",
            "bash -c": "- bash -c 'echo {{ p|quote }}'\n",
            "bin sh": "- /bin/sh -c 'echo {{ p | quote }}' x\n",
            "jinja with quotes before the filter":
                "- sh -c 'bao kv {{ 'a' if x else 'b' }} {{ p | quote }} -'\n",
        }
        for label, text in bad.items():
            with self.subTest(label=label):
                self.assertTrue(quote_in_single_quoted_programs(text), label)

    def test_detector_sees_the_argv_forms_it_guards(self):
        bad = {
            "block argv": "argv:\n  - kubectl\n  - sh\n  - -c\n  - 'bao read \"$1\"'\n  - sh\n  - \"{{ p }}\"\n",
            "second value": "argv:\n  - sh\n  - -c\n  - 'x'\n  - sh\n  - fixed\n  - \"a{{ p }}\"\n",
            "multi-line program": "argv:\n  - sh\n  - -c\n  - >-\n    echo\n    \"$1\"\n  - sh\n  - \"{{ p }}\"\n",
            "flow argv": "argv: [sh, -c, 'bao read \"$1\"', sh, \"{{ p }}\"]\n",
        }
        for label, text in bad.items():
            with self.subTest(label=label):
                self.assertTrue(template_values_in_argv(text), label)

    def test_detector_allows_the_stdin_form_and_fixed_arguments(self):
        good = {
            "stdin form": (
                "argv:\n  - sh\n  - -c\n  - 'IFS= read -r P; bao read \"$P\"'\n"
                "stdin: \"{{ p }}\\n\"\n"
            ),
            "fixed arguments": "argv:\n  - sh\n  - -c\n  - 'x \"$@\"'\n  - sh\n  - ak\n  - shell\n",
            "quote outside the program":
                "cmd: >-\n  printf '%s' {{ j | quote }} |\n  kubectl exec -- sh -c 'cat > /f'\n",
        }
        for label, text in good.items():
            with self.subTest(label=label):
                self.assertEqual(quote_in_single_quoted_programs(text), [], label)
                self.assertEqual(template_values_in_argv(text), [], label)


if __name__ == "__main__":
    unittest.main()
