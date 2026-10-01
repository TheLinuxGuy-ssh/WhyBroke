import os

MODEL = "qwen2.5:3b"
FALLBACK_MODEL = "qwen2.5:7b"

MODEL = os.environ.get("WHYBROKE_MODEL", MODEL)
FALLBACK_MODEL = os.environ.get("WHYBROKE_FALLBACK_MODEL", FALLBACK_MODEL)

MAX_STEPS = int(os.environ.get("WHYBROKE_MAX_STEPS", "10"))

TOOL_TIMEOUT_S = 10
NUM_PREDICT = int(os.environ.get("WHYBROKE_NUM_PREDICT", "512"))
NUM_CTX = int(os.environ.get("WHYBROKE_NUM_CTX", "8192"))

MAX_OUTPUT_CHARS = 4000
MAX_LOG_LINES = 200
MAX_FILE_LINES = 200

LOG_DIRS = ("/var/log",)

# Roots the disk scanner may walk. Read-only, but du is slow on a big tree,
# so it is scoped to the places a human actually asks about.
DISK_SCAN_ROOTS = (
    os.path.expanduser("~"),
    "/var",
    "/tmp",
    "/opt",
    "/srv",
)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

TRANSCRIPT_DIR = os.path.expanduser("~/.whybroke/transcripts")

JOURNAL_SINCE_CHOICES = ("10m", "1h", "6h", "24h", "7d")