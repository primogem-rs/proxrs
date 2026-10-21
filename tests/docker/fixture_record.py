"""Optional append-only, credential-free fixture provenance for acceptance.py."""
from datetime import datetime, timezone
import json
import os
import sys


def record(event, **fields):
    path = os.environ.get("PROXRS_TEST_FIXTURES")
    if path:
        entry = dict(event=event, at=datetime.now(timezone.utc).isoformat(), **fields)
        with open(path, "a") as stream:
            stream.write(json.dumps(entry) + "\n")


if __name__ == "__main__":
    record(sys.argv[1], **dict(argument.split("=", 1) for argument in sys.argv[2:]))
