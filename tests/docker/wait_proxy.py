#!/usr/bin/env python3
"""Keep a real waiting shell while its proxrs child has an invocation owner."""
from pathlib import Path
import sys

from proxy_session import ProxySession


with ProxySession.inherited(sys.argv[2:]) as session:
    Path(sys.argv[1]).write_text(str(session.pid))
    status = session.wait(180)
sys.exit(status)
