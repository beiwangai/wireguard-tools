#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Test Darwin wg-quick DNS behavior with Python 3.9+ and Bash 4+.

Run: python3 src/wg-quick/tests/test_darwin.py [optional-source-path]
Set WG_QUICK_TEST_BASH to a Bash 4+ executable if PATH selects an older Bash.
No root privileges are required and no system network settings are changed.
Real function definitions run in disposable shells with mocked network utilities.
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("source", nargs="?", type=Path, default=HERE.parent / "darwin.bash")
TARGET = parser.parse_args().source.resolve()
BASH = os.environ.get("WG_QUICK_TEST_BASH") or shutil.which("bash")
if not BASH:
    raise SystemExit("Bash was not found. Set WG_QUICK_TEST_BASH to a Bash 4+ executable.")
try:
    version = subprocess.run(
        [BASH, "-c", 'printf "%s" "${BASH_VERSINFO[0]}"'],
        text=True, capture_output=True, check=True,
    ).stdout
except (OSError, subprocess.CalledProcessError) as error:
    raise SystemExit(f"Cannot run Bash: {error}. Set WG_QUICK_TEST_BASH to Bash 4+.")
if not version.isdigit() or int(version) < 4:
    raise SystemExit(
        f"Bash 4+ is required; {BASH} reports major version {version!r}. "
        "Set WG_QUICK_TEST_BASH to a Bash 4+ executable."
    )
text = TARGET.read_text()
entry = re.search(r"^if \[\[ \$# -eq 1 &&", text, re.M)
if entry is None:
    raise SystemExit("Cannot locate wg-quick runtime entry point; refusing to source executable code")
PREFIX = text[:entry.start()]


def run_case(name, dns, search, body, *, fail_get=None, fail_set=None, events=None,
             disabled=False, banner_only=None):
    with tempfile.TemporaryDirectory(prefix="wg-quick-test-") as temporary:
        folder = Path(temporary)
        state_path = folder / "state.json"
        writes_path = folder / "writes.jsonl"
        initial = {"services": {"Wi-Fi": {"dns": dns, "search": search}}}
        if disabled:
            initial["services"]["Wi-Fi"]["disabled"] = True
        if banner_only:
            initial["banner_only"] = banner_only
        if fail_get:
            initial["fail_get"] = fail_get
        if fail_set:
            initial["fail_set"] = fail_set
        state_path.write_text(json.dumps(initial))
        writes_path.touch()
        mock_bin = folder / "bin"
        mock_bin.mkdir()
        mock_route = mock_bin / "route"
        mock_route.write_text("#!/bin/sh\n[ \"$*\" = '-n monitor' ] || exit 0\nfor event in ${WG_TEST_EVENTS:-RTM_MISS: RTM_GET: RTM_GET2: RTM_MISS:}; do printf '%s\\n' \"$event\"; done\nsleep 0.2\n")
        mock_route.chmod(0o755)
        source = folder / "definitions.bash"
        source.write_text(PREFIX)
        mock_setup = r'''
source "$WG_TEST_SOURCE"
export PATH="$WG_TEST_BIN:$PATH"
networksetup() { "$WG_TEST_PYTHON" "$WG_TEST_MOCK" "$@"; }
ifconfig() { return 0; }
del_routes() { :; }
REAL_INTERFACE=utun-test
AUTO_ROUTE4=0
AUTO_ROUTE6=0
MTU=1420
LAUNCHED_BY_LAUNCHD=1
'''
        environment = os.environ | {
            "WG_TEST_SOURCE": str(source), "WG_TEST_STATE": str(state_path),
            "WG_TEST_WRITES": str(writes_path), "WG_TEST_PYTHON": sys.executable,
            "WG_TEST_MOCK": str(HERE / "mock_networksetup.py"),
            "WG_TEST_BIN": str(mock_bin),
            "WG_TEST_CLEANUP": str(folder / "cleanup-started"),
        }
        if events is not None:
            environment["WG_TEST_EVENTS"] = events
        process = subprocess.Popen([BASH, "-c", mock_setup + body], env=environment,
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=12)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
        result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        writes = [json.loads(line) for line in writes_path.read_text().splitlines()]
        state = json.loads(state_path.read_text())["services"]["Wi-Fi"]
        if result.returncode:
            raise AssertionError(f"shell exit {result.returncode}: {result.stderr[-1000:]}")
        return writes, state, result.stdout


def expect_fields(writes, fields):
    actual = [write["field"] for write in writes]
    assert actual == fields, f"expected setters {fields}, received {actual}"


def unchanged():
    writes, state, _ = run_case("unchanged", ["1.1.1.1", "8.8.8.8"], ["corp.example"], '''
DNS=(1.1.1.1 8.8.8.8); DNS_SEARCH=(corp.example)
set_dns
set_dns
set_dns
''')
    expect_fields(writes, [])


def dns_changed():
    writes, state, _ = run_case("dns", ["9.9.9.9"], ["corp.example"], '''
DNS=(1.1.1.1 8.8.8.8); DNS_SEARCH=(corp.example)
set_dns
set_dns
''')
    expect_fields(writes, ["dns"])
    assert state == {"dns": ["1.1.1.1", "8.8.8.8"], "search": ["corp.example"]}


def search_changed():
    writes, state, _ = run_case("search", ["1.1.1.1"], ["old.example"], '''
DNS=(1.1.1.1); DNS_SEARCH=(new.example)
set_dns
set_dns
''')
    expect_fields(writes, ["search"])
    assert state["search"] == ["new.example"]


def empty_search():
    writes, state, _ = run_case("empty", ["1.1.1.1"], ["old.example"], '''
DNS=(1.1.1.1); DNS_SEARCH=()
set_dns
set_dns
''')
    expect_fields(writes, ["search"])
    assert writes[0]["values"] == ["Empty"]
    assert state["search"] == []
    writes, _, _ = run_case("already-empty", ["1.1.1.1"], [], '''
DNS=(1.1.1.1); DNS_SEARCH=()
set_dns
set_dns
''')
    expect_fields(writes, [])


def restoration():
    writes, state, _ = run_case("restore", ["9.9.9.9", "149.112.112.112"], ["old.example"], '''
DNS=(1.1.1.1); DNS_SEARCH=()
set_dns
set_dns
del_dns
''')
    assert state == {"dns": ["9.9.9.9", "149.112.112.112"], "search": ["old.example"]}, state
    assert any(w["values"] == ["Empty"] for w in writes)


def getter_error():
    for field in ("dns", "search"):
        writes, state, _ = run_case("getter-error", ["9.9.9.9"], ["old.example"], '''
DNS=(1.1.1.1); DNS_SEARCH=(new.example)
if set_dns; then :; fi
if del_dns; then :; fi
''', fail_get=field)
        expected = ["9.9.9.9"] if field == "dns" else ["old.example"]
        assert state[field] == expected, f"failed {field} getter clobbered original: {state}"
        assert not [w for w in writes if w["field"] == field], f"setter ran after failed {field} getter: {writes}"


def setter_error():
    writes, state, _ = run_case("setter-error", ["9.9.9.9"], ["old.example"], '''
DNS=(1.1.1.1); DNS_SEARCH=(old.example)
if set_dns; then :; fi
if del_dns; then :; fi
''', fail_set="dns")
    assert state == {"dns": ["9.9.9.9"], "search": ["old.example"]}, state
    assert not any(w["values"] == ["Empty"] for w in writes), writes


def miss_get_monitor():
    writes, state, _ = run_case("monitor", ["1.1.1.1"], [], '''
DNS=(1.1.1.1); DNS_SEARCH=()
collect_new_service_dns
definition="$(declare -f del_dns)"
eval "${definition/del_dns ()/real_del_dns ()}"
del_dns() { touch "$WG_TEST_CLEANUP"; real_del_dns; }
monitor_daemon
monitor_pid=$!
wait "$monitor_pid" || true
''')
    active = [write for write in writes if write["phase"] == "active"]
    assert active == [], f"RTM_MISS/RTM_GET caused DNS reapplications: {active}"
    assert state == {"dns": ["1.1.1.1"], "search": []}, state


def no_dns_monitor():
    writes, state, _ = run_case("no-dns", ["9.9.9.9"], ["old.example"], '''
DNS=(); DNS_SEARCH=()
monitor_daemon
monitor_pid=$!
wait "$monitor_pid" || true
''')
    expect_fields(writes, [])
    assert state == {"dns": ["9.9.9.9"], "search": ["old.example"]}


def change_monitor():
    writes, state, _ = run_case("change-monitor", ["9.9.9.9"], ["corp.example"], '''
DNS=(1.1.1.1); DNS_SEARCH=(corp.example)
collect_new_service_dns
definition="$(declare -f del_dns)"
eval "${definition/del_dns ()/real_del_dns ()}"
del_dns() { touch "$WG_TEST_CLEANUP"; real_del_dns; }
monitor_daemon
monitor_pid=$!
wait "$monitor_pid" || true
''', events="RTM_CHANGE:")
    active = [write for write in writes if write["phase"] == "active"]
    expect_fields(active, ["dns"])
    assert active[0]["values"] == ["1.1.1.1"]
    assert state == {"dns": ["9.9.9.9"], "search": ["corp.example"]}, state


def added_service():
    writes, state, stdout = run_case("new-service", ["9.9.9.9"], ["corp.example"], '''
DNS=(1.1.1.1); DNS_SEARCH=(corp.example)
set_dns
"$WG_TEST_PYTHON" - <<'PY'
import json, os
from pathlib import Path
p = Path(os.environ["WG_TEST_STATE"])
s = json.loads(p.read_text())
s["services"]["USB Ethernet"] = {"dns": ["8.8.4.4"], "search": ["old.example"]}
p.write_text(json.dumps(s))
PY
set_dns
set_dns
del_dns
"$WG_TEST_PYTHON" - <<'PY'
import json, os
from pathlib import Path
print(json.dumps(json.loads(Path(os.environ["WG_TEST_STATE"]).read_text())["services"]))
PY
''')
    assert state == {"dns": ["9.9.9.9"], "search": ["corp.example"]}, state
    services = json.loads(stdout.strip().splitlines()[-1])
    assert services["USB Ethernet"] == {"dns": ["8.8.4.4"], "search": ["old.example"]}, services
    # Wi-Fi DNS once, new service DNS/search once each, plus two restores per service.
    assert len(writes) == 7, writes


def service_list_error():
    for status, output in ((1, "error"), (0, "error"), (0, "empty")):
        body = '''
DNS=(1.1.1.1); DNS_SEARCH=(new.example)
set_dns
"$WG_TEST_PYTHON" - <<'PY'
import json, os
from pathlib import Path
p = Path(os.environ["WG_TEST_STATE"])
s = json.loads(p.read_text())
s["fail_list"] = LIST_STATUS
s["list_output"] = LIST_OUTPUT
p.write_text(json.dumps(s))
PY
set_dns
del_dns
'''.replace("LIST_STATUS", str(status)).replace("LIST_OUTPUT", repr(output))
        writes, state, _ = run_case("list-error", ["9.9.9.9"], ["old.example"], body)
        assert state == {"dns": ["9.9.9.9"], "search": ["old.example"]}, (status, output, state)
        expect_fields(writes, ["dns", "search", "dns", "search"])
        assert writes[-2]["values"] == ["9.9.9.9"]
        assert writes[-1]["values"] == ["old.example"]


def disabled_empty_search_unchanged():
    writes, state, _ = run_case("disabled-empty-search", ["1.1.1.1"], [], '''
DNS=(1.1.1.1); DNS_SEARCH=()
set_dns
set_dns
set_dns
''', disabled=True)
    expect_fields(writes, [])
    assert state["dns"] == ["1.1.1.1"] and state["search"] == [], state


def disabled_search_unchanged():
    writes, state, _ = run_case("disabled-search", ["1.1.1.1"], ["corp.example"], '''
DNS=(1.1.1.1); DNS_SEARCH=(corp.example)
set_dns
set_dns
set_dns
''', disabled=True)
    expect_fields(writes, [])
    assert state["dns"] == ["1.1.1.1"] and state["search"] == ["corp.example"], state


def disabled_restoration():
    writes, state, _ = run_case("disabled-restore", ["9.9.9.9", "149.112.112.112"],
                               ["old.example"], '''
DNS=(1.1.1.1); DNS_SEARCH=()
set_dns
set_dns
del_dns
''', disabled=True)
    assert state["dns"] == ["9.9.9.9", "149.112.112.112"], state
    assert state["search"] == ["old.example"], state
    expect_fields(writes, ["dns", "search", "dns", "search"])
    assert writes[-2]["values"] == ["9.9.9.9", "149.112.112.112"]
    assert writes[-1]["values"] == ["old.example"]


def disabled_empty_restoration():
    writes, state, _ = run_case("disabled-empty-restore", [], [], '''
DNS=(1.1.1.1); DNS_SEARCH=(corp.example)
set_dns
set_dns
del_dns
''', disabled=True)
    assert state["dns"] == [] and state["search"] == [], state
    expect_fields(writes, ["dns", "search", "dns", "search"])
    assert writes[-2]["values"] == ["Empty"]
    assert writes[-1]["values"] == ["Empty"]


def disabled_banner_only():
    for field in ("dns", "search"):
        writes, state, _ = run_case("disabled-banner-only", ["9.9.9.9"],
                                   ["old.example"], '''
DNS=(1.1.1.1); DNS_SEARCH=(corp.example)
set_dns
del_dns
''', disabled=True, banner_only=field)
        expect_fields(writes, [])
        assert state["dns"] == ["9.9.9.9"] and state["search"] == ["old.example"], state


cases = [unchanged, dns_changed, search_changed, empty_search, restoration,
         getter_error, setter_error, miss_get_monitor, no_dns_monitor,
         change_monitor, added_service, service_list_error,
         disabled_empty_search_unchanged, disabled_search_unchanged,
         disabled_restoration, disabled_empty_restoration, disabled_banner_only]
failures = 0
for case in cases:
    try:
        case()
        print(f"PASS {case.__name__}")
    except (AssertionError, subprocess.TimeoutExpired) as error:
        failures += 1
        print(f"FAIL {case.__name__}: {error}")
print(f"{len(cases) - failures}/{len(cases)} tests passed for {TARGET}")
raise SystemExit(bool(failures))
