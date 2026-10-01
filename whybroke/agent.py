import json
import os
import re
import time

from . import config, report, safety, tools


def system_prompt(max_steps):
    return (
        "You are whybroke, a Linux diagnostics assistant. You investigate problems "
        "using ONLY the read-only tools provided. You never modify the system. You "
        "suggest fixes but never apply them.\n"
        "\n"
        "Method, in order: 1) service status, 2) recent logs, 3) resources "
        "(disk, memory, CPU), 4) network and ports. Stop as soon as the cause is clear.\n"
        "\n"
        "Rules:\n"
        "- Call exactly one tool per message.\n"
        "- If a tool fails or returns nothing useful, try a different tool. Do not repeat it.\n"
        "- Quote exact log lines and metric values as evidence. Never invent evidence.\n"
        "- You have at most {} tool calls.\n"
        "\n"
        "When you know the likely cause, reply with ONLY this report:\n"
        "{}".format(max_steps, report.TEMPLATE)
    )


class Agent:
    def __init__(
        self,
        client,
        model=config.MODEL,
        max_steps=config.MAX_STEPS,
        confirm=None,
        log=None,
        transcript_dir=config.TRANSCRIPT_DIR,
    ):
        self.client = client
        self.model = model
        self.max_steps = max_steps
        self.confirm = confirm or (lambda command: False)
        self.log = log or (lambda message: None)
        self.transcript_dir = transcript_dir
        self.system_prompt = system_prompt(max_steps)

    def investigate(self, question):
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": question},
        ]
        final_text = ""
        steps = 0
        nudges = 0

        while steps < self.max_steps:
            started = time.time()
            response = self.client.chat(
                model=self.model,
                messages=messages,
                tools=tools.schemas(),
                options=self._options(with_predict=True),
            )
            message = response["message"]
            elapsed = time.time() - started
            content = (message.get("content") or "").strip()
            tool_calls = message.get("tool_calls") or []

            if not tool_calls and content:
                repaired = repair_tool_call(content)
                if repaired:
                    tool_calls = [repaired]
                    self.log(
                        "step {}/{} | repaired a JSON tool call from the reply "
                        "({:.1f}s)".format(steps + 1, self.max_steps, elapsed)
                    )

            if not tool_calls:
                prose_only = content and not report.has_all_sections(content)
                if prose_only and nudges < 2:
                    nudges += 1
                    messages.append(
                        {"role": "assistant", "content": content}
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": "Reply with a single tool call, or produce the "
                            "final report using the required format.",
                        }
                    )
                    continue
                if content:
                    final_text = content
                    break
                self.log(
                    "step {}/{} | empty reply from the model, forcing a "
                    "conclusion".format(steps + 1, self.max_steps)
                )
                final_text = self._force_conclusion(messages)
                break

            messages.append(self._assistant_message(content, tool_calls))

            for call in tool_calls:
                name, arguments = _split_call(call)
                steps += 1
                output, redacted, blocked = self._execute(name, arguments)
                self.log(
                    "step {}/{} | {}({}) -> {} chars{} ({:.1f}s)".format(
                        steps,
                        self.max_steps,
                        name,
                        _format_args(arguments),
                        len(output),
                        ", {} redacted".format(redacted) if redacted else "",
                        time.time() - started,
                    )
                )
                messages.append(self._tool_message(name, output))
                if blocked:
                    break

            if steps >= self.max_steps:
                messages.append(
                    {
                        "role": "user",
                        "content": "You have used all {}. Conclude now with the "
                        "report format only.".format(self.max_steps),
                    }
                )
                final_text = self._force_conclusion(messages)
                break

        if not final_text:
            final_text = self._force_conclusion(messages)

        self._save_transcript(question, messages, final_text)
        return final_text

    @staticmethod
    def _options(with_predict):
        options = {
            "num_ctx": config.NUM_CTX,
            "temperature": 0.1,
            "top_p": 0.9,
            "repeat_penalty": 1.05,
        }
        if with_predict:
            options["num_predict"] = config.NUM_PREDICT
        return options

    def _execute(self, name, arguments):
        if name not in tools.REGISTRY:
            message = (
                "BLOCKED: {} is not in the read-only allowlist. Available tools: {}. "
                "Ask the user to run it manually if it is essential.".format(
                    name, ", ".join(sorted(tools.REGISTRY))
                )
            )
            self.log("BLOCKED {}: not in allowlist".format(name))
            return message, 0, True

        spec = tools.REGISTRY[name]

        try:
            cleaned = safety.validate(spec, arguments)
        except safety.SafetyError as exc:
            self.log("BLOCKED {}({}): {}".format(name, _format_args(arguments), exc))
            return "INVALID ARGUMENTS: {}".format(exc), 0, True

        if not spec.read_only:
            display = " ".join(spec.build(cleaned)) or spec.name
            if not self.confirm(display):
                return (
                    "BLOCKED by user: {} was not approved.".format(display),
                    0,
                    True,
                )

        try:
            output, redacted = tools.run_tool(spec, arguments)
            return output, redacted, False
        except safety.SafetyError as exc:
            self.log("BLOCKED {}({}): {}".format(name, _format_args(arguments), exc))
            return "INVALID ARGUMENTS: {}".format(exc), 0, True
        except (OSError, ValueError) as exc:
            return "TOOL ERROR: {}".format(exc), 0, False

    def _force_conclusion(self, messages):
        response = self.client.chat(
            model=self.model,
            messages=messages + [
                {
                    "role": "user",
                    "content": "Give your final report now. Use only the five "
                    "headings, no other text.",
                }
            ],
            options=self._options(with_predict=False),
        )
        return (response["message"].get("content") or "").strip()

    @staticmethod
    def _assistant_message(content, tool_calls):
        message = {"role": "assistant", "content": content or ""}
        message["tool_calls"] = [
            {
                "function": {
                    "name": call.get("function", {}).get("name", ""),
                    "arguments": call.get("function", {}).get("arguments", {}) or {},
                }
            }
            for call in tool_calls
        ]
        return message

    @staticmethod
    def _tool_message(name, output):
        return {"role": "tool", "name": name, "content": output}

    def _save_transcript(self, question, messages, final_text):
        if not self.transcript_dir:
            return
        try:
            os.makedirs(self.transcript_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = os.path.join(
                self.transcript_dir, "{}-{}.json".format(stamp, _slug(question))
            )
            with open(path, "w") as handle:
                json.dump(
                    {
                        "question": question,
                        "model": self.model,
                        "messages": messages,
                        "report": final_text,
                    },
                    handle,
                    indent=2,
                )
            self.log("transcript -> {}".format(path))
        except OSError as exc:
            self.log("could not write transcript: {}".format(exc))


def _slug(text, limit=40):
    cleaned = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (cleaned[:limit].rstrip("-")) or "session"


def repair_tool_call(content):
    """Extract a tool call that a small model emitted as raw JSON in its reply."""
    start = content.find("{")
    while start != -1:
        depth = 0
        for index in range(start, len(content)):
            if content[index] == "{":
                depth += 1
            elif content[index] == "}":
                depth -= 1
                if depth == 0:
                    chunk = content[start : index + 1]
                    parsed = _as_call(chunk)
                    if parsed:
                        return parsed
                    break
        start = content.find("{", start + 1)
    return None


def _as_call(chunk):
    try:
        data = json.loads(chunk)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    name = data.get("name") or data.get("tool") or data.get("function")
    if isinstance(name, dict):
        name = name.get("name")
        data = data.get("arguments") or data.get("parameters") or {}

    if not isinstance(name, str) or not name:
        return None

    arguments = data.get("arguments") or data.get("parameters") or data.get("args") or {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}

    return {"function": {"name": name, "arguments": arguments}}


def _split_call(call):
    function = call.get("function", call)
    name = function.get("name", "")
    arguments = function.get("arguments", {}) or {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    return name, arguments


def _format_args(arguments):
    parts = ["{}={!r}".format(key, arguments[key]) for key in sorted(arguments)]
    return ", ".join(parts) or "no args"