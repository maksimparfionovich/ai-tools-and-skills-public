/**
 * Stand registry: one PayloadClient per stand, created on first use and kept for the life of
 * the process, each with its own login. Every tool call names its stand (`target`), so there is
 * no shared "current stand" that another conversation served by the same process could switch.
 */
import { normalizeBaseUrl, targetEnvSuffix, type CmsConfig } from "./config.js";
import { PayloadClient } from "./client.js";
import { checkWrite, type WriteCheck } from "./guard.js";

export interface Target {
  /** Name from CMS_TARGETS, or host[:port] for a stand passed by URL. */
  name: string;
  baseUrl: string;
  /** Whether the stand is listed in CMS_TARGETS (only listed stands get CMS_EMAIL__<NAME> logins). */
  listed: boolean;
}

export interface TargetInfo {
  name: string;
  baseUrl: string;
  isDefault: boolean;
  writes: WriteCheck;
  /** Which login the stand uses: names of the env variables, never their values. */
  login: string;
  session: string;
}

/** Same stand, whatever the spelling: lowercase host, no default port, no trailing slash. */
function keyOf(baseUrl: string): string {
  const u = new URL(baseUrl);
  return `${u.origin}${u.pathname.replace(/\/+$/, "")}`;
}

/** How a stand is shown in tool answers: `qa2 (https://cms-qa2.example.com)`. */
export function targetLabel(t: Target): string {
  return t.listed ? `${t.name} (${t.baseUrl})` : t.baseUrl;
}

export class TargetRegistry {
  private readonly clients = new Map<string, PayloadClient>(); // by keyOf(baseUrl)

  constructor(private readonly cfg: CmsConfig) {}

  /** `target` as a tool call gives it: a CMS_TARGETS name, a base URL, or nothing (CMS_BASE_URL). */
  resolve(target?: string): Target {
    const t = target?.trim();
    if (!t) {
      if (this.cfg.baseUrl) return this.byUrl(this.cfg.baseUrl);
      throw new Error(`No target given: there is no default stand (CMS_BASE_URL). Pass \`target\`: ${this.hint()}.`);
    }
    const name = t.toLowerCase();
    if (this.cfg.targets[name]) return { name, baseUrl: this.cfg.targets[name], listed: true };
    if (/^https?:\/\//i.test(t)) return this.byUrl(normalizeBaseUrl(t, "target"));
    throw new Error(`Unknown target "${t}". Pass ${this.hint()}.`);
  }

  /** Client of the stand; the first call creates it, later calls reuse its login. */
  client(target?: string): { target: Target; client: PayloadClient } {
    const resolved = this.resolve(target);
    const key = keyOf(resolved.baseUrl);
    let client = this.clients.get(key);
    if (!client) {
      const own = resolved.listed ? this.cfg.targetCredentials[resolved.name] : undefined;
      client = new PayloadClient({
        ...this.cfg,
        baseUrl: resolved.baseUrl,
        email: own?.email ?? this.cfg.email,
        password: own?.password ?? this.cfg.password,
        totpSecret: own?.totpSecret ?? this.cfg.totpSecret,
      });
      this.clients.set(key, client);
    }
    return { target: resolved, client };
  }

  /** Listed stands plus stands used by URL in this process. No network. */
  list(): TargetInfo[] {
    const rows = Object.entries(this.cfg.targets).map(([name, baseUrl]) => this.info({ name, baseUrl, listed: true }));
    const seen = new Set(rows.map((r) => keyOf(r.baseUrl)));
    if (this.cfg.baseUrl && !seen.has(keyOf(this.cfg.baseUrl))) {
      rows.push(this.info(this.byUrl(this.cfg.baseUrl)));
      seen.add(keyOf(this.cfg.baseUrl));
    }
    for (const [key, client] of this.clients) {
      if (!seen.has(key)) rows.push(this.info(this.byUrl(client.baseUrl)));
    }
    return rows;
  }

  private info(t: Target): TargetInfo {
    const client = this.clients.get(keyOf(t.baseUrl));
    const own = t.listed ? this.cfg.targetCredentials[t.name] : undefined;
    const suffix = targetEnvSuffix(t.name);
    const ownVars = own
      ? [
          own.email && `CMS_EMAIL__${suffix}`,
          own.password && `CMS_PASSWORD__${suffix}`,
          own.totpSecret && `CMS_TOTP_SECRET__${suffix}`,
        ].filter(Boolean)
      : [];
    const session = client?.session;
    return {
      name: t.name,
      baseUrl: t.baseUrl,
      isDefault: !!this.cfg.baseUrl && keyOf(this.cfg.baseUrl) === keyOf(t.baseUrl),
      writes: checkWrite(t.baseUrl, { allow: this.cfg.allowWriteHosts, readonly: this.cfg.readonlyHosts }),
      login: ownVars.length
        ? `own: ${ownVars.join(", ")} (the rest from CMS_EMAIL / CMS_PASSWORD)`
        : "default: CMS_EMAIL / CMS_PASSWORD",
      session: !session ? "not used yet" : session.mode ? `logged in (${session.mode}) as ${session.email}` : "not logged in",
    };
  }

  /** A stand given by URL: the listed name if CMS_TARGETS has this URL, else the host. */
  private byUrl(baseUrl: string): Target {
    const key = keyOf(baseUrl);
    const listed = Object.entries(this.cfg.targets).find(([, url]) => keyOf(url) === key);
    if (listed) return { name: listed[0], baseUrl: listed[1], listed: true };
    return { name: new URL(baseUrl).host, baseUrl, listed: false };
  }

  private hint(): string {
    const names = Object.keys(this.cfg.targets);
    return names.length
      ? `a stand name (${names.join(", ")}) or a base URL such as http://localhost:3000`
      : "a base URL such as https://cms-qa1.example.com or http://localhost:3000 (stand names can be set in CMS_TARGETS)";
  }
}
