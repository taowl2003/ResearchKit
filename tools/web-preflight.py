#!/usr/bin/env python3
"""Read-only readiness report for the ResearchKit Web deployment boundary."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# This tool must work from a source checkout and must not depend on installation.
ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from researchkit.web_identity import preflight_report  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="check ResearchKit Web deployment prerequisites")
    parser.add_argument("--policy", type=Path, help="root-owned production policy JSON")
    args = parser.parse_args(argv)
    report = preflight_report(args.policy)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
