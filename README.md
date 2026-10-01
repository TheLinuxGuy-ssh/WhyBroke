██╗    ██╗██╗  ██╗██╗   ██╗██████╗ ██████╗  ██████╗ ██╗  ██╗███████╗
██║    ██║██║  ██║╚██╗ ██╔╝██╔══██╗██╔══██╗██╔═══██╗██║ ██╔╝██╔════╝
██║ █╗ ██║███████║ ╚████╔╝ ██████╔╝██████╔╝██║   ██║█████╔╝ █████╗  
██║███╗██║██╔══██║  ╚██╔╝  ██╔══██╗██╔══██╗██║   ██║██╔═██╗ ██╔══╝  
╚███╔███╔╝██║  ██║   ██║   ██████╔╝██║  ██║╚██████╔╝██║  ██╗███████╗
 ╚══╝╚══╝ ╚═╝  ╚═╝   ╚═╝   ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝

![license](https://img.shields.io/badge/license-MIT-22c55e)
![python](https://img.shields.io/badge/python-3.11%2B-22c55e)
![model](https://img.shields.io/badge/model-Qwen2.5%20open--weights-22c55e)
![runtime](https://img.shields.io/badge/runtime-Ollama-22c55e)
![platform](https://img.shields.io/badge/platform-Linux-22c55e)
![local](https://img.shields.io/badge/data-leaves%20your%20machine%3F%20no-22c55e)

A local-first Linux diagnostics agent. You describe a problem in plain language, it
investigates using read-only system tools, reasons over the output with a local
open-weight model, and returns a root-cause hypothesis with the exact log lines that
support it. Nothing leaves the machine. No paid API is used.

## Why local-first matters

When a server is on fire, the three things you least want to do are paste production logs
into a web form, wait on a remote API during an outage, or send the incident to a vendor.
Log files are the most sensitive artifacts a machine has: they carry internal hostnames,
IP ranges, customer identifiers, and sometimes tokens that were printed by accident. A
diagnostics tool that uploads its evidence is a tool you cannot use during an incident.

whybroke runs the model on your own hardware through Ollama, reads the system through a
fixed allowlist of read-only commands, and returns the report on your terminal. The
transcript is saved to `~/.whybroke/transcripts/` and stays there. You can unplug the
network and it works identically, which is also how you should run it on an air-gapped
production host.

## Architecture

```
                    ┌──────────────────────────────────────────┐
                    │              your question               │
                    │   "why did nginx stop responding?"       │
                    └────────────────────┬─────────────────────┘
                                         │
                    ┌────────────────────▼─────────────────────┐
                    │            whybroke/agent.py             │
                    │  bounded loop, step cap, final-answer    │
                    │  detection, small-model JSON repair      │
                    └───┬──────────────────────────────┬───────┘
          tool_calls   │                              │  text
                        │                              │
        ┌───────────────▼──────────────┐   ┌───────────▼──────────────┐
        │      whybroke/safety.py      │   │    whybroke/report.py    │
        │  default deny                │   │  Symptom                 │
        │  name lookup, per-arg        │   │  Evidence                │
        │  validators, redaction,      │   │  Likely root cause       │
        │  truncation, timeout         │   │  Suggested fix           │
        └───────────────┬──────────────┘   │  Confidence              │
                        │                  └──────────────────────────┘
        ┌───────────────▼──────────────────────┐
        │          whybroke/tools.py            │
        │  12 read-only tools, argv lists,     │
        │  subprocess.run, shell=False,        │
        │  per-tool timeout                    │
        └───────────────┬──────────────────────┘
                        │ argv list
        ┌───────────────▼──────────────────────┐
        │        the machine, read-only        │
        │  systemctl  journalctl  df  free     │
        │  uptime  ps  ss  docker  log files   │
        └──────────────────────────────────────┘

        model side (off the loop above, every turn):

        ┌──────────────────────────────────────┐
        │  Ollama  <-  qwen2.5:3b              │
        │  open weights, local inference       │
        │  receives tool schemas + results     │
        └──────────────────────────────────────┘
```

The loop is deliberately boring: send messages and tool schemas, read tool calls,
validate, execute, sanitize, feed back, stop on a report or the step cap. The model never
sees a command string it can execute, only a schema of what it may ask for.

## Install

Requires Python 3.11 or newer, Linux with systemd, and [Ollama](https://ollama.com).

```bash
# 1. ollama
curl -fsSL https://ollama.com/install.sh | sh
systemctl status ollama --no-pager

# 2. the model (about 2 GB)
ollama pull qwen2.5:3b
ollama list

# 3. whybroke
git clone https://github.com/YOUR_USER/whybroke.git
cd whybroke
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

`ollama` is the only dependency. Everything else is the standard library, including the
tests, which run on `unittest` rather than pytest.

### Model choice

| Role | Model | Footprint | Notes |
|---|---|---|---|
| Primary | `qwen2.5:3b` | about 2.0 GB RAM, no VRAM needed | reliable tool calling at 3B, runs on CPU, fits an 8 GB laptop |
| Fallback | `qwen2.5:7b` | about 4.7 GB RAM, or 5 GB VRAM | better reasoning, needs roughly 6 GB free to stay responsive |

Qwen2.5 was chosen over Llama 3.1 8B for this workload because function calling is the
bottleneck, not prose quality, and Qwen2.5 is stronger at emitting structured tool calls
at small sizes. On a machine with 16 GB RAM or any GPU, use the fallback:

```bash
python -m whybroke "why is the server slow" --model qwen2.5:7b
WHYBROKE_MODEL=qwen2.5:7b python -m whybroke "why did nginx stop"
```

## Run

```bash
python -m whybroke "why did nginx stop responding?"
python -m whybroke "is this machine out of memory or disk?"
python -m whybroke "why is my container restarting" --max-steps 12
```

The step log goes to stderr and the report to stdout, so you can pipe it:

```bash
python -m whybroke "why is ssh timing out" > report.md
```

Useful flags: `--model` to pick a tag, `--max-steps` to change the step budget,
`--no-transcript` to skip writing a transcript.

## Install the skill

The skill is standard Agent Skills format and loads into any compatible agent, not just
this harness. It carries the investigation order and six failure playbooks, so an agent
without whybroke still gets the methodology.

```bash
mkdir -p ~/.agents/skills
cp -r skill/whybroke ~/.agents/skills/whybroke
```

Then in any Agent Skills compatible agent:

```
use the whybroke skill to find out why the api service is failing
```

Layout is the standard one: `SKILL.md` with `name` and `description` frontmatter, folder
name matching the skill name, and a `references/` folder of playbooks. No scripts, no
runtime requirements, nothing to install.

## Safety model

The harness cannot execute an arbitrary command. The guarantee comes from structure, not
from filtering.

**Default deny.** A tool call is dispatched by dictionary lookup in a frozen registry of
twelve entries. A name that is not a key never reaches `subprocess`, it returns a
`BLOCKED` message naming the allowed tools.

**No shell, ever.** Every command is built as a Python list of strings and executed with
`subprocess.run(argv, ...)` with no shell. Pipes, redirects, `&&`, `;` and `$(...)` are
not blocked by pattern matching, they are structurally impossible, because there is no
shell to interpret them.

**Per-argument validation.** A unit name must full-match `[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}`,
a container name a similar pattern, ports are ints in range, line counts are clamped to
1 through 200, `since` is an enum, and unknown argument keys are rejected outright. This
is what stops `unit="nginx; rm -rf /"`; it fails validation before any process starts.

**Path containment.** The bounded log reader resolves the path with `os.path.realpath`,
which collapses `..` and follows symlinks, then requires the resolved path to sit inside
an allowed log directory. `/var/log/../../etc/shadow` is rejected. Prefixed-but-not-inside
paths like `/var/log_evil` are rejected too.

**Bounded execution.** Every tool has a timeout of 10 or 15 seconds. Nonzero exit codes
and timeouts come back as structured `TOOL ERROR` / `TOOL TIMEOUT` strings rather than
exceptions, which is both safer and easier for a small model to handle.

**Redaction before the model sees it.** Output passes through seven redaction patterns
(private key blocks, URL credentials, bearer tokens, assigned secrets, JWTs, AWS keys,
long hex blobs) and is only then truncated. The count of redactions appears in the step
log, so you can see when a log contained something sensitive.

**Confirmation gate.** Anything not marked read-only requires an explicit typed `y` at the
terminal before it runs, and the full command is printed first. All twelve shipped tools
are read-only, so this gate exists for the case where you extend the registry, and it is
covered by a test rather than left as a claim.

**Suggest, never apply.** The agent writes fixes for you to run. It has no write path at
all.

## Tests

Safety tests run offline with no model and no network:

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

99 tests, all passing, no model or network required. They cover injection attempts through
unit names, container names, ports and `since` values, path traversal including prefix
confusion, redaction of seven secret shapes, truncation bounds, tool timeouts, missing
binaries, the repair path for small-model JSON, the report parser, the agent loop driven by
a scripted fake client, and the repository itself: skill frontmatter and folder naming,
playbook completeness, shell syntax of every scenario, single-dependency hygiene, the CLI,
and the doctor.

A second live check runs the safety layer and every tool against your actual machine, also
with no model required:

```bash
python -m whybroke --doctor
```

### Broken-system scenarios

Each script breaks something on purpose and prints the diagnosis it expects.

```bash
sudo tests/scenarios/break_disk.sh        # 64MB loop filesystem filled to 100%
sudo tests/scenarios/break_unit.sh        # unit with a dangling ExecStart (203/EXEC)
bash  tests/scenarios/break_port.sh       # port 8099 bound twice
sudo tests/scenarios/break_container.sh  # container exiting 1 in a crashloop
sudo tests/scenarios/break_oom.sh        # container OOM killed, exit 137
sudo tests/scenarios/fix_all.sh          # undo everything
```

The table below states what each scenario is built to expose and the exact evidence that
would prove a correct diagnosis. It is a specification, not a claim of measured results.
Run the scripts, then fill the last column from the actual output.

| Scenario | Root cause to find | Evidence that proves it | Measured |
|---|---|---|---|
| Disk full | loop filesystem at 100%, write fails ENOSPC | `df -h` 100% on `/mnt/whybroke-test`, journal `No space left on device` | not run |
| Failed unit | `ExecStart` binary does not exist, status 203/EXEC | `systemctl status` shows `status=203/EXEC` | not run |
| Port in use | another process already holds 8099 | `ss -tulnp` shows the port bound to python3 | not run |
| Crashed container | entrypoint exits 1, restart policy crashloops it | `docker ps -a` Restarting, `docker logs` exit 1 | needs docker |
| OOM kill | container exceeded its 48MB cgroup limit, kernel killed it | exit 137, `OOMKilled=true`, kernel `Out of memory: Killed process` | needs docker |

Scoring rules and the failure modes worth watching for are in
[`tests/scenarios/EXPECTED.md`](tests/scenarios/EXPECTED.md).

Fill the Measured column from real output before submitting. A table with honest numbers,
failures included, reads better than a table of checkmarks.

## Verify it yourself

No model required. This runs the safety layer and every tool against the real machine:

```bash
python -m whybroke --doctor
```

```
PASS  python                             3.11+ required
PASS  systemd                            /run/systemd/system present
PASS  system prompt                      all 5 report headings
PASS  tool schemas                       12 tools, all with JSON schemas
PASS  allowlist is read-only             all 12 tools read-only
PASS  injection blocked                  unit, port and path traversal all rejected
PASS  unknown tool denied                'rm' blocked before any process started
PASS  redaction                          4 secrets redacted
PASS  truncation                         20000 chars -> 4030 keeping head and tail
PASS  tools execute live                 11/11 tools executed live
PASS  timeout enforced                   sleep 30 killed after 1s
```

A missing model is a warning, not a failure, so this still passes on a machine with no
model pulled. Exit code is 1 only if a safety check fails.

To see the demo story without a model, break something and read the tool output yourself:

```bash
sudo tests/scenarios/break_disk.sh      # prints the question and the expected diagnosis
python -m whybroke --doctor             # shows disk_usage reporting the full mount
sudo tests/scenarios/fix_all.sh
```

## Credits

Built on work by other people, all of which is credited here:

- [Ollama](https://github.com/ollama/ollama), MIT license, for local model serving.
- [`ollama-python`](https://github.com/ollama/ollama-python), MIT license, for the client.
- [Qwen2.5](https://github.com/QwenLM/Qwen2.5), Apache-2.0 license, for the open weights
  that do the reasoning. Weights are used through Ollama, unmodified.
- [Agent Skills](https://github.com/agentskills) open standard, for the `SKILL.md`
  frontmatter and directory conventions this project follows.

Original work in this repository: the investigation loop and its small-model tool-call
repair, the default-deny safety layer and its argument validators and redaction pass, the
twelve-tool registry and its schemas, the five-section report contract, the `--doctor` self
check, the test suite, the break scenarios, and the six diagnostic playbooks.

## Layout

```
whybroke/
  whybroke/
    config.py    constants and environment overrides
    safety.py    validators, redaction, truncation
    tools.py     ToolSpec registry and execution
    agent.py     the loop, prompts, repair, transcripts
    report.py    five-section parse and render
    __main__.py  CLI and confirmation gate
  skill/whybroke/
    SKILL.md
    references/  six playbooks
  whybroke/
    doctor.py    environment and safety self-test, no model needed
  tests/
    safety/test_safety.py
    test_agent.py   the loop driven by a scripted fake client
    test_repo.py    skill spec, scripts, packaging, CLI, doctor
    scenarios/      break_*.sh, fix_all.sh, EXPECTED.md
```

## License

MIT. See [LICENSE](LICENSE).