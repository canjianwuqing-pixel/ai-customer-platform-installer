"""Real Compose interpolation with a sudo-like environment and subdirectory cwd."""

import json
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent.parent
env = {key: value for key, value in os.environ.items()
       if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE"}}
result = subprocess.run([sys.argv[1], *sys.argv[2:], "config", "--format", "json"],
                        cwd=root / "stacks/chatwoot", env=env, capture_output=True)
if result.returncode:
    sys.exit("Filtered-environment Compose configuration failed (details suppressed).")
config = json.loads(result.stdout)
# Compose escapes literal dollars in its reusable config output.
assert config["services"]["rails"]["environment"]["POSTGRES_PASSWORD"].replace("$$", "$") == "legacy$literal # password suffix"
assert config["name"] == "ai-customer-platform-test"
print("Real Compose snapshot, literal credentials, filtered environment and subdirectory checks passed.")
