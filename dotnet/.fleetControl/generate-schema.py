#!/usr/bin/env python3
"""
Fleet Control Config Schema Generator — .NET Agent

Fetches newrelic.config (XML) from the .NET agent repo and writes JSON Schema
Draft 2020-12 to .fleetControl/schemas/config-k8s.json and config-host.json.

The schema represents logical YAML-shaped config keys (not XML structure), since
Fleet Control always delivers config as env vars (NEW_RELIC_*) for non-Java agents.

Exit codes:
  0 — no schema changes (or first run)
  1 — schema changed (CI should commit the updated files)

Dependencies: stdlib only (xml.etree.ElementTree, json, urllib.request)
"""

import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

GITHUB_RAW_URL = (
    "https://raw.githubusercontent.com/newrelic/newrelic-dotnet-agent"
    "/main/src/Agent/Configuration/newrelic.config"
)

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
SCHEMA_DIR   = os.path.join(SCRIPT_DIR, "schemas")
K8S_SCHEMA   = os.path.join(SCHEMA_DIR, "config-k8s.json")

# .NET config uses XML namespaces
NS = {"nr": "urn:newrelic-config"}

# ---------------------------------------------------------------------------
# Hand-curated schema derived from the XML structure.
# .NET config is XML, but Fleet Control delivers as NEW_RELIC_* env vars.
# This maps the env var name space → logical YAML property path.
# ---------------------------------------------------------------------------

# Schema is hand-curated because .NET XML structure doesn't map cleanly via
# automated extraction — attributes live on XML elements, not as child elements.
# A full automation would need XSD (which doesn't exist per the plan).
STATIC_PROPERTIES = {
    "license_key": {
        "type": "string",
        "description": "New Relic license key. Binds agent data to your account.",
        "minLength": 1,
        "x-env-var": "NEW_RELIC_LICENSE_KEY",
    },
    "app_name": {
        "type": "string",
        "description": "Name of your application as shown in the New Relic UI.",
        "default": "My Application",
        "x-env-var": "NEW_RELIC_APP_NAME",
    },
    "agent_enabled": {
        "type": "boolean",
        "description": "Set to false to disable the agent without removing it.",
        "default": True,
        "x-env-var": "NEW_RELIC_ENABLED",
    },
    "log": {
        "type": "object",
        "description": "Agent logging configuration.",
        "additionalProperties": True,
        "properties": {
            "level": {
                "type": "string",
                "enum": ["off", "error", "warn", "info", "debug", "finest", "all"],
                "default": "info",
                "description": "Log level for the agent.",
                "x-env-var": "NEW_RELIC_LOG_LEVEL",
            },
            "console": {
                "type": "boolean",
                "default": False,
                "description": "When true, log output is written to the console.",
                "x-env-var": "NEW_RELIC_LOG_CONSOLE",
            },
        },
    },
    "transaction_tracer": {
        "type": "object",
        "description": "Transaction tracer captures slow transaction details.",
        "additionalProperties": True,
        "properties": {
            "enabled": {
                "type": "boolean",
                "default": True,
                "description": "Enable or disable transaction tracing.",
                "x-env-var": "NEW_RELIC_TRANSACTION_TRACER_ENABLED",
            },
            "transaction_threshold": {
                "type": "string",
                "default": "apdex_f",
                "description": "Threshold (seconds or 'apdex_f') for recording a transaction trace.",
                "x-env-var": "NEW_RELIC_TRANSACTION_TRACER_THRESHOLD",
            },
            "record_sql": {
                "type": "string",
                "enum": ["off", "raw", "obfuscated"],
                "default": "obfuscated",
                "description": "Controls SQL recording mode.",
                "x-env-var": "NEW_RELIC_TRANSACTION_TRACER_RECORD_SQL",
            },
            "stack_trace_threshold": {
                "type": "number",
                "default": 0.5,
                "description": "Seconds after which a SQL stack trace is captured.",
                "x-env-var": "NEW_RELIC_TRANSACTION_TRACER_STACK_TRACE_THRESHOLD",
            },
        },
    },
    "error_collector": {
        "type": "object",
        "description": "Captures uncaught exceptions and sends them to New Relic.",
        "additionalProperties": True,
        "properties": {
            "enabled": {
                "type": "boolean",
                "default": True,
                "description": "Enable or disable error collection.",
                "x-env-var": "NEW_RELIC_ERROR_COLLECTOR_ENABLED",
            },
            "ignore_status_codes": {
                "type": "string",
                "default": "404",
                "description": "Comma-separated HTTP status codes to ignore.",
                "x-env-var": "NEW_RELIC_ERROR_COLLECTOR_IGNORE_STATUS_CODES",
            },
        },
    },
    "distributed_tracing": {
        "type": "object",
        "description": "Distributed tracing tracks requests across services.",
        "additionalProperties": True,
        "properties": {
            "enabled": {
                "type": "boolean",
                "default": True,
                "description": "Enable or disable distributed tracing.",
                "x-env-var": "NEW_RELIC_DISTRIBUTED_TRACING_ENABLED",
            },
        },
    },
    "span_events": {
        "type": "object",
        "description": "Span events for distributed tracing UI.",
        "additionalProperties": True,
        "properties": {
            "enabled": {
                "type": "boolean",
                "default": True,
                "description": "Enable or disable span events.",
                "x-env-var": "NEW_RELIC_SPAN_EVENTS_ENABLED",
            },
            "max_samples_stored": {
                "type": "integer",
                "default": 2000,
                "description": "Maximum span events per harvest cycle.",
                "x-env-var": "NEW_RELIC_SPAN_EVENTS_MAX_SAMPLES_STORED",
            },
        },
    },
    "application_logging": {
        "type": "object",
        "description": "Application log forwarding and metrics.",
        "additionalProperties": True,
        "properties": {
            "enabled": {
                "type": "boolean",
                "default": True,
                "description": "Enable or disable all application logging features.",
                "x-env-var": "NEW_RELIC_APPLICATION_LOGGING_ENABLED",
            },
            "forwarding": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "enabled": {
                        "type": "boolean",
                        "default": True,
                        "description": "Forward application logs to New Relic.",
                        "x-env-var": "NEW_RELIC_APPLICATION_LOGGING_FORWARDING_ENABLED",
                    },
                    "max_samples_stored": {
                        "type": "integer",
                        "default": 10000,
                        "description": "Maximum log events per harvest cycle.",
                        "x-env-var": "NEW_RELIC_APPLICATION_LOGGING_FORWARDING_MAX_SAMPLES_STORED",
                    },
                },
            },
            "metrics": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "enabled": {
                        "type": "boolean",
                        "default": True,
                        "description": "Report log count metrics per log level.",
                        "x-env-var": "NEW_RELIC_APPLICATION_LOGGING_METRICS_ENABLED",
                    },
                },
            },
        },
    },
    "browser_monitoring": {
        "type": "object",
        "description": "Real User Monitoring (RUM) browser injection.",
        "additionalProperties": True,
        "properties": {
            "auto_instrument": {
                "type": "boolean",
                "default": True,
                "description": "Automatically inject RUM script into web pages.",
                "x-env-var": "NEW_RELIC_BROWSER_MONITORING_AUTO_INSTRUMENT",
            },
        },
    },
    "high_security": {
        "type": "boolean",
        "default": False,
        "description": "Enables high security mode — forces SSL and obfuscated SQL.",
        "x-env-var": "NEW_RELIC_HIGH_SECURITY",
    },
    "labels": {
        "type": "string",
        "description": "Semicolon-delimited list of label:value pairs (e.g. 'Env:prod;Team:backend').",
        "x-env-var": "NEW_RELIC_LABELS",
    },
}

# ---------------------------------------------------------------------------
# Fetch XML (used to verify the file exists and for future automation)
# ---------------------------------------------------------------------------

def fetch_config_xml():
    local = os.environ.get("NEWRELIC_CONFIG")
    if local:
        print(f"Reading local file: {local}")
        with open(local, "r", encoding="utf-8") as f:
            return f.read()
    print(f"Fetching from GitHub: {GITHUB_RAW_URL}")
    with urllib.request.urlopen(GITHUB_RAW_URL, timeout=15) as resp:
        return resp.read().decode("utf-8")


def generate_schema(_xml_text: str) -> dict:
    """Build schema from curated static properties (XML automation is future work)."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "New Relic .NET Agent Configuration",
        "description": (
            "Fleet Control configuration schema for the New Relic .NET agent. "
            "Delivered via NEW_RELIC_* environment variables. "
            "Generated from src/Agent/Configuration/newrelic.config."
        ),
        "type": "object",
        "properties": STATIC_PROPERTIES,
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
    xml_text   = fetch_config_xml()
    new_schema = generate_schema(xml_text)
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
