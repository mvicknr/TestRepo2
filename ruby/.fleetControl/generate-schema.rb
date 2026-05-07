#!/usr/bin/env ruby
# Fleet Control Config Schema Generator — Ruby Agent
#
# The Ruby newrelic.yml stores all optional config as commented-out dot-notation
# keys (e.g. "# application_logging.forwarding.enabled: true"). This script
# extracts those comment-style definitions and builds a nested JSON Schema.
#
# Exit codes: 0 = no changes, 1 = schema changed
# Dependencies: stdlib only (psych, json, net/http)

require 'json'
require 'net/http'
require 'psych'
require 'uri'

GITHUB_RAW_URL = 'https://raw.githubusercontent.com/newrelic/newrelic-ruby-agent/main/newrelic.yml'

SCRIPT_DIR  = File.dirname(File.expand_path(__FILE__))
SCHEMA_DIR  = File.join(SCRIPT_DIR, 'schemas')
K8S_SCHEMA  = File.join(SCHEMA_DIR, 'config-k8s.json')

# Enum overrides derived from comment text
ENUM_OVERRIDES = {
  'log_level'                              => %w[error warn info debug],
  'application_logging.forwarding.log_level' => %w[debug info warn error fatal unknown],
  'transaction_tracer.record_sql'          => %w[off raw obfuscated],
  'security.mode'                          => %w[IAST RASP],
}.freeze

EXCLUDE_PREFIXES = %w[
  audit_log
  instrumentation
  disable_
  heroku
  process_host
  marshaller
  preflight
  utilization
].freeze

# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
def load_newrelic_yml
  local = ENV['NEWRELIC_YML']
  if local
    puts "Reading local: #{local}"
    return File.read(local)
  end
  puts "Fetching from GitHub: #{GITHUB_RAW_URL}"
  Net::HTTP.get(URI(GITHUB_RAW_URL))
end

# ---------------------------------------------------------------------------
# Type inference from a YAML value string
# ---------------------------------------------------------------------------
def infer_type(value_str)
  return ['boolean', false] if value_str == 'false'
  return ['boolean', true]  if value_str == 'true'
  return ['array',   []]    if value_str =~ /\A\[/
  return ['object',  {}]    if value_str =~ /\A\{/
  return ['integer', value_str.to_i] if value_str =~ /\A-?\d+\z/
  return ['number',  value_str.to_f] if value_str =~ /\A-?\d+\.\d+\z/
  ['string', value_str.empty? ? nil : value_str]
end

# ---------------------------------------------------------------------------
# Extract all config keys from the common: section.
# Ruby's YAML has most options as commented dot-notation lines:
#   # application_logging.forwarding.enabled: true
# Real uncommented keys (license_key, app_name, log_level) are also captured.
# ---------------------------------------------------------------------------
def extract_all_keys(raw_text)
  in_common = false
  results   = {}      # dot_path => { type, default, description }
  pending   = []      # comment lines before the current key

  raw_text.each_line do |line|
    stripped = line.strip

    if stripped.start_with?('common:')
      in_common = true
      next
    end
    break if in_common && stripped =~ /\A(development|test|staging|production):/

    next unless in_common

    # Real uncommented key: "  key_name: value"
    real = stripped.match(/\A([a-z_][a-z0-9_]*)\s*:\s*(.*)\z/)
    if real && !stripped.start_with?('#')
      key  = real[1]
      val  = real[2].gsub(/['"]?<%=.*?%>['"]?/, 'placeholder').strip
      type, default_val = infer_type(val)
      results[key] = { 'type' => type, 'default' => default_val,
                       'description' => pending.join(' ') }
      pending = []
      next
    end

    # Commented-out config key: "# dot.notation.key: value"
    commented = stripped.match(/\A#\s+([a-z_][a-z0-9_.]*)\s*:\s*(.*)\z/)
    if commented
      key_path = commented[1]
      val_str  = commented[2].strip

      # Skip keys that look like prose ("# See: docs.newrelic.com") or excluded prefixes
      unless key_path.include?(' ') ||
             EXCLUDE_PREFIXES.any? { |p| key_path.start_with?(p) }
        type, default_val = infer_type(val_str)
        results[key_path] ||= { 'type' => type, 'default' => default_val,
                                 'description' => pending.join(' ') }
      end
      pending = []
      next
    end

    # Accumulate description from comment lines
    if stripped.start_with?('#')
      text = stripped.sub(/\A#+\s*/, '').strip
      pending << text unless text.empty? ||
                             text =~ /\AFor example/i ||
                             text =~ /\A-\s/  # bullet list items
    else
      pending = []
    end
  end

  results
end

# ---------------------------------------------------------------------------
# Build nested JSON Schema properties from flat dot-notation map
# ---------------------------------------------------------------------------
def set_nested(node, parts, info)
  key = parts[0]
  if parts.length == 1
    node[key] = info
  else
    # If this slot is already a leaf, don't overwrite
    node[key] = {} unless node[key].is_a?(Hash)
    set_nested(node[key], parts[1..], info) unless node[key].key?('type') && node[key]['type'] != 'object'
  end
end

def tree_to_schema(tree)
  props = {}
  tree.each do |key, val|
    next unless val.is_a?(Hash)

    if val.key?('type') && val['type'] != 'object'
      # Leaf property
      dot_path = key  # approximate — good enough for enum lookup
      if ENUM_OVERRIDES.key?(dot_path)
        prop = { 'type' => 'string', 'enum' => ENUM_OVERRIDES[dot_path] }
        prop['default'] = val['default'] if ENUM_OVERRIDES[dot_path].include?(val['default'])
      else
        prop = { 'type' => val['type'] }
        prop['default'] = val['default'] unless val['default'].nil? || val['default'] == ''
        prop['items']              = { 'type' => 'string' } if val['type'] == 'array'
        prop['additionalProperties'] = true                 if val['type'] == 'object'
      end
      prop['description'] = val['description'] if val['description'] && !val['description'].empty?
      props[key] = prop
    else
      # Nested section: val is a Hash of sub-keys
      nested = tree_to_schema(val)
      next if nested.empty?
      props[key] = {
        'type'                 => 'object',
        'properties'           => nested,
        'additionalProperties' => true,
      }
    end
  end
  props
end

def build_properties(flat_map)
  tree = {}
  flat_map.each do |dot_path, info|
    parts = dot_path.split('.')
    set_nested(tree, parts, info)
  end
  tree_to_schema(tree)
end

# ---------------------------------------------------------------------------
# Schema generation
# ---------------------------------------------------------------------------
def generate_schema(raw_text)
  flat   = extract_all_keys(raw_text)
  props  = build_properties(flat)

  props['license_key'] = {
    'type'        => 'string',
    'description' => 'New Relic license key. Binds agent data to your account.',
    'minLength'   => 1,
  }

  {
    '$schema'              => 'https://json-schema.org/draft/2020-12/schema',
    'title'                => 'New Relic Ruby Agent Configuration',
    'description'          => 'Fleet Control configuration schema for the New Relic Ruby agent. Generated from newrelic.yml.',
    'type'                 => 'object',
    'properties'           => props,
    'required'             => %w[license_key app_name],
    'additionalProperties' => true,
  }
end

# ---------------------------------------------------------------------------
# Diff / write helpers
# ---------------------------------------------------------------------------
def diff_schemas(old_s, new_s, path = '')
  changes = []
  old_p   = (old_s || {})['properties'] || {}
  new_p   = (new_s || {})['properties'] || {}
  (old_p.keys | new_p.keys).each do |key|
    child = path.empty? ? key : "#{path}.#{key}"
    if    !old_p.key?(key)                                             then changes << "  + added:   #{child}"
    elsif !new_p.key?(key)                                             then changes << "  - removed: #{child}"
    elsif old_p[key]['type'] == 'object' && new_p[key]['type'] == 'object'
          changes.concat diff_schemas(old_p[key], new_p[key], child)
    elsif old_p[key] != new_p[key]                                     then changes << "  ~ changed: #{child}"
    end
  end
  changes.sort
end

def load_existing(path)
  return {} unless File.exist?(path)
  JSON.parse(File.read(path))
rescue JSON::ParserError
  {}
end

require 'fileutils'
def write_schema(schema, path)
  FileUtils.mkdir_p(File.dirname(path))
  File.write(path, JSON.pretty_generate(schema) + "\n")
end

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
raw      = load_newrelic_yml
schema   = generate_schema(raw)
old_k8s  = load_existing(K8S_SCHEMA)
changes  = diff_schemas(old_k8s, schema)

write_schema(schema, K8S_SCHEMA)
puts "Wrote: #{K8S_SCHEMA}"

if old_k8s.empty?
  puts "\nFirst run — schema created."
  exit 0
elsif changes.any?
  puts "\nSchema changes (#{changes.size}):"
  changes.each { |c| puts c }
  exit 1
else
  puts "\nNo schema changes."
  exit 0
end
