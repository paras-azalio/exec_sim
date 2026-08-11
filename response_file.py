"""Two operator-friendly ways to tell the simulator what to answer.

Both are converted into the same dict the `Scenario` class already consumes, so
nothing in the command engine has to change.

------------------------------------------------------------------------------
1) Flat text  (--responses answers.txt)
------------------------------------------------------------------------------
One line per exchange: the command (a regex) then `=>` then the reply. The reply
is either inline text or `@file` pointing at a .txt/.xml holding the response -
which is what you want for anything multi-line like a NETCONF dump.

    # comment lines and blank lines are ignored
    date \\+%Y-%m-%d_%H-%M-%S            => 2026-08-11_21-30-00
    netconfprov .*--user root            =>                        | exit=1
    netconfprov .*--user netconfcli      =>                        | exit=0
    cat /storage/CapacityLicenseKey_.*   => @answers/CapacityLicenseKey.xml

Options go after a `|` and are comma or pipe separated:

    exit=N            process exit code (default 0)
    profile=NAME      only answer this profile: sbc | repo | node1 | node2 | any
    occurrences=1,3   only on the 1st and 3rd time the command is seen

------------------------------------------------------------------------------
2) Single YAML  (--responses answers.yaml)
------------------------------------------------------------------------------
Request and response live together in one file:

    profile: sbc            # default for every exchange below
    exchanges:
      - request:  'date \\+%Y-%m-%d_%H-%M-%S'
        response: '2026-08-11_21-30-00'
      - request:  'netconfprov .*--user root'
        response: ''
        exit: 1
      - request:  'cat /storage/CapacityLicenseKey_.*'
        response_file: answers/CapacityLicenseKey.xml

`@file` / `response_file` paths are resolved relative to the response file.
"""
import os
import re

_SPLIT = "=>"


def _read_response_file(base_dir, ref):
    path = ref if os.path.isabs(ref) else os.path.join(base_dir, ref)
    if not os.path.exists(path):
        raise SystemExit("response file not found: %s" % path)
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _parse_opts(blob):
    """`exit=1, profile=sbc, occurrences=1,2` -> dict for a ScenarioRule."""
    out = {}
    if not blob:
        return out
    for part in re.split(r"[|,](?![0-9])", blob):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = (x.strip() for x in part.split("=", 1))
        if k == "exit":
            out["exit"] = int(v)
        elif k == "profile":
            out["profile"] = v
        elif k == "occurrences":
            out["occurrences"] = [int(x) for x in re.split(r"[,\s]+", v) if x]
    return out


def _load_txt(path):
    base = os.path.dirname(os.path.abspath(path))
    rules = []
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip("\n").rstrip("\r")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if _SPLIT not in line:
                raise SystemExit("%s:%d: expected '%s' separator" % (path, lineno, _SPLIT))
            cmd, rest = line.split(_SPLIT, 1)

            opts_blob = ""
            if "|" in rest:
                rest, opts_blob = rest.split("|", 1)
            reply = rest.strip()

            rule = {"match": cmd.strip()}
            if reply.startswith("@"):
                rule["stdout"] = _read_response_file(base, reply[1:].strip())
            else:
                rule["stdout"] = reply
            rule.update(_parse_opts(opts_blob))
            rules.append(rule)
    return rules


def _load_yaml(path):
    import yaml
    base = os.path.dirname(os.path.abspath(path))
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}

    # allow a plain list of exchanges, or a mapping with defaults
    if isinstance(doc, list):
        doc = {"exchanges": doc}
    default_profile = doc.get("profile")

    rules = []
    for ex in doc.get("exchanges") or []:
        if "request" not in ex:
            raise SystemExit("%s: an exchange is missing 'request'" % path)
        rule = {"match": ex["request"]}
        if ex.get("response_file"):
            rule["stdout"] = _read_response_file(base, ex["response_file"])
        else:
            rule["stdout"] = ex.get("response", "") or ""
        if "exit" in ex:
            rule["exit"] = int(ex["exit"])
        prof = ex.get("profile", default_profile)
        if prof:
            rule["profile"] = prof
        if ex.get("occurrences"):
            rule["occurrences"] = list(ex["occurrences"])
        rules.append(rule)

    extra = {k: doc[k] for k in ("users", "profiles", "defaults", "checksum") if k in doc}
    return rules, extra


def load(path):
    """Return a scenario-raw dict built from a .txt or .yaml response file."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".yaml", ".yml"):
        rules, extra = _load_yaml(path)
    else:
        rules, extra = _load_txt(path), {}
    raw = {"rules": rules}
    raw.update(extra)
    return raw


def merge(scenario_raw, responses_raw):
    """Response-file rules take precedence over scenario rules (checked first)."""
    merged = dict(scenario_raw or {})
    merged["rules"] = list(responses_raw.get("rules") or []) + list((scenario_raw or {}).get("rules") or [])
    for key in ("users", "profiles", "defaults", "checksum"):
        if key in responses_raw:
            merged.setdefault(key, responses_raw[key])
    return merged
