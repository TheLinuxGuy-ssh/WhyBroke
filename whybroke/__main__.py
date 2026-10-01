import argparse
import os
import sys

from . import agent as agent_module
from . import config, report


def build_parser():
    parser = argparse.ArgumentParser(
        prog="whybroke",
        description="Local-first Linux diagnostics agent. Read-only investigation with a local model.",
    )
    parser.add_argument("question", help="what went wrong, in plain language")
    parser.add_argument(
        "-m", "--model", default=config.MODEL, help="ollama model tag to use"
    )
    parser.add_argument(
        "-s",
        "--max-steps",
        type=int,
        default=config.MAX_STEPS,
        help="maximum tool calls before forcing a conclusion",
    )
    parser.add_argument(
        "--no-transcript", action="store_true", help="do not save a transcript"
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="check the environment and the safety layer, needs no model",
    )
    return parser


def main(argv=None):
    parser = build_parser()

    if argv is None:
        argv = sys.argv[1:]
    if "--doctor" in argv:
        from . import doctor

        return doctor.run()

    args = parser.parse_args(argv)

    if os.environ.get("WHYBROKE_FORCE_NO_OLLAMA") == "1":
        print(
            "the ollama client is not installed. run: pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 2

    try:
        import ollama
    except ImportError:
        print(
            "the ollama client is not installed. run: pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 2

    try:
        client = ollama.Client(host=config.OLLAMA_HOST)
        client.list()
    except Exception as exc:
        print(
            "cannot reach ollama at {} ({}). is the service running?".format(
                config.OLLAMA_HOST, exc
            ),
            file=sys.stderr,
        )
        return 2

    def log(message):
        print(message, file=sys.stderr, flush=True)

    def confirm(command):
        print("the model wants to run a non allowlisted command:", file=sys.stderr)
        print("  {}".format(command), file=sys.stderr)
        answer = input("approve? [y/N] ").strip().lower()
        return answer == "y"

    log("model {} | max steps {}".format(args.model, args.max_steps))

    diag = agent_module.Agent(
        client=client,
        model=args.model,
        max_steps=args.max_steps,
        confirm=confirm,
        log=log,
        transcript_dir=None if args.no_transcript else config.TRANSCRIPT_DIR,
    )

    raw = diag.investigate(args.question)
    sections = report.parse(raw)

    # A small model will sometimes invent a size it never read. Say so rather
    # than presenting it as a finding.
    fabricated = report.ungrounded_evidence(sections, diag.corpus)
    if fabricated:
        print(
            "WARNING: the model cited {} that no tool returned ({}). "
            "treat the evidence section as unreliable.".format(
                ", ".join(fabricated), "invented"
            ),
            file=sys.stderr,
        )
        sections["EVIDENCE"] = (
            "UNVERIFIED. The model cited values that no tool returned "
            "({}). Re-run with a higher step cap or a larger model before "
            "trusting this section. Original text kept below.\n\n{}".format(
                ", ".join(fabricated), sections["EVIDENCE"]
            )
        )

    if not any(sections[h] != "Not identified" for h in report.HEADINGS):
        print(
            report.render(sections, args.question),
            file=sys.stderr,
        )
        print("\nno conclusive diagnosis was reached.\n", file=sys.stderr)
        return 1

    print(report.render(sections, args.question))
    return 0


if __name__ == "__main__":
    sys.exit(main())