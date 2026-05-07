#!/usr/bin/env python3
"""
Fleet Control Config Schema Generator — Java Agent (prototype)

Reads newrelic.yml from the Java agent repo and writes JSON Schema Draft 2020-12
to .fleetControl/schemas/config-k8s.json and config-host.json.

Exit codes:
  0 — no schema changes (or first run)
  1 — schema changed (CI should commit the updated files)

Dependencies: PyYAML (pip install pyyaml)
"""

import json
import os
import re
import sys
import urllib.request

import yaml

# ---------------------------------------------------------------------------
# Source — GitHub raw URL by default, override via NEWRELIC_YML env var
# ---------------------------------------------------------------------------
GITHUB_RAW_URL = (
    "https://raw.githubusercontent.com/newrelic/newrelic-java-agent"
    "/main/newrelic-agent/src/main/resources/newrelic.yml"
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_DIR = os.path.join(SCRIPT_DIR, "schemas")


def load_newrelic_yml() -> str:
    """Return raw YAML text from a local file path or the GitHub URL."""
    local_path = os.environ.get("NEWRELIC_YML")
    if local_path:
        print(f"Reading local file: {local_path}")
        with open(local_path, "r", encoding="utf-8") as f:
            return f.read()
    print(f"Fetching from GitHub: {GITHUB_RAW_URL}")
    with urllib.request.urlopen(GITHUB_RAW_URL, timeout=15) as resp:
        return resp.read().decode("utf-8")

SCHEMA_DIR = os.path.join(SCRIPT_DIR, "schemas")
K8S_SCHEMA_PATH = os.path.join(SCHEMA_DIR, "config-k8s.json")

# ---------------------------------------------------------------------------
# Enum / special-value overrides derived from comment analysis
# ---------------------------------------------------------------------------
ENUM_OVERRIDES = {
    # "section.key": ["val1", "val2", ...]
    "log_level": ["off", "severe", "warning", "info", "fine", "finer", "finest"],
    "transaction_tracer.record_sql": ["off", "raw", "obfuscated"],
    "attributes.http_attribute_mode": ["standard", "legacy", "both"],
    "security.mode": ["IAST", "RASP"],
    "distributed_tracing.sampler.remote_parent_sampled": ["default", "always_on", "always_off"],
    "distributed_tracing.sampler.remote_parent_not_sampled": ["default", "always_on", "always_off"],
}

# ---------------------------------------------------------------------------
# Keys to exclude (private / internal / debugging / derived at runtime)
# ---------------------------------------------------------------------------
EXCLUDE_KEYS = {
    "class_transformer",          # complex instrumentation controls — not Fleet Control scope
    "obfuscate_jvm_props",        # JVM-internal, not K8s config
    "metric_ingest_uri",          # auto-derived from license key
    "event_ingest_uri",           # auto-derived from license key
    "send_jvm_props",             # JVM-internal
    "max_stack_trace_lines",      # debugging/internal
    "log_file_count",             # agent-local logging internals
    "log_limit_in_kbytes",        # agent-local logging internals
    "log_daily",                  # agent-local logging internals
    "log_file_name",              # agent-local logging internals
    "log_file_path",              # agent-local logging internals
    "audit_mode",                 # debugging
    "thread_profiler",            # not available to Lite accounts, niche use
    "security.agent",             # internal sub-flag
    "security.scan_controllers",  # advanced IAST controls
    "security.scan_schedule",     # advanced IAST controls
    "security.exclude_from_iast_scan",  # advanced IAST controls
    "slow_transactions",          # internal / advanced
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def strip_erb(value: str) -> str:
    """Replace ERB placeholders with a descriptive placeholder string."""
    return re.sub(r"<%=\s*\w+\s*%>", "<%= required %>", value)


def infer_type(value):
    """Map a Python value to JSON Schema type string."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def make_property(key_path: str, value, description: str = "") -> dict:
    """Build a JSON Schema property node."""
    flat_key = key_path.split(".")[-1]

    # Check enum overrides
    if key_path in ENUM_OVERRIDES or flat_key in ENUM_OVERRIDES:
        enum_vals = ENUM_OVERRIDES.get(key_path) or ENUM_OVERRIDES.get(flat_key)
        prop = {"type": "string", "enum": enum_vals}
        if value is not None and value in enum_vals:
            prop["default"] = value
    else:
        json_type = infer_type(value)
        prop = {"type": json_type}

        if json_type == "array":
            prop["items"] = {"type": "string"}
        elif json_type == "object":
            prop["type"] = "object"
            prop["additionalProperties"] = True

        if value is not None and json_type not in ("object", "array"):
            # Stringify ERB placeholders
            if isinstance(value, str) and "<%=" in value:
                prop["default"] = strip_erb(value)
            else:
                prop["default"] = value

    if description:
        prop["description"] = description.strip()

    return prop


# ---------------------------------------------------------------------------
# YAML comment extraction
# Reads the raw YAML text and builds a map: yaml_key_path → comment text
# ---------------------------------------------------------------------------

def extract_comments(raw_text: str) -> dict:
    """
    Very lightweight comment extractor.
    Returns {last_seen_key: comment_block} by scanning line-by-line.
    """
    comments = {}
    pending_comment_lines = []
    # Track indentation levels → key stack
    indent_key_stack = []  # list of (indent_level, key)

    for line in raw_text.splitlines():
        stripped = line.lstrip()
        indent = len(line) - len(stripped)

        if stripped.startswith("#"):
            pending_comment_lines.append(stripped[1:].strip())
            continue

        # A non-comment, non-blank line that could be a key
        key_match = re.match(r"^([a-zA-Z_][\w\-]*)\s*:", stripped)
        if key_match and not stripped.startswith("-"):
            key = key_match.group(1)
            # Pop stack entries that are deeper or equal indent
            while indent_key_stack and indent_key_stack[-1][0] >= indent:
                indent_key_stack.pop()
            indent_key_stack.append((indent, key))
            key_path = ".".join(k for _, k in indent_key_stack)

            if pending_comment_lines:
                comments[key_path] = " ".join(pending_comment_lines)
            pending_comment_lines = []
        else:
            # Non-key line resets pending comment if it's a value line
            if stripped and not stripped.startswith("#"):
                pending_comment_lines = []

    return comments


# ---------------------------------------------------------------------------
# Schema builder — recursively walk the `common:` section
# ---------------------------------------------------------------------------

def build_properties(data: dict, comments: dict, prefix: str = "") -> dict:
    """
    Recursively convert a dict of config values into JSON Schema properties.
    Returns {"properties": {...}, "required": [...]}
    """
    properties = {}

    for key, value in data.items():
        key_path = f"{prefix}.{key}" if prefix else key
        flat_key = key_path.lstrip("common.")

        # Skip excluded keys (check both full path and tail)
        if any(flat_key == ex or flat_key.startswith(ex + ".") for ex in EXCLUDE_KEYS):
            continue

        # Comment lookup — try both "common.<path>" and "<path>"
        desc = comments.get(f"common.{flat_key}", "") or comments.get(flat_key, "")

        if isinstance(value, dict) and value:
            # Nested object — recurse
            nested = build_properties(value, comments, prefix=key_path)
            prop = {
                "type": "object",
                "properties": nested["properties"],
                "additionalProperties": True,
            }
            if nested.get("required"):
                prop["required"] = nested["required"]
            if desc:
                prop["description"] = desc.strip()
        else:
            prop = make_property(flat_key, value, desc)

        properties[key] = prop

    return {"properties": properties, "required": []}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def generate_schema(raw_text: str) -> dict:

    # Strip ERB for safe YAML parsing
    safe_text = re.sub(r"<%=.*?%>", '"__ERB_PLACEHOLDER__"', raw_text)

    # PyYAML may complain about YAML anchors in some edge cases; use safe_load
    data = yaml.safe_load(safe_text)
    common = data.get("common", {})

    comments = extract_comments(raw_text)
    built = build_properties(common, comments, prefix="common")
    properties = built["properties"]

    # Mark license_key and app_name as required at the top level
    required = ["license_key", "app_name"]

    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "New Relic Java Agent Configuration",
        "description": (
            "Fleet Control configuration schema for the New Relic Java agent. "
            "Generated from newrelic-agent/src/main/resources/newrelic.yml."
        ),
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": True,
    }

    # Override license_key to be a required string (ERB placeholder → required string)
    if "license_key" in properties:
        properties["license_key"] = {
            "type": "string",
            "description": (
                "New Relic license key associated with your account. "
                "Binds the agent's data to your account in the New Relic UI."
            ),
            "minLength": 1,
        }

    return schema


def diff_schemas(old: dict, new: dict, path: str = "") -> list:
    """Return a list of human-readable change descriptions."""
    changes = []
    old_props = old.get("properties", {})
    new_props = new.get("properties", {})

    for key in set(list(old_props) + list(new_props)):
        child_path = f"{path}.{key}" if path else key
        if key not in old_props:
            changes.append(f"  + added:   {child_path}")
        elif key not in new_props:
            changes.append(f"  - removed: {child_path}")
        else:
            old_p = old_props[key]
            new_p = new_props[key]
            if old_p.get("type") == "object" and new_p.get("type") == "object":
                changes.extend(diff_schemas(old_p, new_p, child_path))
            elif old_p != new_p:
                changes.append(f"  ~ changed: {child_path}")

    return sorted(changes)


def load_existing(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def write_schema(schema: dict, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2)
        f.write("\n")


def main():
    raw_text = load_newrelic_yml()
    new_schema = generate_schema(raw_text)

    old_k8s = load_existing(K8S_SCHEMA_PATH)
    changes = diff_schemas(old_k8s, new_schema)

    write_schema(new_schema, K8S_SCHEMA_PATH)
    print(f"Wrote:   {K8S_SCHEMA_PATH}")

    if not old_k8s:
        print("\nFirst run — schema created.")
        sys.exit(0)

    if changes:
        print(f"\nSchema changes detected ({len(changes)}):")
        for c in changes:
            print(c)
        sys.exit(1)
    else:
        print("\nNo schema changes.")
        sys.exit(0)


if __name__ == "__main__":
    main()
