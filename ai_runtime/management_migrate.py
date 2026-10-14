"""Explicit, repeatable import of existing YAML into API override records."""

import json
import sys

from .config import RuntimeFailure, ServiceResolver
from .management_store import ManagementStore


def main():
    try:
        result = ManagementStore(ServiceResolver.from_environment()).migrate_yaml()
    except RuntimeFailure as error:
        print(json.dumps({"error": error.code}), file=sys.stderr)
        return 1
    print(json.dumps(result))  # Counts and schema version only, no configuration.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
