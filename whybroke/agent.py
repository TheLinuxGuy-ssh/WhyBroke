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
        "- If a tool is blocked or returns nothing useful, choose a DIFFERENT tool or "
        "change an argument. Never repeat a call that was already refused.\n"
        "- To find what is filling a folder, use scan_disk_usage with an absolute "
        "path such as ~/Downloads or the home directory.\n"
        "- To find which single file is biggest, use largest_files with that path.\n"
        "- If you were told a service crashed, you MUST call service_status for that "
        "unit and then unit_logs for the same unit. Do not name a cause before you "
        "have read both.\n"
        "- Only scan_disk_usage and read_log_file take a path. read_log_file reads "
        "files under /var/log only.\n"
        "- EVIDENCE must contain the literal text and numbers from the tool output, "
        "not the tool call you made. If disk_usage printed a table, copy the "
        "interesting row into EVIDENCE. Writing only the call name is a failed "
        "diagnosis.\n"
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
        self._rejected = set()
        self._rejected_names = []
        self._collected = []

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
                final_text = self._force_conclusion(
                    messages, self._collected, list(self._rejected_names)
                )
                break

            messages.append(self._assistant_message(content, tool_calls))

            for call in tool_calls:
                name, arguments = _split_call(call)
                steps += 1
                signature = (name, _stable_args(arguments))
                if signature in self._rejected:
                    output = (
                        "BLOCKED AGAIN: you already tried this exact call and it was "
                        "refused. Do not repeat it. Pick a different tool, change an "
                        "argument, or conclude now with what you have."
                    )
                    blocked = True
                    redacted = 0
                    self.log("step {}/{} | repeat of a blocked call, refused".format(
                        steps, self.max_steps
                    ))
                else:
                    output, redacted, blocked = self._execute(name, arguments)
                    if blocked:
                        self._rejected.add(signature)
                        self._rejected_names.append(
                            "{}({})".format(name, _format_args(arguments))
                        )
                    elif len(self._collected) < 12:
                        self._collected.append(
                            "{} -> {}".format(name, output[:700])
                        )
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
                final_text = self._force_conclusion(
                    messages, self._collected, list(self._rejected_names)
                )
                break

        if not final_text:
            final_text = self._force_conclusion(
                messages, self._collected, list(self._rejected_names)
            )

        self._save_transcript(question, messages, final_text)
        return final_text

    @property
    def corpus(self):
        """All real tool output seen this run, for the grounding check."""
        return "\n".join(self._collected)

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

    def _force_conclusion(self, messages, collected=None, blocked=None):
        guidance = (
            "Give your final report now. Use only the five headings, no other text. "
            "Copy the actual output values into EVIDENCE."
        )
        if collected:
            guidance += (
                "\nYou must build EVIDENCE only from these tool results and nothing else:"
                + "\n".join(collected[-6:])
                + "\nCopy exact numbers and paths from them. Do not invent any value."
            )
        else:
            guidance += (
                "\nNo tool has produced any output, so you have no evidence. Write "
                "EVIDENCE: no tool produced output, then set CONFIDENCE: low. Do not "
                "state any file size, path, or log line you did not actually read."
            )
        if blocked:
            guidance += (
                "\nThese calls were refused, do not claim their results: "
                + ", ".join(blocked[:6])
            )

        response = self.client.chat(
            model=self.model,
            messages=messages + [{"role": "user", "content": guidance}],
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
        """Send both key spellings so 0.3.x and 0.6.x clients both work.

        ollama renamed the field from `name` to `tool_name`. The client models
        it with pydantic, which ignores the key it does not know, so sending
        both is safe on either version.
        """
        return {"role": "tool", "name": name, "tool_name": name, "content": output}

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


def _stable_args(arguments):
    return json.dumps(arguments, sort_keys=True, default=str)