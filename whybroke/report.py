import re

HEADINGS = (
    "SYMPTOM",
    "EVIDENCE",
    "LIKELY ROOT CAUSE",
    "SUGGESTED FIX",
    "CONFIDENCE",
)

TEMPLATE = "\n\n".join(
    "{}: {}".format(heading, placeholder)
    for heading, placeholder in (
        ("SYMPTOM", "<one line>"),
        ("EVIDENCE", "<exact quoted lines and values, with the tool each came from>"),
        ("LIKELY ROOT CAUSE", "<one to three lines>"),
        ("SUGGESTED FIX", "<steps or commands for the user to run themselves>"),
        ("CONFIDENCE", "<low, medium, or high, plus one line why>"),
    )
)


def has_all_sections(text):
    upper = text.upper()
    return all(heading in upper for heading in HEADINGS)


def parse(text):
    """Split the model's final message into the five report sections."""
    pattern = re.compile(
        r"^\s*(?:[*#]+\s*)?(" + "|".join(re.escape(h) for h in HEADINGS) + r")\s*:?\s*",
        re.IGNORECASE | re.MULTILINE,
    )
    matches = list(pattern.finditer(text or ""))
    sections = {}

    for index, match in enumerate(matches):
        heading = match.group(1).upper()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip(" \t\n*#-")
        if heading in sections and not sections[heading]:
            sections[heading] = body
        elif heading not in sections:
            sections[heading] = body

    for heading in HEADINGS:
        sections.setdefault(heading, "")
        if not sections[heading] or sections[heading] == "<one line>":
            if heading != "SYMPTOM":
                sections[heading] = "Not identified"
    return sections


def render(sections, question=None):
    lines = []
    if question:
        lines.append("# whybroke report")
        lines.append("")
        lines.append("Question: {}".format(question))
        lines.append("")
    for heading in HEADINGS:
        lines.append("## {}".format(heading.title()))
        lines.append("")
        lines.append(sections.get(heading) or "Not identified")
        lines.append("")
    lines.append(
        "This report suggests fixes only. whybroke never applies changes to the system."
    )
    return "\n".join(lines)