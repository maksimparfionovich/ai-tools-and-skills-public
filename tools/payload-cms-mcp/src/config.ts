/**
 * Runtime configuration for the Payload CMS MCP server.
 * Values come from environment variables (the MCP client's `env` block), so the same
 * server binary can serve any set of stands (qa1, staging, localhost, ...) just by changing env.
 *
 * Non-secret settings may also live in `config.local.json` next to the package
 * (or the file named by CMS_CONFIG_FILE): `{"CMS_ALLOW_WRITE_HOSTS": "...", ...}`.
 * They fill in only what env does not set, so one file serves every client config that
 * starts this server (Claude Code, Claude Desktop, a plugin). Credentials stay in env.
 */
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseHostList } from "./guard.js";

/** Login of one stand; a field left unset falls back to CMS_EMAIL / CMS_PASSWORD / CMS_TOTP_SECRET. */
export interface TargetCredentials {
  email?: string;
  password?: string;
  totpSecret?: string;
}

export interface CmsConfig {
  /** Default stand from CMS_BASE_URL, used when a call names no target; empty = target required. */
  baseUrl: string;
  email: string;
  password: string;
  totpSecret?: string;
  defaultLocale: string;
  defaultDepth: number;
  timeoutMs: number;
  payloadTypesPath?: string;
  /** Optional HTTP(S) proxy for outbound requests (corporate/QA networks). */
  proxyUrl?: string;
  /** Host patterns where writes are allowed besides localhost (see guard.ts). */
  allowWriteHosts: string[];
  /** Host patterns that are always read-only, e.g. prod and UAT (see guard.ts). */
  readonlyHosts: string[];
  /** Locales the CMS supports, from CMS_LOCALES; empty when not configured. */
  locales: string[];
  /** Slug of the auth collection (stock Payload: `users`) — for /api/<slug>/login and /me. */
  usersCollection: string;
  /** Explicit login endpoint; empty = try /api/auth/login, then /api/<usersCollection>/login. */
  loginPath?: string;
  /** Named stands from CMS_TARGETS: lowercase name -> base URL without trailing slash. */
  targets: Record<string, string>;
  /** Per-stand login overrides from env (CMS_EMAIL__QA1, CMS_PASSWORD__PROD, ...), by stand name. */
  targetCredentials: Record<string, TargetCredentials>;
  /** Settings file that was read, and which keys came from it (for the startup log). */
  settingsFile?: { path: string; keys: string[]; ignoredSecrets: string[] };
}

// Module lives in src/ (tsx) or dist/ (node), both one level under the package root.
export const LOCAL_CONFIG_PATH = resolve(dirname(fileURLToPath(import.meta.url)), "..", "config.local.json");

/** Keys that are never read from the settings file: credentials belong in the client's env. */
const SECRET_KEYS = ["CMS_EMAIL", "CMS_PASSWORD", "CMS_TOTP_SECRET"];

/** CMS_PASSWORD, and also its per-stand form CMS_PASSWORD__PROD. */
function isSecretKey(key: string): boolean {
  return SECRET_KEYS.some((s) => key === s || key.startsWith(`${s}__`));
}

/** Stand names: what a call passes as `target` and what CMS_EMAIL__<NAME> is built from. */
const TARGET_NAME = /^[a-z0-9][a-z0-9_-]*$/;

/** Env suffix of a stand name: `qa1` -> `QA1`, `prod-eu` -> `PROD_EU`. */
export function targetEnvSuffix(name: string): string {
  return name.toUpperCase().replace(/-/g, "_");
}

export interface LocalSettings {
  path: string;
  values: Record<string, string>;
  ignoredSecrets: string[];
}

/** Reads the optional settings file. Missing file — empty settings; broken file — an error. */
export function loadLocalSettings(path: string = LOCAL_CONFIG_PATH): LocalSettings {
  if (!existsSync(path)) return { path, values: {}, ignoredSecrets: [] };
  let data: unknown;
  try {
    data = JSON.parse(readFileSync(path, "utf8"));
  } catch (e: any) {
    throw new Error(`Cannot read settings file ${path}: ${e?.message ?? e}`);
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    throw new Error(`Settings file ${path} must be a JSON object of CMS_* keys`);
  }
  const values: Record<string, string> = {};
  const ignoredSecrets: string[] = [];
  for (const [k, v] of Object.entries(data as Record<string, unknown>)) {
    if (!k.startsWith("CMS_") || v === null || v === undefined) continue;
    if (isSecretKey(k)) {
      ignoredSecrets.push(k);
      continue;
    }
    if (Array.isArray(v)) values[k] = v.join(",");
    // An object (CMS_TARGETS) becomes the env form: name=url,name=url.
    else if (typeof v === "object") values[k] = Object.entries(v).map(([n, u]) => `${n}=${u}`).join(",");
    else values[k] = String(v);
  }
  return { path, values, ignoredSecrets };
}

function req(name: string, value: string | undefined): string {
  if (!value || !value.trim()) {
    throw new Error(
      `Missing required environment variable ${name}. ` +
        `Set it in your MCP server config (env) or a .env file.`,
    );
  }
  return value.trim();
}

/** Base URL as the client uses it: http(s) only, no trailing slash. Throws a readable error otherwise. */
export function normalizeBaseUrl(value: string, source: string): string {
  const v = value.trim().replace(/\/+$/, "");
  let url: URL;
  try {
    url = new URL(v);
  } catch {
    throw new Error(`${source}: "${value}" is not a URL (expected e.g. https://cms-qa1.example.com)`);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error(`${source}: "${value}" must start with http:// or https://`);
  }
  return v;
}

/** CMS_TARGETS: `qa1=https://cms-qa1.example.com,local=http://localhost:3000`. */
export function parseTargets(value: string | undefined): Record<string, string> {
  const targets: Record<string, string> = {};
  for (const item of (value ?? "").split(",").map((s) => s.trim()).filter(Boolean)) {
    const eq = item.indexOf("=");
    if (eq < 1) {
      throw new Error(`CMS_TARGETS: "${item}" must look like name=https://cms.example.com`);
    }
    const name = item.slice(0, eq).trim().toLowerCase();
    if (!TARGET_NAME.test(name)) {
      throw new Error(`CMS_TARGETS: stand name "${name}" may contain only latin letters, digits, "-" and "_"`);
    }
    if (targets[name]) throw new Error(`CMS_TARGETS: stand "${name}" is listed twice`);
    targets[name] = normalizeBaseUrl(item.slice(eq + 1), `CMS_TARGETS (${name})`);
  }
  return targets;
}

/** CMS_EMAIL__<NAME>, CMS_PASSWORD__<NAME>, CMS_TOTP_SECRET__<NAME> for every named stand. */
function readTargetCredentials(
  env: NodeJS.ProcessEnv,
  targets: Record<string, string>,
): Record<string, TargetCredentials> {
  const result: Record<string, TargetCredentials> = {};
  for (const name of Object.keys(targets)) {
    const suffix = targetEnvSuffix(name);
    const own: TargetCredentials = {
      email: env[`CMS_EMAIL__${suffix}`]?.trim() || undefined,
      password: env[`CMS_PASSWORD__${suffix}`]?.trim() || undefined,
      totpSecret: env[`CMS_TOTP_SECRET__${suffix}`]?.trim() || undefined,
    };
    if (own.email || own.password || own.totpSecret) result[name] = own;
  }
  return result;
}

export function loadConfig(
  processEnv: NodeJS.ProcessEnv = process.env,
  local: LocalSettings = loadLocalSettings(processEnv.CMS_CONFIG_FILE?.trim() || LOCAL_CONFIG_PATH),
): CmsConfig {
  // env wins; the settings file fills in only keys that env leaves unset or empty.
  const fromFile = Object.keys(local.values).filter((k) => !processEnv[k]?.trim());
  const env: NodeJS.ProcessEnv = { ...processEnv };
  for (const k of fromFile) env[k] = local.values[k];

  // No default stand unless CMS_BASE_URL is set: then every call must name its target.
  const baseUrl = env.CMS_BASE_URL?.trim() ? normalizeBaseUrl(env.CMS_BASE_URL, "CMS_BASE_URL") : "";
  const targets = parseTargets(env.CMS_TARGETS);
  return {
    baseUrl,
    email: req("CMS_EMAIL", env.CMS_EMAIL),
    password: req("CMS_PASSWORD", env.CMS_PASSWORD),
    totpSecret: env.CMS_TOTP_SECRET?.trim() || undefined,
    defaultLocale: (env.CMS_DEFAULT_LOCALE || "en").trim(),
    defaultDepth: Number.parseInt(env.CMS_DEFAULT_DEPTH || "1", 10) || 0,
    timeoutMs: Number.parseInt(env.CMS_TIMEOUT_MS || "30000", 10) || 30000,
    payloadTypesPath: env.CMS_PAYLOAD_TYPES_PATH?.trim() || undefined,
    proxyUrl:
      env.CMS_HTTPS_PROXY?.trim() ||
      env.HTTPS_PROXY?.trim() ||
      env.https_proxy?.trim() ||
      env.HTTP_PROXY?.trim() ||
      env.http_proxy?.trim() ||
      undefined,
    allowWriteHosts: parseHostList(env.CMS_ALLOW_WRITE_HOSTS),
    readonlyHosts: parseHostList(env.CMS_READONLY_HOSTS),
    locales: (env.CMS_LOCALES || "").split(",").map((s) => s.trim()).filter(Boolean),
    usersCollection: (env.CMS_USERS_COLLECTION || "users").trim().replace(/^\/+|\/+$/g, ""),
    loginPath: env.CMS_LOGIN_PATH?.trim() || undefined,
    targets,
    targetCredentials: readTargetCredentials(env, targets),
    settingsFile:
      fromFile.length || local.ignoredSecrets.length
        ? { path: local.path, keys: fromFile, ignoredSecrets: local.ignoredSecrets }
        : undefined,
  };
}
