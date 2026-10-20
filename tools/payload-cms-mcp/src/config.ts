/**
 * Runtime configuration for the Payload CMS MCP server.
 * Values come from environment variables (the MCP client's `env` block), so the same
 * server binary can target any stand (qa1, staging, localhost, ...) just by changing env.
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

export interface CmsConfig {
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
  /** Settings file that was read, and which keys came from it (for the startup log). */
  settingsFile?: { path: string; keys: string[]; ignoredSecrets: string[] };
}

// Module lives in src/ (tsx) or dist/ (node), both one level under the package root.
export const LOCAL_CONFIG_PATH = resolve(dirname(fileURLToPath(import.meta.url)), "..", "config.local.json");

/** Keys that are never read from the settings file: credentials belong in the client's env. */
const SECRET_KEYS = new Set(["CMS_EMAIL", "CMS_PASSWORD", "CMS_TOTP_SECRET"]);

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
    if (SECRET_KEYS.has(k)) {
      ignoredSecrets.push(k);
      continue;
    }
    values[k] = Array.isArray(v) ? v.join(",") : String(v);
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

export function loadConfig(
  processEnv: NodeJS.ProcessEnv = process.env,
  local: LocalSettings = loadLocalSettings(processEnv.CMS_CONFIG_FILE?.trim() || LOCAL_CONFIG_PATH),
): CmsConfig {
  // env wins; the settings file fills in only keys that env leaves unset or empty.
  const fromFile = Object.keys(local.values).filter((k) => !processEnv[k]?.trim());
  const env: NodeJS.ProcessEnv = { ...processEnv };
  for (const k of fromFile) env[k] = local.values[k];

  // No default stand: base URL must be provided at runtime via cms_set_target.
  // CMS_BASE_URL is optional and, if present, only acts as a starting target.
  const baseUrl = (env.CMS_BASE_URL || "").trim().replace(/\/+$/, "");
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
    settingsFile:
      fromFile.length || local.ignoredSecrets.length
        ? { path: local.path, keys: fromFile, ignoredSecrets: local.ignoredSecrets }
        : undefined,
  };
}
