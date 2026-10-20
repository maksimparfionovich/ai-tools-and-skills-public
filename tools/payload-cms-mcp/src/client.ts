/**
 * Thin, schema-agnostic client over the Payload CMS REST API.
 *
 * Design goal: survive CMS schema changes. The CRUD methods do NOT hardcode
 * collection fields — they pass `data` through as-is. High-level convenience
 * (defaults, lexical content, validation hints) lives in the MCP tool layer,
 * driven by a regenerable schema snapshot. So when the CMS changes, the raw
 * surface keeps working and only the optional hints need refreshing.
 */
import { ProxyAgent } from "undici";
import type { CmsConfig } from "./config.js";
import { checkWrite, type WriteCheck } from "./guard.js";
import { buildQueryString } from "./qs.js";
import { generateTotp } from "./totp.js";

interface RawOpts {
  body?: unknown;
  query?: Record<string, unknown>;
  bearer?: string;
  skipAuth?: boolean;
  formData?: FormData;
}

// Cookie-session auth (CMS with its own auth plugin): session + CSRF cookies instead of a JWT.
const SESSION_COOKIE = "__Host-cms_session";
const CSRF_COOKIE = "__Host-cms_csrf";
const SESSION_PATH = "/api/auth/session";
const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
// Fallback idle window when /api/auth/session is unavailable or malformed.
const FALLBACK_SESSION_SEC = 10 * 60;

export interface FindOptions {
  where?: Record<string, unknown>;
  locale?: string;
  depth?: number;
  limit?: number;
  page?: number;
  sort?: string;
}

export interface WriteOptions {
  locale?: string;
  depth?: number;
  /** draft=true skips required-field validation (Payload drafts). */
  draft?: boolean;
}

export class CmsError extends Error {
  status: number;
  body: unknown;
  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.name = "CmsError";
    this.status = status;
    this.body = body;
  }
}

export class PayloadClient {
  // Login mode is detected from the login response: "jwt" (stock Payload) or "cookie" (auth plugin).
  private mode: "jwt" | "cookie" | null = null;
  private token: string | null = null;
  private tokenExp = 0; // epoch seconds
  // Cookie mode: name -> value jar, filled from Set-Cookie of every response.
  private cookies = new Map<string, string>();
  private sessionIdleSec = 0; // idle window the server grants after each authenticated request
  private sessionAbsoluteExp = Infinity; // epoch seconds, hard session limit
  private sessionDeadline = 0; // epoch seconds, min(idle deadline, absolute limit)
  private loginInFlight: Promise<void> | null = null;
  private dispatcher?: ProxyAgent;

  constructor(private cfg: CmsConfig) {
    if (cfg.proxyUrl) this.dispatcher = new ProxyAgent(cfg.proxyUrl);
  }

  get baseUrl(): string {
    return this.cfg.baseUrl;
  }

  /**
   * Override the target stand (and optionally credentials) at runtime, for the
   * rest of this server process. Clears the cached token / session cookies so
   * the next call re-authenticates against the new base URL.
   */
  setTarget(opts: { baseUrl?: string; email?: string; password?: string }): {
    baseUrl: string;
    email: string;
  } {
    if (opts.baseUrl) this.cfg.baseUrl = opts.baseUrl.replace(/\/+$/, "");
    if (opts.email) this.cfg.email = opts.email;
    if (opts.password) this.cfg.password = opts.password;
    this.resetAuth();
    this.cookies.clear();
    return { baseUrl: this.cfg.baseUrl, email: this.cfg.email };
  }

  // ---------------------------------------------------------------- auth ----

  private async login(): Promise<void> {
    // Fresh start: drop any previous session so stale cookies can't be mistaken for new ones.
    this.resetAuth();
    this.cookies.clear();

    // CMS_LOGIN_PATH, or a custom auth endpoint (/api/auth/login, used by CMS with their own auth
    // plugin) with a fallback to stock Payload /api/<users>/login when the custom one is absent (404).
    const paths = this.cfg.loginPath
      ? [this.cfg.loginPath]
      : ["/api/auth/login", `/api/${this.cfg.usersCollection}/login`];
    let res: Record<string, any> = {};
    for (const [i, path] of paths.entries()) {
      try {
        res = await this.raw("POST", path, {
          body: { email: this.cfg.email, password: this.cfg.password },
          skipAuth: true,
        });
        break;
      } catch (e) {
        if (e instanceof CmsError && (e.status === 404 || e.status === 405) && i < paths.length - 1) continue;
        throw e;
      }
    }

    // Old CMS, no 2FA: standard auth response with a JWT in the body.
    if (res.token) {
      this.setToken(res.token as string, res.exp as number | undefined);
      return;
    }

    // 2FA required.
    if (res.requires2fa && res.pendingToken) {
      if (!this.cfg.totpSecret) {
        throw new Error(
          "Account requires 2FA but CMS_TOTP_SECRET is not set. " +
            "Use a test user with 2FA disabled, or provide CMS_TOTP_SECRET.",
        );
      }
      const code = generateTotp(this.cfg.totpSecret);
      const verify = await this.raw("POST", "/api/auth/2fa-verify", {
        body: { code },
        bearer: res.pendingToken as string,
        skipAuth: true,
      });
      if (verify.token) {
        this.setToken(verify.token as string, undefined);
        return;
      }
      // New CMS: no token in the body, the session arrives as cookies.
      if (this.cookies.has(SESSION_COOKIE)) {
        await this.startCookieSession();
        return;
      }
      throw new Error(
        `2FA verification returned neither a token nor a ${SESSION_COOKIE} cookie ` +
          "(if a proxy sits in between, check that it forwards Set-Cookie).",
      );
    }

    if (res.requiresSetup) {
      throw new Error(
        "Account needs 2FA setup before it can log in. Complete 2FA setup in the CMS admin first.",
      );
    }

    // New CMS, no 2FA: `{authenticated: true}` plus session/CSRF cookies.
    if (res.authenticated === true) {
      if (this.cookies.has(SESSION_COOKIE)) {
        await this.startCookieSession();
        return;
      }
      throw new Error(
        `Login returned authenticated:true but no ${SESSION_COOKIE} cookie was set ` +
          "(if a proxy sits in between, check that it forwards Set-Cookie).",
      );
    }

    throw new Error("Login failed: unexpected response shape.");
  }

  private setToken(token: string, exp?: number): void {
    this.mode = "jwt";
    this.token = token;
    // Payload tokens last 14 days; refresh a bit early. If exp missing, assume 12h.
    this.tokenExp = exp ?? Math.floor(Date.now() / 1000) + 12 * 3600;
  }

  /**
   * Cookie mode: ask the server how long the session lives. `GET /api/auth/session`
   * does not extend the session, so it is safe to call right after login.
   */
  private async startCookieSession(): Promise<void> {
    this.mode = "cookie";
    const nowMs = Date.now();
    // Start from the fallback; replaced below if the server tells us the real numbers.
    this.sessionIdleSec = FALLBACK_SESSION_SEC;
    this.sessionAbsoluteExp = Infinity;
    this.sessionDeadline = Math.floor(nowMs / 1000) + FALLBACK_SESSION_SEC;

    if (!this.cookies.has(CSRF_COOKIE)) {
      console.error(
        `[payload-cms-mcp] warning: login set no ${CSRF_COOKIE} cookie, write requests will be rejected`,
      );
    }

    try {
      const info = await this.raw("GET", SESSION_PATH);
      const idleMs = Date.parse(String(info.idleExpiresAt));
      const absMs = Date.parse(String(info.absoluteExpiresAt));
      if (!Number.isFinite(idleMs) || !Number.isFinite(absMs) || idleMs <= nowMs) {
        throw new Error(`unexpected response ${JSON.stringify(info)}`);
      }
      this.sessionIdleSec = Math.floor((idleMs - nowMs) / 1000);
      this.sessionAbsoluteExp = Math.floor(absMs / 1000);
      this.sessionDeadline = Math.min(Math.floor(idleMs / 1000), this.sessionAbsoluteExp);
    } catch (e: any) {
      // stderr only: stdout belongs to the MCP stdio transport.
      console.error(
        `[payload-cms-mcp] warning: could not read ${SESSION_PATH} (${e?.message ?? e}); ` +
          `assuming a ${FALLBACK_SESSION_SEC / 60}-minute session`,
      );
    }
  }

  /** Cookie mode: an authenticated request just succeeded, so the server pushed the idle deadline forward. */
  private slideSession(): void {
    const now = Math.floor(Date.now() / 1000);
    this.sessionDeadline = Math.min(now + this.sessionIdleSec, this.sessionAbsoluteExp);
  }

  /** Forget the login state (the cookie jar is cleared separately: on fresh login / target change). */
  private resetAuth(): void {
    this.mode = null;
    this.token = null;
    this.tokenExp = 0;
    this.sessionIdleSec = 0;
    this.sessionAbsoluteExp = Infinity;
    this.sessionDeadline = 0;
  }

  private async ensureAuth(): Promise<void> {
    const now = Math.floor(Date.now() / 1000);
    const fresh =
      this.mode === "cookie"
        ? this.cookies.has(SESSION_COOKIE) && now < this.sessionDeadline - 60
        : this.mode === "jwt" && !!this.token && now < this.tokenExp - 60;
    if (fresh) return;
    // One login at a time: parallel tool calls share it (also keeps the cookie jar consistent).
    this.loginInFlight ??= this.login().finally(() => {
      this.loginInFlight = null;
    });
    await this.loginInFlight;
  }

  /** Store cookies from the response's Set-Cookie headers (name=value only, attributes ignored). */
  private storeCookies(res: Response): void {
    const h = res.headers as Headers & { getSetCookie?: () => string[] };
    let lines: string[];
    if (typeof h.getSetCookie === "function") {
      lines = h.getSetCookie();
    } else {
      // Old runtimes: one comma-joined header. Split on commas that start a new `name=` pair,
      // not on the comma inside `Expires=Wed, 21 Oct ...`.
      const joined = res.headers.get("set-cookie");
      lines = joined ? joined.split(/,(?=\s*[^;,=\s]+=)/) : [];
    }
    for (const line of lines) {
      const [pair, ...attrs] = line.split(";");
      const eq = pair.indexOf("=");
      if (eq < 1) continue;
      const name = pair.slice(0, eq).trim();
      const value = pair.slice(eq + 1).trim();
      const maxAge = attrs.map((a) => /^\s*max-age\s*=\s*(-?\d+)/i.exec(a)).find((m) => m !== null);
      if (!value || (maxAge && Number(maxAge[1]) <= 0)) this.cookies.delete(name); // deletion
      else this.cookies.set(name, value);
    }
  }

  // ------------------------------------------------------------ low level ----

  private async raw(method: string, path: string, opts: RawOpts = {}): Promise<Record<string, any>> {
    if (!this.cfg.baseUrl) {
      throw new CmsError(
        "No target stand set. Call cms_set_target with the stand base URL first (there is no default CMS_BASE_URL).",
        0,
        null,
      );
    }
    const qs = opts.query ? buildQueryString(opts.query) : "";
    const url = `${this.cfg.baseUrl}${path}${qs ? `?${qs}` : ""}`;

    const headers: Record<string, string> = {};
    if (opts.bearer) {
      headers["Authorization"] = `Bearer ${opts.bearer}`;
    } else if (!opts.skipAuth) {
      if (this.mode === "cookie") {
        // Session cookie only (the server ignores a JWT header). Writes also need Origin,
        // Sec-Fetch-Site and x-csrf-token (= CSRF cookie value), else they count as anonymous.
        headers["Cookie"] = Array.from(this.cookies, ([k, v]) => `${k}=${v}`).join("; ");
        if (MUTATING_METHODS.has(method.toUpperCase())) {
          headers["Origin"] = new URL(this.cfg.baseUrl).origin;
          headers["Sec-Fetch-Site"] = "same-origin";
          const csrf = this.cookies.get(CSRF_COOKIE);
          if (csrf) headers["x-csrf-token"] = csrf;
        }
      } else if (this.token) {
        headers["Authorization"] = `JWT ${this.token}`;
      }
    }

    let body: string | FormData | undefined;
    if (opts.formData) {
      body = opts.formData; // runtime sets multipart boundary automatically
    } else if (opts.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.body);
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.cfg.timeoutMs);
    let res: Response;
    try {
      // `dispatcher` is an undici extension to fetch options (not in DOM types).
      const init = { method, headers, body, signal: controller.signal } as RequestInit & {
        dispatcher?: ProxyAgent;
      };
      if (this.dispatcher) init.dispatcher = this.dispatcher;
      res = await fetch(url, init);
    } catch (e: any) {
      throw new CmsError(`Network error calling ${method} ${path}: ${e?.message ?? e}`, 0, null);
    } finally {
      clearTimeout(timer);
    }

    this.storeCookies(res);

    const text = await res.text();
    let parsed: any = undefined;
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = { raw: text };
      }
    }

    if (!res.ok) {
      const msg =
        parsed?.errors?.[0]?.message ||
        parsed?.error ||
        parsed?.message ||
        `${res.status} ${res.statusText}`;
      throw new CmsError(`${method} ${path} failed: ${msg}`, res.status, parsed);
    }

    // Any authenticated request extends the idle window (except the session probe itself).
    if (this.mode === "cookie" && !opts.skipAuth && !opts.bearer && path !== SESSION_PATH) {
      this.slideSession();
    }

    return parsed ?? {};
  }

  /**
   * Authenticated request with one automatic re-login on 401. In cookie mode also on 403:
   * Payload answers an anonymous (expired-session) request to a closed collection with 403.
   */
  private async authed(method: string, path: string, opts: RawOpts = {}): Promise<Record<string, any>> {
    this.assertWritable(method, path);
    await this.ensureAuth();
    try {
      return await this.raw(method, path, opts);
    } catch (e) {
      if (e instanceof CmsError && (e.status === 401 || (this.mode === "cookie" && e.status === 403))) {
        this.resetAuth();
        await this.ensureAuth();
        return await this.raw(method, path, opts);
      }
      throw e;
    }
  }

  /** Write guard (guard.ts): refuse mutating requests to read-only hosts before any login. */
  private assertWritable(method: string, path: string): void {
    if (!MUTATING_METHODS.has(method.toUpperCase()) || !this.cfg.baseUrl) return;
    const check = this.writeStatus();
    if (!check.allowed) {
      throw new CmsError(`Write blocked: ${method.toUpperCase()} ${path}: ${check.reason}. Reads still work.`, 0, null);
    }
  }

  // --------------------------------------------------------------- public ----

  /** Whether the current target accepts writes, and why. */
  writeStatus(): WriteCheck {
    return checkWrite(this.cfg.baseUrl, { allow: this.cfg.allowWriteHosts, readonly: this.cfg.readonlyHosts });
  }

  /** Current authenticated user (also a handy connectivity check). */
  async me(): Promise<Record<string, any>> {
    return this.authed("GET", `/api/${this.cfg.usersCollection}/me`);
  }

  /** Raw escape hatch: call any REST path. Keeps working even if helpers drift. */
  async request(
    method: string,
    path: string,
    opts: { body?: unknown; query?: Record<string, unknown> } = {},
  ): Promise<Record<string, any>> {
    const p = path.startsWith("/") ? path : `/${path}`;
    return this.authed(method.toUpperCase(), p, opts);
  }

  async find(collection: string, opts: FindOptions = {}): Promise<Record<string, any>> {
    return this.authed("GET", `/api/${collection}`, {
      query: {
        where: opts.where,
        locale: opts.locale ?? this.cfg.defaultLocale,
        depth: opts.depth ?? this.cfg.defaultDepth,
        limit: opts.limit,
        page: opts.page,
        sort: opts.sort,
      },
    });
  }

  async findByID(
    collection: string,
    id: string | number,
    opts: { locale?: string; depth?: number; draft?: boolean } = {},
  ): Promise<Record<string, any>> {
    return this.authed("GET", `/api/${collection}/${id}`, {
      query: {
        locale: opts.locale ?? this.cfg.defaultLocale,
        depth: opts.depth ?? this.cfg.defaultDepth,
        draft: opts.draft,
      },
    });
  }

  async create(
    collection: string,
    data: Record<string, unknown>,
    opts: WriteOptions = {},
  ): Promise<Record<string, any>> {
    return this.authed("POST", `/api/${collection}`, {
      body: data,
      query: {
        locale: opts.locale ?? this.cfg.defaultLocale,
        depth: opts.depth ?? this.cfg.defaultDepth,
        draft: opts.draft,
      },
    });
  }

  async update(
    collection: string,
    id: string | number,
    data: Record<string, unknown>,
    opts: WriteOptions = {},
  ): Promise<Record<string, any>> {
    return this.authed("PATCH", `/api/${collection}/${id}`, {
      body: data,
      query: {
        locale: opts.locale ?? this.cfg.defaultLocale,
        depth: opts.depth ?? this.cfg.defaultDepth,
        draft: opts.draft,
      },
    });
  }

  async remove(collection: string, id: string | number): Promise<Record<string, any>> {
    return this.authed("DELETE", `/api/${collection}/${id}`, {});
  }

  /**
   * Upload a file to an upload-enabled collection (media, article-covers,
   * author-images, tag-images). `extraData` are sibling fields (e.g. alt).
   */
  async upload(
    collection: string,
    file: { buffer: Buffer; filename: string; mimetype: string },
    extraData: Record<string, unknown> = {},
    opts: { locale?: string } = {},
  ): Promise<Record<string, any>> {
    this.assertWritable("POST", `/api/${collection}`);
    await this.ensureAuth();
    const form = new FormData();
    const blob = new Blob([new Uint8Array(file.buffer)], { type: file.mimetype });
    form.append("file", blob, file.filename);
    if (Object.keys(extraData).length > 0) {
      form.append("_payload", JSON.stringify(extraData));
    }
    return this.authed("POST", `/api/${collection}`, {
      formData: form,
      query: { locale: opts.locale ?? this.cfg.defaultLocale },
    });
  }
}
