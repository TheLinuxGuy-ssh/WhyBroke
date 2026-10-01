import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKILL_DIR = ROOT / "skill" / "whybroke"
SKILL_MD = SKILL_DIR / "SKILL.md"

PLAYBOOKS = (
    "disk-full",
    "oom-kill",
    "port-in-use",
    "crashed-container",
    "failed-unit",
    "permission-errors",
)

FRONTMATTER_REQUIRED = ("name", "description")


def parse_frontmatter(text):
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 3)
    if end == -1:
        return None, text
    block = text[4:end]
    body = text[end + 5 :]
    fields = {}
    for line in block.split("\n"):
        if ":" in line and not line.strip().startswith("#"):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields, body


class FrontmatterTests(unittest.TestCase):
    def setUp(self):
        self.text = SKILL_MD.read_text()
        self.fields, self.body = parse_frontmatter(self.text)

    def test_frontmatter_exists(self):
        self.assertIsNotNone(self.fields, "SKILL.md must open with YAML frontmatter")

    def test_name_and_description_present(self):
        for key in FRONTMATTER_REQUIRED:
            self.assertIn(key, self.fields)
            self.assertTrue(self.fields[key].strip(), "{} must not be empty".format(key))

    def test_name_matches_folder(self):
        self.assertEqual(self.fields["name"], "whybroke")
        self.assertEqual(self.fields["name"], SKILL_DIR.name)

    def test_name_is_lowercase_hyphenated(self):
        name = self.fields["name"]
        self.assertEqual(name, name.lower())
        self.assertNotIn("_", name)
        self.assertNotIn(" ", name)
        for char in name:
            self.assertIn(char, "abcdefghijklmnopqrstuvwxyz-")

    def test_description_says_what_and_when(self):
        description = self.fields["description"].lower()
        self.assertIn("use when", description)
        self.assertGreater(len(description), 80)

    def test_body_has_content(self):
        self.assertGreater(len(self.body.strip()), 400)


class LayoutTests(unittest.TestCase):
    def test_skill_md_exists(self):
        self.assertTrue(SKILL_MD.is_file())

    def test_every_playbook_exists(self):
        for playbook in PLAYBOOKS:
            path = SKILL_DIR / "references" / (playbook + ".md")
            self.assertTrue(path.is_file(), "missing playbook {}".format(path))

    def test_every_playbook_is_linked_from_skill_md(self):
        text = SKILL_MD.read_text()
        for playbook in PLAYBOOKS:
            self.assertIn("references/" + playbook + ".md", text, playbook)

    def test_every_playbook_is_substantial(self):
        for playbook in PLAYBOOKS:
            text = (SKILL_DIR / "references" / (playbook + ".md")).read_text()
            self.assertGreater(len(text.splitlines()), 20, playbook)

    def test_playbooks_state_evidence_and_fixes(self):
        for playbook in PLAYBOOKS:
            text = (SKILL_DIR / "references" / (playbook + ".md")).read_text()
            lowered = text.lower()
            self.assertIn("evidence", lowered, playbook)
            self.assertIn("root cause", lowered, playbook)
            self.assertIn("fixes to suggest", lowered, playbook)
            self.assertIn("false positive", lowered, playbook)

    def test_playbooks_offer_fixes_as_suggestions_not_actions(self):
        """The agent has no write path, so every playbook fix must be phrased as a suggestion."""
        for playbook in PLAYBOOKS:
            text = (SKILL_DIR / "references" / (playbook + ".md")).read_text()
            start = text.find("## Fixes to suggest")
            self.assertNotEqual(start, -1, "{} has no fixes section".format(playbook))
            fixes = text[start:]
            self.assertNotIn("I will ", fixes, playbook)
            self.assertNotIn("run this now", fixes.lower(), playbook)
            self.assertIn("suggest", fixes.lower(), playbook)

    def test_skill_has_no_runtime_requirement(self):
        text = SKILL_MD.read_text()
        self.assertNotIn("pip install", text)
        self.assertNotIn("import ", text)

    def test_investigation_order_in_body(self):
        body = SKILL_MD.read_text()
        for step in ("Service status", "Recent logs", "Resources", "Network and ports"):
            self.assertIn(step, body, step)

    def test_report_format_matches_implementation(self):
        from whybroke import report

        body = SKILL_MD.read_text()
        for heading in report.HEADINGS:
            self.assertIn(heading, body, heading)


class ScenarioScriptTests(unittest.TestCase):
    def setUp(self):
        self.dir = ROOT / "tests" / "scenarios"

    def scripts(self):
        return sorted(self.dir.glob("*.sh"))

    def test_expected_scripts_exist(self):
        for name in (
            "break_disk.sh",
            "break_unit.sh",
            "break_port.sh",
            "break_oom.sh",
            "break_container.sh",
            "fix_all.sh",
        ):
            self.assertTrue((self.dir / name).is_file(), name)

    def test_scripts_are_executable(self):
        for path in self.scripts():
            self.assertTrue(os.access(path, os.X_OK), "{} is not executable".format(path))

    def test_scripts_pass_bash_syntax_check(self):
        for path in self.scripts():
            result = subprocess.run(
                ["bash", "-n", str(path)], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, "{}: {}".format(path, result.stderr))

    def test_break_scripts_use_strict_mode(self):
        for path in self.scripts():
            if path.name == "fix_all.sh":
                continue
            self.assertIn("set -euo pipefail", path.read_text(), path.name)

    def test_fix_script_tolerates_partial_failures(self):
        """Cleanup must never abort halfway, so it runs without -e."""
        text = (self.dir / "fix_all.sh").read_text()
        self.assertIn("set -uo pipefail", text)

    def test_scripts_state_expected_diagnosis(self):
        for path in self.scripts():
            if path.name == "fix_all.sh":
                continue
            text = path.read_text()
            self.assertIn("EXPECTED QUESTION", text, path.name)
            self.assertIn("EXPECTED DIAGNOSIS", text, path.name)
            self.assertIn("EXPECTED EVIDENCE", text, path.name)

    def test_container_scripts_degrade_without_docker(self):
        for name in ("break_container.sh", "break_oom.sh"):
            text = (self.dir / name).read_text()
            self.assertIn("command -v docker", text, name)

    def test_fix_script_cleans_up_every_scenario(self):
        text = (self.dir / "fix_all.sh").read_text()
        for marker in ("whybroke-demo", "whybroke-broken", "whybroke-oom", "umount"):
            self.assertIn(marker, text, marker)

    def test_expected_doc_documents_every_scenario(self):
        text = (self.dir / "EXPECTED.md").read_text()
        for marker in ("break_disk", "break_unit", "break_port", "break_container", "break_oom"):
            self.assertIn(marker, text, marker)


class PackagingTests(unittest.TestCase):
    def test_license_is_mit(self):
        text = (ROOT / "LICENSE").read_text()
        self.assertIn("MIT License", text)

    def test_requirements_has_only_ollama(self):
        lines = [
            line.strip()
            for line in (ROOT / "requirements.txt").read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("ollama"), lines)

    def test_no_imports_beyond_stdlib_and_ollama(self):
        """Every module in the package must import only the stdlib, ollama, or itself."""
        allowed = {
            "argparse",
            "dataclasses",
            "json",
            "os",
            "re",
            "shutil",
            "subprocess",
            "sys",
            "tempfile",
            "time",
            "typing",
        }
        for path in sorted((ROOT / "whybroke").glob("*.py")):
            for line in path.read_text().splitlines():
                line = line.strip()
                if line.startswith("from ") and " import " in line:
                    head, _, tail = line.partition(" import ")
                    module = head[5:].strip()
                    if module.startswith("."):
                        continue
                elif line.startswith("import "):
                    module = line[7:].split()[0].rstrip(",")
                else:
                    continue
                root = module.split(".")[0]
                self.assertTrue(
                    root in allowed or root == "ollama",
                    "{} imports disallowed module {}".format(path.name, module),
                )

    def test_readme_has_required_sections(self):
        text = (ROOT / "README.md").read_text()
        for heading in (
            "Why local-first matters",
            "Architecture",
            "Install",
            "Install the skill",
            "Safety model",
            "Verify it yourself",
            "Credits",
            "License",
        ):
            self.assertIn(heading, text, heading)

    def test_readme_promotes_the_model_free_demo(self):
        """The GIF was cut for time, so the README must lead with --doctor instead."""
        text = (ROOT / "README.md").read_text()
        self.assertIn("python -m whybroke --doctor", text)
        self.assertNotIn("demo.gif", text)
        self.assertNotIn("demo/demo", text)
        self.assertFalse((ROOT / "demo").exists(), "no empty demo/ dir left behind")

    def test_readme_claims_no_unmeasured_results(self):
        """Unrun scenarios must be labelled as such, never presented as passing."""
        text = (ROOT / "README.md").read_text()
        self.assertIn("specification, not a claim of measured results", text)
        self.assertNotIn("| pending |", text)

    def test_readme_no_placeholder_urls(self):
        text = (ROOT / "README.md").read_text()
        self.assertIn("github.com/YOUR_USER/whybroke", text, "clone URL must be marked as a placeholder")

    def test_readme_badges_are_green(self):
        text = (ROOT / "README.md").read_text()
        badges = [line for line in text.splitlines() if "img.shields.io" in line]
        self.assertGreaterEqual(len(badges), 4)
        for badge in badges:
            self.assertIn("22c55e", badge, badge)

    def test_readme_has_no_emojis(self):
        """Box drawing is allowed for the architecture diagram; emoji are not."""
        text = (ROOT / "README.md").read_text()
        for char in text:
            if char.isascii():
                continue
            codepoint = ord(char)
            is_box_drawing = 0x2500 <= codepoint <= 0x25FF
            is_arrow = 0x2190 <= codepoint <= 0x21FF
            self.assertTrue(
                is_box_drawing or is_arrow,
                "non-ascii, non-box character {!r} (U+{:04X})".format(char, codepoint),
            )


class DoctorTests(unittest.TestCase):
    """The doctor is the fallback demo, so it must survive a hostile environment."""

    def run_doctor(self, env=None):
        child_env = dict(os.environ)
        if env:
            child_env.update(env)
        return subprocess.run(
            [sys.executable, "-m", "whybroke", "--doctor"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(ROOT),
            env=child_env,
        )

    def test_doctor_runs_without_a_model(self):
        result = self.run_doctor({"WHYBROKE_FORCE_NO_OLLAMA": "1", "OLLAMA_HOST": "http://127.0.0.1:1"})
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("whybroke doctor", result.stdout)

    def test_doctor_reports_the_safety_layer_as_passing(self):
        result = self.run_doctor()
        for check in (
            "injection blocked",
            "unknown tool denied",
            "redaction",
            "truncation",
            "allowlist is read-only",
            "timeout enforced",
        ):
            self.assertIn(check, result.stdout, check)

    def test_doctor_exercises_the_tools_live(self):
        """At minimum the 11 non-docker tools must run; container_logs needs a container."""
        from whybroke import doctor, tools

        result = self.run_doctor()
        line = [l for l in result.stdout.splitlines() if "tools execute live" in l][0]
        self.assertTrue(line.startswith("PASS"), line)
        match = re.search(r"(\d+)/(\d+) tools executed live", line)
        self.assertIsNotNone(match, line)
        executed, total = int(match.group(1)), int(match.group(2))
        self.assertEqual(executed, total, "every planned tool must succeed")
        self.assertGreaterEqual(total, 11, line)
        if not tools.docker_available():
            self.assertEqual(total, 11, "without docker only 11 tools are planned")

    def test_doctor_summary_line_present(self):
        result = self.run_doctor()
        self.assertRegex(result.stdout, r"\d+ passed, \d+ warnings?, \d+ failed")

    def test_doctor_does_not_write_files(self):
        before = set(os.listdir(ROOT))
        self.run_doctor()
        self.assertEqual(set(os.listdir(ROOT)), before)

    def test_doctor_leaves_no_temp_dirs(self):
        import glob

        before = set(glob.glob("/tmp/whybroke-doctor-*"))
        self.run_doctor()
        self.assertEqual(set(glob.glob("/tmp/whybroke-doctor-*")), before)

    def test_doctor_checks_are_unique_and_cover_the_safety_layer(self):
        from whybroke import doctor

        names = [check.__name__ for check in doctor.CHECKS]
        self.assertEqual(len(names), len(set(names)), "no duplicate checks")
        for required in (
            "check_injection",
            "check_unknown_tool",
            "check_redaction",
            "check_truncation",
            "check_read_only",
            "check_every_tool",
            "check_timeout",
        ):
            self.assertIn(required, names, required)

    def test_doctor_checks_are_all_callable(self):
        from whybroke import doctor

        for check in doctor.CHECKS:
            self.assertTrue(callable(check), check.__name__)


class TranscriptTests(unittest.TestCase):
    def test_transcript_is_written(self):
        from tests.test_agent import FINAL, FakeClient, say
        from whybroke import agent

        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient([say(FINAL)])
            diag = agent.Agent(
                client=client, model="fake", transcript_dir=tmp
            )
            diag.investigate("why did nginx stop?")
            files = list(Path(tmp).glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertIn("nginx-stop", files[0].name)
            data = json.loads(files[0].read_text())
            self.assertEqual(data["question"], "why did nginx stop?")
            self.assertEqual(data["model"], "fake")
            self.assertIn("messages", data)
            self.assertIn(FINAL.split("\n")[0], data["report"])

    def test_transcript_can_be_disabled(self):
        from tests.test_agent import FINAL, FakeClient, say
        from whybroke import agent

        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient([say(FINAL)])
            diag = agent.Agent(client=client, model="fake", transcript_dir=tmp)
            diag.investigate("x")
            self.assertTrue(list(Path(tmp).glob("*.json")))
            os.remove(next(Path(tmp).glob("*.json")))

            client = FakeClient([say(FINAL)])
            diag = agent.Agent(client=client, model="fake", transcript_dir=None)
            diag.investigate("x")
            self.assertFalse(list(Path(tmp).glob("*.json")))


class CliTests(unittest.TestCase):
    def run_cli(self, args, timeout=20, env=None):
        child_env = dict(os.environ)
        if env:
            child_env.update(env)
        return subprocess.run(
            [sys.executable, "-m", "whybroke"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(ROOT),
            env=child_env,
        )

    def test_help_works(self):
        result = self.run_cli(["--help"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("whybroke", result.stdout)
        self.assertIn("--model", result.stdout)
        self.assertIn("--max-steps", result.stdout)
        self.assertIn("--doctor", result.stdout)

    def test_no_such_tool_flag_is_not_accepted(self):
        result = self.run_cli(["--execute-anything"])
        self.assertNotEqual(result.returncode, 0)

    def test_unreachable_ollama_is_reported_cleanly(self):
        os.environ["OLLAMA_HOST"] = "http://127.0.0.1:1"
        try:
            result = self.run_cli(["why is disk full"])
        finally:
            del os.environ["OLLAMA_HOST"]
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
        self.assertTrue(result.stderr.strip(), "must explain itself on stderr")
        self.assertEqual(result.stdout, "")

    def test_missing_client_is_reported_cleanly(self):
        result = self.run_cli(["why is disk full"], env={"WHYBROKE_FORCE_NO_OLLAMA": "1"})
        self.assertNotIn("Traceback", result.stderr)

    def test_module_has_no_side_effects_on_import(self):
        result = subprocess.run(
            [sys.executable, "-c", "import whybroke, whybroke.agent, whybroke.tools; print('ok')"],
            capture_output=True,
            text=True,
            timeout=20,
            cwd=str(ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)