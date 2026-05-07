#!/usr/bin/env python3
"""
Fleet Control Config Schema Generator — Python Agent

Fetches newrelic/newrelic.ini from the Python agent repo and writes JSON Schema
Draft 2020-12 to .fleetControl/schemas/config-k8s.json and config-host.json.

INI dot-notation keys (e.g. transaction_tracer.enabled) map to nested JSON
Schema objects. Only the [newrelic] section is processed.

Exit codes:
  0 — no schema changes (or first run)
  1 — schema changed (CI should commit the updated files)

Dependencies: stdlib only (configparser, json, urllib.request)
"""

import configparser
import io
import json
import os
import re
import sys
import urllib.request

GITHUB_RAW_URL = (
    "https://raw.githubusercontent.com/newrelic/newrelic-python-agent"
    "/main/newrelic/newrelic.ini"
)

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
SCHEMA_DIR  = os.path.join(SCRIPT_DIR, "schemas")
K8S_SCHEMA  = os.path.join(SCHEMA_DIR, "config-k8s.json")

# ---------------------------------------------------------------------------
# Enum overrides
# ---------------------------------------------------------------------------
ENUM_OVERRIDES = {
    "log_level": ["critical", "error", "warning", "info", "debug"],
    "transaction_tracer.record_sql": ["off", "raw", "obfuscated"],
}

# Keys to exclude (internal/debugging)
EXCLUDE_KEYS = {
    "debug.log_agent_initialization",
    "debug.log_data_collector_calls",
    "debug.log_data_collector_payloads",
    "debug.log_malformed_json_data",
}

# ---------------------------------------------------------------------------
# Fetch source
# ---------------------------------------------------------------------------

def load_newrelic_ini() -> str:
    local = os.environ.get("NEWRELIC_INI")
    if local:
        print(f"Reading local file: {local}")
        with open(local, "r", encoding="utf-8") as f:
            return f.read()
    print(f"Fetching from GitHub: {GITHUB_RAW_URL}")
    with urllib.request.urlopen(GITHUB_RAW_URL, timeout=15) as resp:
        return resp.read().decode("utf-8")


# ---------------------------------------------------------------------------
# Comment extraction (INI format)
# Maps key -> comment text from lines preceding the key= line
# ---------------------------------------------------------------------------

def extract_comments(raw_text: str) -> dict:
    comments = {}
    pending = []
    for line in raw_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(";") or stripped.startswith("#"):
            pending.append(stripped.lstrip(";# ").strip())
            continue
        m = re.match(r"^([a-zA-Z_][\w\.]*)\s*=", stripped)
        if m:
            key = m.group(1)
            if pending:
                comments[key] = " ".join(pending)
            pending = []
        elif stripped and not stripped.startswith("["):
            pending = []
    return comments


# ---------------------------------------------------------------------------
# Type inference from INI string values
# ---------------------------------------------------------------------------

def infer_type_from_value(value: str):
    if value.lower() in ("true", "false"):
        return "boolean", value.lower() == "true"
    try:
        iv = int(value)
        return "integer", iv
    except ValueError:
        pass
    try:
        fv = float(value)
        return "number", fv
    except ValueError:
        pass
    return "string", value


# ---------------------------------------------------------------------------
# Build nested properties from flat INI dot-notation keys
# ---------------------------------------------------------------------------

def set_nested(d: dict, keys: list, value):
    """Set d[keys[0]][keys[1]]... = value, creating dicts as needed."""
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    d[keys[-1]] = value


def build_schema_tree(items: dict, comments: dict) -> dict:
    """
    Convert flat dot-notation items to nested JSON Schema properties.
    items: {dot.key: (json_type, default_value)}
    """
    tree = {}
    for dot_key, (json_type, default_val) in items.items():
        if dot_key in EXCLUDE_KEYS:
            continue
        parts = dot_key.split(".")
        set_nested(tree, parts, (json_type, default_val, comments.get(dot_key, "")))

    def dict_to_schema(d):
        properties = {}
        for key, val in d.items():
            if isinstance(val, dict):
                # Nested section
                nested_props = dict_to_schema(val)
                properties[key] = {
                    "type": "object",
                    "properties": nested_props,
                    "additionalProperties": True,
                }
            else:
                json_type, default_val, desc = val
                flat_path_key = key  # use key as fallback for enum override
                if dot_key in ENUM_OVERRIDES or key in ENUM_OVERRIDES:
                    enum_vals = ENUM_OVERRIDES.get(dot_key) or ENUM_OVERRIDES.get(key)
                    prop = {"type": "string", "enum": enum_vals}
                    if default_val in enum_vals:
                        prop["default"] = default_val
                else:
                    prop = {"type": json_type}
                    if default_val is not None and json_type not in ("object",):
                        prop["default"] = default_val
                if desc:
                    prop["description"] = desc
                properties[key] = prop
        return properties

    return dict_to_schema(tree)


# ---------------------------------------------------------------------------
# Main schema generation
# ---------------------------------------------------------------------------

def generate_schema(raw_text: str) -> dict:
    # configparser needs a section — the [newrelic] section maps to our root
    # Strip comment lines that start with ';' (INI style) for configparser
    cfg = configparser.RawConfigParser()
    cfg.read_string(raw_text)

    if not cfg.has_section("newrelic"):
        raise ValueError("No [newrelic] section found in newrelic.ini")

    comments = extract_comments(raw_text)
    items = {}
    for key, value in cfg.items("newrelic"):
        json_type, typed_val = infer_type_from_value(value)
        items[key] = (json_type, typed_val)

    properties = build_schema_tree(items, comments)

    # Overrides for known required fields
    properties["license_key"] = {
        "type": "string",
        "description": "New Relic license key. Binds agent data to your account.",
        "minLength": 1,
        "x-env-var": "NEW_RELIC_LICENSE_KEY",
    }
    properties.setdefault("app_name", {
        "type": "string",
        "description": "Name of the application as shown in the New Relic UI.",
        "default": "Python Application",
        "x-env-var": "NEW_RELIC_APP_NAME",
    })

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "New Relic Python Agent Configuration",
        "description": (
            "Fleet Control configuration schema for the New Relic Python agent. "
            "Generated from newrelic/newrelic.ini."
        ),
        "type": "object",
        "properties": properties,
        "required": ["license_key", "app_name"],
        "additionalProperties": True,
    }


# ---------------------------------------------------------------------------
# Diff / write helpers
# ---------------------------------------------------------------------------

def diff_schemas(old: dict, new: dict, path: str = "") -> list:
    changes = []
    old_props = old.get("properties", {})
    new_props = new.get("properties", {})
    for key in set(list(old_props) + list(new_props)):
        child = f"{path}.{key}" if path else key
        if key not in old_props:
            changes.append(f"  + added:   {child}")
        elif key not in new_props:
            changes.append(f"  - removed: {child}")
        elif old_props[key].get("type") == "object" and new_props[key].get("type") == "object":
            changes.extend(diff_schemas(old_props[key], new_props[key], child))
        elif old_props[key] != new_props[key]:
            changes.append(f"  ~ changed: {child}")
    return sorted(changes)


def load_existing(p: str) -> dict:
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def write_schema(schema: dict, p: str):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2)
        f.write("\n")


def main():
    raw_text   = load_newrelic_ini()
    new_schema = generate_schema(raw_text)
    old_k8s    = load_existing(K8S_SCHEMA)
    changes    = diff_schemas(old_k8s, new_schema)

    write_schema(new_schema, K8S_SCHEMA)
    print(f"Wrote:   {K8S_SCHEMA}")

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
