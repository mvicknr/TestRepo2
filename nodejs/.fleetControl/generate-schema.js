#!/usr/bin/env node
/**
 * Fleet Control Config Schema Generator — Node.js Agent
 *
 * Fetches lib/config/default.js, stubs its internal require() dependencies,
 * calls definition() to get the full config structure, and generates
 * JSON Schema Draft 2020-12.
 *
 * Exit codes: 0 = no changes, 1 = schema changed
 * Dependencies: Node.js stdlib only
 */

'use strict';

const fs    = require('fs');
const https = require('https');
const path  = require('path');
const vm    = require('vm');

const GITHUB_RAW_URL =
  'https://raw.githubusercontent.com/newrelic/node-newrelic/main/lib/config/default.js';

const SCRIPT_DIR  = __dirname;
const SCHEMA_DIR  = path.join(SCRIPT_DIR, 'schemas');
const K8S_SCHEMA  = path.join(SCHEMA_DIR, 'config-k8s.json');

// Top-level keys to exclude (internal / runtime-only)
const EXCLUDE_KEYS = new Set([
  'newrelic_home', 'host', 'port', 'proxy_host', 'proxy_port', 'proxy_user',
  'proxy_pass', 'proxy_scheme', 'certificates', 'ignored_params',
  'ssl', 'apdex_t', 'capture_params', 'encoding_key', 'cross_application_tracer',
  'rum', 'browser_key', 'browser_monitoring_key',
  'debug', 'logger', 'config_path', 'config_file_path',
  'serverless_mode', 'feature_flag', 'utilization',
  'heroku', 'worker_threads', 'grpc',
]);

// Enum overrides
const ENUM_OVERRIDES = {
  'logging.level':                    ['fatal', 'error', 'warn', 'info', 'debug', 'trace'],
  'transaction_tracer.record_sql':    ['off', 'raw', 'obfuscated'],
  'transaction_tracer.obfuscated_sql_fields': null,  // skip
};

// ---------------------------------------------------------------------------
// Typed formatter stubs — we identify them by __schemaType later
// ---------------------------------------------------------------------------
function makeTypedStub(schemaType) {
  const fn = (v) => v;
  fn.__schemaType = schemaType;
  return fn;
}

const FORMATTER_STUBS = {
  array:      makeTypedStub('array'),
  int:        makeTypedStub('integer'),
  float:      makeTypedStub('number'),
  boolean:    makeTypedStub('boolean'),
  object:     makeTypedStub('object'),
  objectList: makeTypedStub('object'),
  allowList:  makeTypedStub('array'),
  regex:      makeTypedStub('string'),
};

// ---------------------------------------------------------------------------
// Fetch helpers
// ---------------------------------------------------------------------------
function fetchUrl(url) {
  return new Promise((resolve, reject) => {
    https.get(url, (res) => {
      if (res.statusCode === 301 || res.statusCode === 302) {
        return fetchUrl(res.headers.location).then(resolve).catch(reject);
      }
      const chunks = [];
      res.on('data', c => chunks.push(c));
      res.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
      res.on('error', reject);
    }).on('error', reject);
  });
}

function loadDefaultJs() {
  const local = process.env.NEWRELIC_JS;
  if (local) {
    console.log(`Reading local: ${local}`);
    return Promise.resolve(fs.readFileSync(local, 'utf8'));
  }
  console.log(`Fetching from GitHub: ${GITHUB_RAW_URL}`);
  return fetchUrl(GITHUB_RAW_URL);
}

// ---------------------------------------------------------------------------
// Run default.js in a vm sandbox with stubbed requires
// ---------------------------------------------------------------------------
function evalDefaultJs(src) {
  const mod = { exports: {} };

  const sandbox = {
    module:     mod,
    exports:    mod.exports,
    __dirname:  '/stub',
    __filename: '/stub/default.js',
    console,
    process:    { env: {}, version: process.version, platform: process.platform, cwd: () => '/stub' },
    require(id) {
      if (id === './formatters')                   return FORMATTER_STUBS;
      if (id === './build-instrumentation-config') return [];
      if (id === './samplers')                     return { config: [] };
      if (id === 'path')                           return require('path');
      if (id === 'os')                             return require('os');
      // Any other internal require → empty object
      return {};
    },
  };
  sandbox.exports = sandbox.module.exports;

  try {
    new vm.Script(src, { filename: 'default.js' }).runInNewContext(sandbox);
  } catch (e) {
    console.warn(`Warning: vm eval error: ${e.message}`);
  }

  return sandbox.module.exports;
}

// ---------------------------------------------------------------------------
// Extract JSDoc descriptions: maps key name → description text
// ---------------------------------------------------------------------------
function extractJsDocDescriptions(src) {
  const descriptions = {};
  // Match /** ... */ blocks followed by a property definition
  const docBlockRe = /\/\*\*([\s\S]*?)\*\/\s*([a-zA-Z_$][a-zA-Z0-9_$]*)\s*:/g;
  let m;
  while ((m = docBlockRe.exec(src)) !== null) {
    const comment = m[1].replace(/\n\s*\*\s?/g, ' ').trim();
    const key     = m[2];
    // Strip @param/@returns tags
    const desc = comment.replace(/@\w+[^\n]*/g, '').trim().replace(/\s+/g, ' ');
    if (desc) descriptions[key] = desc;
  }
  return descriptions;
}

// ---------------------------------------------------------------------------
// Infer schema type from a default value + optional formatter reference
// ---------------------------------------------------------------------------
function inferType(value, formatter) {
  if (formatter && formatter.__schemaType) return formatter.__schemaType;
  if (typeof value === 'boolean')           return 'boolean';
  if (Number.isInteger(value))              return 'integer';
  if (typeof value === 'number')            return 'number';
  if (Array.isArray(value))                 return 'array';
  if (value !== null && typeof value === 'object') return 'object';
  return 'string';
}

// ---------------------------------------------------------------------------
// Recursively build JSON Schema properties from the definition() result
// ---------------------------------------------------------------------------
function buildProperties(node, descriptions, keyPath) {
  const properties = {};

  for (const [key, entry] of Object.entries(node)) {
    const fullPath = keyPath ? `${keyPath}.${key}` : key;

    if (EXCLUDE_KEYS.has(key))                   continue;
    if (ENUM_OVERRIDES[fullPath] === null)        continue;

    const isLeaf = entry !== null &&
                   typeof entry === 'object' &&
                   !Array.isArray(entry) &&
                   'default' in entry;

    if (isLeaf) {
      // Leaf config entry: { default, formatter?, env? }
      const { default: def, formatter, env } = entry;
      const jsonType = ENUM_OVERRIDES[fullPath]
        ? 'string'
        : inferType(def, typeof formatter === 'function' ? formatter : null);

      let prop;
      if (ENUM_OVERRIDES[fullPath]) {
        prop = { type: 'string', enum: ENUM_OVERRIDES[fullPath] };
        if (ENUM_OVERRIDES[fullPath].includes(def)) prop.default = def;
      } else {
        prop = { type: jsonType };
        if (def !== null && def !== undefined && jsonType !== 'object') {
          if (Array.isArray(def) && def.length === 0) {
            prop.items = { type: 'string' };
          } else if (!Array.isArray(def)) {
            prop.default = def;
          }
        }
        if (jsonType === 'array')  prop.items              = prop.items || { type: 'string' };
        if (jsonType === 'object') prop.additionalProperties = true;
      }
      if (env)                    prop['x-env-var']  = env;
      if (descriptions[key])      prop.description   = descriptions[key];
      properties[key] = prop;

    } else if (entry !== null && typeof entry === 'object' && !Array.isArray(entry)) {
      // Nested config section
      const nested = buildProperties(entry, descriptions, fullPath);
      if (Object.keys(nested).length > 0) {
        properties[key] = {
          type: 'object',
          properties: nested,
          additionalProperties: true,
        };
      }
    } else {
      // Raw primitive default (e.g. license_key: '')
      const jsonType = inferType(entry, null);
      const prop = { type: jsonType };
      if (entry !== null && entry !== '' && entry !== undefined) prop.default = entry;
      if (descriptions[key]) prop.description = descriptions[key];
      properties[key] = prop;
    }
  }

  return properties;
}

// ---------------------------------------------------------------------------
// Diff helpers
// ---------------------------------------------------------------------------
function diffSchemas(oldS, newS, prefix) {
  const changes = [];
  const oldP = (oldS && oldS.properties) || {};
  const newP = (newS && newS.properties) || {};
  for (const key of new Set([...Object.keys(oldP), ...Object.keys(newP)])) {
    const child = prefix ? `${prefix}.${key}` : key;
    if      (!(key in oldP))                                           changes.push(`  + added:   ${child}`);
    else if (!(key in newP))                                           changes.push(`  - removed: ${child}`);
    else if (oldP[key].type === 'object' && newP[key].type === 'object') changes.push(...diffSchemas(oldP[key], newP[key], child));
    else if (JSON.stringify(oldP[key]) !== JSON.stringify(newP[key])) changes.push(`  ~ changed: ${child}`);
  }
  return changes.sort();
}

function loadExisting(p) {
  try { return JSON.parse(fs.readFileSync(p, 'utf8')); } catch { return {}; }
}

function writeSchema(schema, p) {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, JSON.stringify(schema, null, 2) + '\n');
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
loadDefaultJs().then((src) => {
  const exported     = evalDefaultJs(src);
  const descriptions = extractJsDocDescriptions(src);

  // node-newrelic exports defaultConfig.definition as a function
  const definitionFn = exported.definition;
  if (typeof definitionFn !== 'function') {
    console.error('ERROR: could not find definition() in default.js');
    process.exit(2);
  }

  const config     = definitionFn();
  const properties = buildProperties(config, descriptions, '');

  // Ensure license_key is properly typed (it's a raw '' in the source)
  properties['license_key'] = {
    type:           'string',
    description:    'New Relic license key. Binds agent data to your account.',
    minLength:      1,
    'x-env-var':    'NEW_RELIC_LICENSE_KEY',
  };

  const schema = {
    $schema:              'https://json-schema.org/draft/2020-12/schema',
    title:                'New Relic Node.js Agent Configuration',
    description:          'Fleet Control configuration schema for the New Relic Node.js agent. Generated from lib/config/default.js.',
    type:                 'object',
    properties,
    required:             ['license_key', 'app_name'],
    additionalProperties: true,
  };

  const oldK8s   = loadExisting(K8S_SCHEMA);
  const changes  = diffSchemas(oldK8s, schema, '');

  writeSchema(schema, K8S_SCHEMA);
  console.log(`Wrote: ${K8S_SCHEMA}`);

  if (!Object.keys(oldK8s).length) {
    console.log('\nFirst run — schema created.');
    process.exit(0);
  } else if (changes.length) {
    console.log(`\nSchema changes (${changes.length}):`);
    changes.forEach(c => console.log(c));
    process.exit(1);
  } else {
    console.log('\nNo schema changes.');
    process.exit(0);
  }
}).catch((err) => {
  console.error(`ERROR: ${err.message}`);
  process.exit(2);
});
