#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""A file-backed networksetup stand-in; never invokes system utilities."""
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["WG_TEST_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
operation = args[0]
service = args[1] if len(args) > 1 else None
if operation == "-listallnetworkservices":
    if "fail_list" in state:
        if state.get("list_output", "error") == "error":
            print("Error: simulated service-list failure")
        sys.exit(state["fail_list"])
    print("An asterisk (*) denotes that a network service is disabled.")
    print("\n".join(("*" if settings.get("disabled") else "") + name
                    for name, settings in state["services"].items()))
elif operation in ("-getdnsservers", "-getsearchdomains"):
    field = "dns" if operation == "-getdnsservers" else "search"
    if state["services"][service].get("disabled"):
        print(f"(Please note: {service} is currently disabled)")
    if state.get("banner_only") == field:
        sys.exit(0)
    if state.get("fail_get") == field:
        print("Error: simulated getter failure")
        sys.exit(1)
    values = state["services"][service][field]
    label = "DNS Servers" if field == "dns" else "Search Domains"
    print("\n".join(values) if values else f"There aren't any {label} set on {service}.")
elif operation in ("-setdnsservers", "-setsearchdomains"):
    field = "dns" if operation == "-setdnsservers" else "search"
    values = args[2:]
    with Path(os.environ["WG_TEST_WRITES"]).open("a") as out:
        phase = "cleanup" if Path(os.environ["WG_TEST_CLEANUP"]).exists() else "active"
        out.write(json.dumps({"field": field, "service": service, "values": values, "phase": phase}) + "\n")
    if state.get("fail_set") == field:
        print("Error: simulated setter failure")
        sys.exit(1)
    state["services"][service][field] = [] if values == ["Empty"] else values
    state_path.write_text(json.dumps(state))
else:
    raise SystemExit(f"Unsupported mock operation: {args!r}")
