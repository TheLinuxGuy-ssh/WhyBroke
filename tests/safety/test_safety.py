import os
import unittest

from whybroke import agent, config, report, safety, tools


class RedactionTests(unittest.TestCase):
    def test_redacts_private_key_block(self):
        text = (
            "before\n-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIabc\n-----END RSA PRIVATE KEY-----\nafter"
        )
        cleaned, count = safety.redact(text)
        self.assertNotIn("MIIabc", cleaned)
        self.assertGreaterEqual(count, 1)
        self.assertIn("before", cleaned)
        self.assertIn("after", cleaned)

    def test_redacts_assigned_secrets(self):
        for probe in (
            "password=hunter2 rest",
            "api_key: abcdef123456 rest",
            "token = zzz9999 rest",
            "Authorization: Bearer abcdefghijklmnop",
        ):
            cleaned, count = safety.redact(probe)
            self.assertIn("[REDACTED]", cleaned, probe)
            self.assertGreaterEqual(count, 1, probe)

    def test_redacts_url_credentials_and_jwt(self):
        cleaned, _ = safety.redact("dsn postgres://user:s3cr3t@db:5432/x")
        self.assertNotIn("s3cr3t", cleaned)
        cleaned, _ = safety.redact("auth eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9")
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", cleaned)

    def test_normal_text_untouched(self):
        text = "systemd[1]: Started nginx.service - high performance web server."
        cleaned, count = safety.redact(text)
        self.assertEqual(cleaned, text)
        self.assertEqual(count, 0)


class TruncationTests(unittest.TestCase):
    def test_short_text_untouched(self):
        self.assertEqual(safety.truncate("hello", 100), "hello")

    def test_keeps_head_and_tail(self):
        text = "HEAD" + ("x" * 5000) + "TAIL"
        result = safety.truncate(text, 200)
        self.assertLessEqual(len(result), 240)
        self.assertIn("HEAD", result)
        self.assertIn("TAIL", result)
        self.assertIn("omitted", result)


class ValidatorTests(unittest.TestCase):
    def test_unit_accepts_valid(self):
        for name in ("nginx.service", "sshd", "systemd-timesyncd.service"):
            self.assertEqual(safety.check_unit(name), name)

    def test_unit_rejects_injection(self):
        attacks = [
            "nginx; rm -rf /",
            "nginx && reboot",
            "nginx | tee /tmp/x",
            "nginx > /etc/passwd",
            "ng inx",
            "nginx\nreboot",
            "$(whoami)",
            "",
            "a" * 100,
        ]
        for attack in attacks:
            with self.assertRaises(safety.SafetyError, msg=attack):
                safety.check_unit(attack)

    def test_container_rejects_injection(self):
        for attack in ("c;id", "--flag", "c d", ""):
            with self.assertRaises(safety.SafetyError, msg=attack):
                safety.check_container(attack)

    def test_int_bounds(self):
        check = safety.int_arg("lines", 1, 200)
        self.assertEqual(check("50"), 50)
        for bad in (0, 201, -1, "abc", None, "1e3"):
            with self.assertRaises(safety.SafetyError, msg=str(bad)):
                check(bad)

    def test_port_bounds(self):
        self.assertIsNone(safety.optional_port(None))
        self.assertEqual(safety.optional_port("8080"), 8080)
        for bad in (0, 70000, "http", "-1"):
            with self.assertRaises(safety.SafetyError, msg=str(bad)):
                safety.optional_port(bad)

    def test_since_enum(self):
        self.assertEqual(safety.check_since("1h"), "1h")
        for bad in ("-1h", "1 hour", "always", "10s"):
            with self.assertRaises(safety.SafetyError, msg=bad):
                safety.check_since(bad)


class LogPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = "/tmp/whybroke_selftest"
        os.makedirs(self.tmp, exist_ok=True)
        self.real = os.path.join(self.tmp, "app.log")
        with open(self.real, "w") as handle:
            handle.write("line\n")
        self.saved = config.LOG_DIRS

    def tearDown(self):
        config.LOG_DIRS = self.saved
        os.remove(self.real)
        os.rmdir(self.tmp)

    def test_allows_path_inside_allowed_dir(self):
        config.LOG_DIRS = (self.tmp,)
        self.assertEqual(safety.check_log_path(self.real), self.real)

    def test_rejects_traversal(self):
        config.LOG_DIRS = ("/var/log",)
        for attack in (
            "/var/log/../../etc/shadow",
            "/etc/shadow",
            "/var/log/../../root/.ssh/id_rsa",
        ):
            with self.assertRaises(safety.SafetyError, msg=attack):
                safety.check_log_path(attack)

    def test_rejects_prefix_confusion(self):
        config.LOG_DIRS = (self.tmp,)
        sibling = self.tmp + "_evil/x.log"
        with self.assertRaises(safety.SafetyError):
            safety.check_log_path(sibling)

    def test_rejects_directory_and_missing(self):
        config.LOG_DIRS = (self.tmp,)
        with self.assertRaises(safety.SafetyError):
            safety.check_log_path(self.tmp)
        with self.assertRaises(safety.SafetyError):
            safety.check_log_path(os.path.join(self.tmp, "nope.log"))


class ValidateTests(unittest.TestCase):
    def setUp(self):
        self.spec = tools.REGISTRY["unit_logs"]

    def test_rejects_unknown_argument(self):
        with self.assertRaises(safety.SafetyError) as ctx:
            safety.validate(self.spec, {"unit": "nginx.service", "sudo": "yes"})
        self.assertIn("unknown argument", str(ctx.exception))

    def test_rejects_missing_required(self):
        with self.assertRaises(safety.SafetyError) as ctx:
            safety.validate(self.spec, {"lines": 10})
        self.assertIn("requires", str(ctx.exception))

    def test_normalizes_numeric_string(self):
        cleaned = safety.validate(self.spec, {"unit": "nginx.service", "lines": "25"})
        self.assertEqual(cleaned, {"unit": "nginx.service", "lines": 25})

    def test_rejects_non_object(self):
        with self.assertRaises(safety.SafetyError):
            safety.validate(self.spec, ["nginx.service"])


class RegistryTests(unittest.TestCase):
    def test_every_tool_is_read_only(self):
        for name, spec in tools.REGISTRY.items():
            self.assertTrue(spec.read_only, name)

    def test_every_tool_has_validators_for_schema_args(self):
        for name, spec in tools.REGISTRY.items():
            props = set(spec.parameters.get("properties", {}))
            self.assertEqual(props, set(spec.validators), name)

    def test_build_never_returns_shell_metacharacters(self):
        spec = tools.REGISTRY["service_status"]
        argv = spec.build({"unit": "nginx.service"})
        self.assertEqual(argv[0], "systemctl")
        self.assertEqual(argv[1], "status")
        self.assertIsInstance(argv, list)

    def test_schemas_shape(self):
        schemas = tools.schemas()
        self.assertEqual(len(schemas), len(tools.REGISTRY))
        for schema in schemas:
            self.assertEqual(schema["type"], "function")
            self.assertIn("name", schema["function"])
            self.assertIn("description", schema["function"])
            self.assertEqual(schema["function"]["parameters"]["type"], "object")

    def test_expected_tool_count(self):
        self.assertEqual(len(tools.REGISTRY), 12)


class RunToolTests(unittest.TestCase):
    def test_unknown_command_is_not_reachable(self):
        self.assertNotIn("run_shell", tools.REGISTRY)
        for banned in ("sh", "bash", "eval", "exec", "rm", "reboot", "systemctl_restart"):
            self.assertNotIn(banned, tools.REGISTRY)

    def test_disk_usage_runs_and_is_truncated(self):
        text, redacted = tools.run_tool(tools.REGISTRY["disk_usage"], {})
        self.assertLessEqual(len(text), config.MAX_OUTPUT_CHARS + 60)
        self.assertIsInstance(redacted, int)
        self.assertNotIn("TOOL TIMEOUT", text)

    def test_redaction_applies_to_real_output(self):
        saved = config.LOG_DIRS
        tmp = "/tmp/whybroke_redact"
        os.makedirs(tmp, exist_ok=True)
        path = os.path.join(tmp, "secret.log")
        with open(path, "w") as handle:
            handle.write("connecting password=supersecret123\n")
        try:
            config.LOG_DIRS = (tmp,)
            text, count = tools.run_tool(
                tools.REGISTRY["read_log_file"], {"path": path, "tail_lines": 10}
            )
            self.assertNotIn("supersecret123", text)
            self.assertIn("[REDACTED]", text)
            self.assertGreaterEqual(count, 1)
        finally:
            config.LOG_DIRS = saved
            os.remove(path)
            os.rmdir(tmp)

    def test_timeout_is_enforced(self):
        spec = tools.REGISTRY["disk_usage"]
        try:
            text = tools.run_argv(["sleep", "5"], 1)
        except Exception as exc:
            self.fail("run_argv should not raise: {}".format(exc))
        self.assertIn("TOOL TIMEOUT", text)

    def test_missing_binary_is_reported_not_raised(self):
        text = tools.run_argv(["whybroke-does-not-exist", "--x"], 2)
        self.assertIn("TOOL UNAVAILABLE", text)


class RepairTests(unittest.TestCase):
    def test_repairs_plain_json(self):
        call = agent.repair_tool_call(
            'I should check: {"name": "disk_usage", "arguments": {}} done'
        )
        self.assertEqual(call["function"]["name"], "disk_usage")
        self.assertEqual(call["function"]["arguments"], {})

    def test_repairs_fenced_json(self):
        call = agent.repair_tool_call(
            '```json\n{"name": "service_status", "arguments": {"unit": "nginx"}}\n```'
        )
        self.assertEqual(call["function"]["arguments"]["unit"], "nginx")

    def test_repairs_string_arguments(self):
        call = agent.repair_tool_call(
            '{"name": "unit_logs", "arguments": "{\\"unit\\": \\"sshd\\"}"}'
        )
        self.assertEqual(call["function"]["arguments"], {"unit": "sshd"})

    def test_ignores_prose(self):
        self.assertIsNone(agent.repair_tool_call("no json here, the disk looks full"))
        self.assertIsNone(agent.repair_tool_call(""))


class ReportTests(unittest.TestCase):
    SAMPLE = (
        "SYMPTOM: nginx stopped serving requests.\n"
        "EVIDENCE: journalctl unit nginx.service reports 'bind() to 0.0.0.0:80 failed'\n"
        "LIKELY ROOT CAUSE: port 80 already held by another process.\n"
        "SUGGESTED FIX: identify the owner with ss -tulnp and stop it.\n"
        "CONFIDENCE: high, the log line names the exact failure."
    )

    def test_parses_all_sections(self):
        sections = report.parse(self.SAMPLE)
        self.assertIn("port 80", sections["LIKELY ROOT CAUSE"])
        self.assertIn("nginx stopped", sections["SYMPTOM"])
        self.assertEqual(sections["CONFIDENCE"].split()[0], "high,")

    def test_parses_markdown_bold_headings(self):
        text = self.SAMPLE.replace("SYMPTOM:", "**SYMPTOM:**").replace(
            "EVIDENCE:", "\n## EVIDENCE:"
        )
        sections = report.parse(text)
        self.assertIn("nginx stopped", sections["SYMPTOM"])
        self.assertIn("bind()", sections["EVIDENCE"])

    def test_missing_sections_become_not_identified(self):
        sections = report.parse("SYMPTOM: something is odd")
        self.assertEqual(sections["LIKELY ROOT CAUSE"], "Not identified")
        self.assertEqual(sections["CONFIDENCE"], "Not identified")

    def test_render_includes_never_applies_note(self):
        rendered = report.render(report.parse(self.SAMPLE), "why is nginx down?")
        self.assertIn("never applies changes", rendered)
        for heading in report.HEADINGS:
            self.assertIn(heading.title(), rendered)

    def test_has_all_sections(self):
        self.assertTrue(report.has_all_sections(self.SAMPLE))
        self.assertFalse(report.has_all_sections("still investigating"))


class PromptTests(unittest.TestCase):
    def test_prompt_contains_contract(self):
        prompt = agent.system_prompt(10)
        for needle in (
            "whybroke",
            "never modify",
            "exactly one tool",
            "at most 10 tool calls",
            "SYMPTOM:",
            "LIKELY ROOT CAUSE:",
            "CONFIDENCE:",
        ):
            self.assertIn(needle, prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)