import unittest

from whybroke import agent as agent_module
from whybroke import config, report


class FakeMessage(dict):
    pass


class FakeClient:
    """Scripted stand-in for ollama.Client so the loop can be tested offline."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat(self, model=None, messages=None, tools=None, options=None):
        self.calls.append(
            {
                "model": model,
                "messages": list(messages),
                "tools": tools,
                "options": options,
            }
        )
        if not self.script:
            return {"message": {"content": "", "tool_calls": None}}
        return self.script.pop(0)


def call(name, **arguments):
    return {"message": {"content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}}


def say(content):
    return {"message": {"content": content, "tool_calls": None}}


FINAL = (
    "SYMPTOM: nginx stopped serving.\n"
    "EVIDENCE: unit_logs reported 'No space left on device' writing /mnt/x/writer.log\n"
    "LIKELY ROOT CAUSE: the loop filesystem is full.\n"
    "SUGGESTED FIX: free space with sudo journalctl --vacuum-size=200M\n"
    "CONFIDENCE: high, the errno is named in the log"
)


def run(script, **kwargs):
    client = FakeClient(script)
    diag = agent_module.Agent(
        client=client,
        model="fake",
        transcript_dir=None,
        confirm=kwargs.pop("confirm", None),
        **kwargs,
    )
    return client, diag.investigate("why did nginx stop?")


class LoopTests(unittest.TestCase):
    def test_two_tool_calls_then_report(self):
        client, text = run([call("disk_usage"), call("memory_usage"), say(FINAL)])
        sections = report.parse(text)
        self.assertIn("loop filesystem is full", sections["LIKELY ROOT CAUSE"])
        self.assertEqual(len(client.calls), 3)
        self.assertTrue(client.calls[0]["tools"], "tool schemas must be sent")

    def test_tool_result_fed_back_as_tool_role(self):
        client, _ = run([call("disk_usage"), say(FINAL)])
        second = client.calls[1]["messages"]
        tool_messages = [m for m in second if m.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0]["name"], "disk_usage")
        self.assertIn("/", tool_messages[0]["content"])

    def test_system_prompt_sent_first(self):
        client, _ = run([say(FINAL)])
        first = client.calls[0]["messages"][0]
        self.assertEqual(first["role"], "system")
        self.assertIn("whybroke", first["content"])

    def test_step_cap_forces_conclusion(self):
        script = [call("disk_usage") for _ in range(4)] + [say(FINAL)]
        client, text = run(script, max_steps=4)
        sections = report.parse(text)
        self.assertIn("loop filesystem", sections["LIKELY ROOT CAUSE"])
        self.assertEqual(len(client.calls), 5, "one forced conclusion call after the cap")
        fed = [m["content"] for m in client.calls[-1]["messages"] if m.get("role") == "user"]
        self.assertTrue(
            any("used all" in text for text in fed),
            "the step cap must tell the model to conclude, got {}".format(fed),
        )
        self.assertFalse(
            client.calls[-1]["tools"], "the conclusion call must not offer tools"
        )

    def test_repaired_json_tool_call(self):
        client, text = run(
            [
                say('{"name": "disk_usage", "arguments": {}}'),
                say(FINAL),
            ]
        )
        sections = report.parse(text)
        self.assertIn("loop filesystem", sections["LIKELY ROOT CAUSE"])
        fed_back = [m for m in client.calls[1]["messages"] if m.get("role") == "tool"]
        self.assertTrue(fed_back, "the repaired call must actually execute")

    def test_unknown_tool_blocked(self):
        client, text = run([call("rm_rf_slash"), say(FINAL)])
        tool_messages = [m for m in client.calls[1]["messages"] if m.get("role") == "tool"]
        self.assertIn("BLOCKED", tool_messages[0]["content"])
        self.assertIn("not in the read-only allowlist", tool_messages[0]["content"])

    def test_bad_arguments_rejected_before_execution(self):
        client, _ = run(
            [call("service_status", unit="nginx; rm -rf /"), say(FINAL)]
        )
        tool_messages = [m for m in client.calls[1]["messages"] if m.get("role") == "tool"]
        self.assertIn("INVALID ARGUMENTS", tool_messages[0]["content"])

    def test_output_redacted_before_model_sees_it(self):
        client, _ = run([call("memory_usage"), say(FINAL)])
        tool_messages = [m for m in client.calls[1]["messages"] if m.get("role") == "tool"]
        content = tool_messages[0]["content"]
        self.assertLessEqual(len(content), config.MAX_OUTPUT_CHARS + 60)

    def test_nudge_when_model_returns_prose(self):
        client, _ = run([say("let me think about this"), say(FINAL)])
        nudged = client.calls[1]["messages"][-1]
        self.assertEqual(nudged["role"], "user")
        self.assertIn("single tool call", nudged["content"])

    def test_tool_schema_sent_on_every_turn(self):
        client, _ = run([call("disk_usage"), call("memory_usage"), say(FINAL)])
        for recorded in client.calls[:-1]:
            names = {s["function"]["name"] for s in recorded["tools"]}
            self.assertIn("disk_usage", names)
            self.assertIn("service_status", names)

    def test_num_predict_capped(self):
        client, _ = run([say(FINAL)])
        self.assertEqual(client.calls[0]["options"]["num_predict"], config.NUM_PREDICT)

    def test_temperature_is_low_for_diagnosis(self):
        client, _ = run([say(FINAL)])
        self.assertLess(client.calls[0]["options"]["temperature"], 0.5)

    def test_context_window_is_set(self):
        client, _ = run([say(FINAL)])
        self.assertEqual(client.calls[0]["options"]["num_ctx"], config.NUM_CTX)


class ConfirmGateTests(unittest.TestCase):
    def test_confirmation_prompt_shown_for_non_allowlisted(self):
        from whybroke import tools
        from dataclasses import replace

        from whybroke import safety

        spec = replace(
            tools.REGISTRY["service_status"],
            name="restart_service",
            description="Restart a unit. Not read-only, used to prove the gate.",
            parameters={
                "type": "object",
                "properties": {"unit": {"type": "string"}},
                "required": ["unit"],
            },
            read_only=False,
            build=lambda a: ["systemctl", "restart", a["unit"]],
            validators={"unit": safety.check_unit},
        )
        tools.REGISTRY["restart_service"] = spec
        try:
            asked = []

            def confirm(command):
                asked.append(command)
                return False

            client = FakeClient([call("restart_service", unit="nginx"), say(FINAL)])
            diag = agent_module.Agent(
                client=client, model="fake", transcript_dir=None, confirm=confirm
            )
            diag.investigate("why is nginx down")
            self.assertEqual(asked, ["systemctl restart nginx"])
            blocked = [
                m for m in client.calls[1]["messages"] if m.get("role") == "tool"
            ][0]["content"]
            self.assertIn("BLOCKED by user", blocked)
        finally:
            del tools.REGISTRY["restart_service"]


if __name__ == "__main__":
    unittest.main(verbosity=2)