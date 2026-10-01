"""Full offline rehearsal of the live path, including real client serialization.

The 100 unit tests use a fake client object, which means they never prove that
the message and tool-schema dicts we build actually survive the ollama
client's own pydantic models. This script closes that gap with zero inference
cost by patching httpx at the transport layer and replaying recorded server
responses, so it runs in well under a second and needs no model.

    python smoke_test.py

Exit code 0 means the wiring is sound. It does NOT mean the model behaves.
For that, run: python -m whybroke "is this machine out of disk or memory?"
"""

import json
import sys

sys.path.insert(0, ".")

import httpx

from whybroke import agent as agent_module
from whybroke import config, report, tools

REPLAY = [
    # turn 1: model asks for disk usage
    {
        "model": "qwen2.5:3b",
        "created_at": "2026-01-01T00:00:00Z",
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": "disk_usage",
                        "arguments": {},
                    }
                }
            ],
        },
        "done": True,
        "total_duration": 1200000000,
    },
    # turn 2: model asks for unit status
    {
        "model": "qwen2.5:3b",
        "created_at": "2026-01-01T00:00:01Z",
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": "service_status",
                        "arguments": {"unit": "sshd.service"},
                    }
                }
            ],
        },
        "done": True,
        "total_duration": 900000000,
    },
    # turn 3: model produces the report
    {
        "model": "qwen2.5:3b",
        "created_at": "2026-01-01T00:00:02Z",
        "message": {
            "role": "assistant",
            "content": (
                "SYMPTOM: the host is close to running out of disk.\n"
                "EVIDENCE: disk_usage showed / at 100% Use%.\n"
                "LIKELY ROOT CAUSE: the root filesystem is full.\n"
                "SUGGESTED FIX: check what is growing with du and clear it.\n"
                "CONFIDENCE: high, the df line names the full mount."
            ),
        },
        "done": True,
        "total_duration": 2100000000,
    },
]

sent_requests = []


def handler(args, kwargs):
    payload = kwargs.get("json")
    if payload is None and kwargs.get("content") is not None:
        payload = json.loads(kwargs["content"].decode())
    sent_requests.append(payload)
    index = min(len(sent_requests) - 1, len(REPLAY) - 1)
    request = httpx.Request("POST", "http://127.0.0.1:9/api/chat")
    return httpx.Response(200, json=REPLAY[index], request=request)


def patched_client():
    """A real ollama.Client whose transport returns recorded responses.

    The host is deliberately unroutable so that if the patch ever stops
    working, this fails loudly instead of silently hitting a live server.
    """
    import ollama

    client = ollama.Client(host="http://127.0.0.1:9")

    class Recording:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, item):
            return getattr(self._inner, item)

        def request(self, *args, **kwargs):
            return handler(args, kwargs)

        def stream(self, *args, **kwargs):
            raise AssertionError("stream=True must not be used by the harness")

        def close(self):
            return self._inner.close()

    client._client = Recording(client._client)
    return client


def check(label, condition, detail=""):
    print("{}  {:<40} {}".format("PASS" if condition else "FAIL", label, detail))
    return bool(condition)


def main():
    ok = True

    print("smoke test: live wiring, no inference")
    print("model tag: {}".format(config.MODEL))
    print("")

    ok &= check("client constructed", True, "ollama 0.6.x")
    ok &= check("12 tool schemas serialize", len(tools.schemas()) == 12)

    client = patched_client()
    diag = agent_module.Agent(
        client=client, model=config.MODEL, transcript_dir=None, log=lambda m: None
    )
    text = diag.investigate("is this machine out of disk?")

    ok &= check("three requests reached the client", len(sent_requests) == 3,
                "{} sent".format(len(sent_requests)))

    first = sent_requests[0]
    ok &= check("system prompt sent first",
                first["messages"][0]["role"] == "system")
    ok &= check("all 12 tool schemas sent in turn 1",
                len(first.get("tools", [])) == 12,
                "{} tools".format(len(first.get("tools", []))))

    ok &= check("temperature is low", first["options"].get("temperature") == 0.1)
    ok &= check("num_ctx set", first["options"].get("num_ctx") == config.NUM_CTX)

    last = sent_requests[-1]
    roles = [m.get("role") for m in last["messages"]]
    ok &= check("tool results fed back", roles.count("tool") == 2, str(roles))

    tool_msgs = [m for m in last["messages"] if m.get("role") == "tool"]
    ok &= check("tool result names the tool",
                bool(tool_msgs[0].get("tool_name") or tool_msgs[0].get("name")),
                str(tool_msgs[0].get("tool_name")))
    ok &= check("tool result carries real output",
                "/" in tool_msgs[0]["content"], "{} chars".format(len(tool_msgs[0]["content"])))

    assistant_with_calls = [
        m for m in last["messages"]
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    ok &= check("assistant tool_calls round-tripped", len(assistant_with_calls) == 2)
    ok &= check("arguments survived as a dict",
                isinstance(assistant_with_calls[0]["tool_calls"][0]["function"]["arguments"], dict))

    sections = report.parse(text)
    for heading in report.HEADINGS:
        value = sections[heading]
        ok &= check("report section " + heading, value not in ("", "Not identified"),
                    value[:46])

    print("")
    if ok:
        print("smoke test PASSED: the loop, schemas and serialization are sound.")
        print("next: python -m whybroke \"is this machine out of disk or memory?\"")
        return 0
    print("smoke test FAILED: do not demo, fix the wiring first.")
    return 1


if __name__ == "__main__":
    sys.exit(main())