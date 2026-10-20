/**
 * Write guard. Mutating requests (POST/PUT/PATCH/DELETE, uploads) are checked
 * against two host lists before any login, so a blocked request never leaves
 * the machine:
 *   - CMS_READONLY_HOSTS — always read-only, nothing unlocks them (prod, UAT,
 *     anything running on the prod database). Wins over everything else.
 *   - localhost / 127.0.0.1 / ::1 — writable.
 *   - CMS_ALLOW_WRITE_HOSTS — writable (your dev / qa stands).
 *   - any other host — read-only.
 *
 * Pattern syntax (both lists, comma-separated, case-insensitive):
 *   cms-qa1.example.com   exact host (a full URL works too: the hostname is taken)
 *   cms-qa*.example.com   `*` matches any characters except a dot
 *   *.example.com         the domain itself and every subdomain at any depth
 */

const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);

export interface WriteCheck {
  allowed: boolean;
  reason: string;
}

export interface WriteRules {
  allow: readonly string[];
  readonly: readonly string[];
}

/** Whether `host` matches one pattern (see the syntax above). */
export function hostMatches(host: string, pattern: string): boolean {
  const h = host.toLowerCase();
  const p = pattern.toLowerCase();
  if (p.startsWith("*.")) {
    const domain = p.slice(2);
    if (!domain.includes("*")) return h === domain || h.endsWith(`.${domain}`);
  }
  const re = new RegExp(
    "^" + p.split("*").map((part) => part.replace(/[.+?^${}()|[\]\\]/g, "\\$&")).join("[^.]*") + "$",
  );
  return re.test(h);
}

export function checkWrite(baseUrl: string, rules: WriteRules): WriteCheck {
  let host: string;
  try {
    host = new URL(baseUrl).hostname.toLowerCase();
  } catch {
    return { allowed: false, reason: `invalid base URL "${baseUrl}"` };
  }
  const ro = rules.readonly.find((p) => hostMatches(host, p));
  if (ro) return { allowed: false, reason: `${host} is read-only (CMS_READONLY_HOSTS: ${ro})` };
  if (LOCAL_HOSTS.has(host)) return { allowed: true, reason: "localhost" };
  const ok = rules.allow.find((p) => hostMatches(host, p));
  if (ok) return { allowed: true, reason: `listed in CMS_ALLOW_WRITE_HOSTS (${ok})` };
  return {
    allowed: false,
    reason: `${host} is not in CMS_ALLOW_WRITE_HOSTS; add it there to allow writes`,
  };
}

/** Comma-separated hosts, URLs or patterns -> lowercase host patterns without scheme, port or path. */
export function parseHostList(value: string | undefined): string[] {
  return (value ?? "")
    .split(",")
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean)
    .map((s) => {
      const hostPort = s.replace(/^[a-z][a-z0-9+.-]*:\/\//, "").replace(/[/?#].*$/, "");
      const m = /^(\[[^\]]*\]|[^:]+)(?::\d+)?$/.exec(hostPort); // host[:port], [ipv6][:port]
      return m ? m[1] : hostPort;
    });
}
