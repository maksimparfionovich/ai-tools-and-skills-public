/**
 * Fake Payload CMS for offline tests: one instance = one stand.
 *   - "jwt" mode: stock Payload, /api/auth/login answers 404, /api/users/login gives a JWT;
 *   - "cookie" mode: auth plugin, /api/auth/login sets session + CSRF cookies, /api/auth/session tells the lifetime.
 * Each stand accepts only its own password and its own token / cookie, and counts what reached it,
 * so a test can prove that two stands used side by side never see each other's credentials.
 */
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";

export interface FakeCmsOptions {
  /** Short name used in tokens, cookies and answers (`docs[].stand`). */
  name: string;
  mode: "jwt" | "cookie";
  email: string;
  password: string;
  /** "127.0.0.1" or "localhost": two hosts let a test give the stands different write rules. */
  host?: string;
  /** Delay of the login answer, so parallel calls really overlap. */
  loginDelayMs?: number;
}

export interface FakeCms {
  baseUrl: string;
  /** Every request as "METHOD /path". */
  hits: string[];
  /** Successful logins. */
  logins: number;
  /** Requests that carried a credential of another stand (token, cookie) or the wrong password. */
  foreign: string[];
  /** Tags created on this stand, by id. */
  tags: Map<number, string>;
  close(): void;
}

function send(res: ServerResponse, status: number, body: unknown, headers: Record<string, string | string[]> = {}): void {
  res.writeHead(status, { "Content-Type": "application/json", ...headers });
  res.end(JSON.stringify(body));
}

export async function startFakeCms(opts: FakeCmsOptions): Promise<FakeCms> {
  const token = `tok-${opts.name}`;
  const session = `sess-${opts.name}`;
  const csrf = `csrf-${opts.name}`;
  let nextId = 1;
  const state = { hits: [] as string[], logins: 0, foreign: [] as string[], tags: new Map<number, string>() };

  const server: Server = createServer((req: IncomingMessage, res: ServerResponse) => {
    const path = (req.url ?? "").split("?")[0];
    const line = `${req.method} ${path}`;
    state.hits.push(line);
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", async () => {
      const raw = Buffer.concat(chunks).toString("utf8");
      const body = raw && req.headers["content-type"]?.includes("json") ? JSON.parse(raw) : {};
      const auth = req.headers.authorization ?? "";
      const cookie = req.headers.cookie ?? "";
      // Any token or cookie that is not ours means another stand's login leaked here.
      if (auth.startsWith("JWT ") && auth !== `JWT ${token}`) state.foreign.push(`${line}: ${auth}`);
      if (cookie && !cookie.includes(session) && !cookie.includes(csrf)) state.foreign.push(`${line}: ${cookie}`);
      if (cookie.includes("sess-") && !cookie.includes(session)) state.foreign.push(`${line}: ${cookie}`);

      const loginOk = body.email === opts.email && body.password === opts.password;
      if (req.method === "POST" && path === "/api/auth/login") {
        if (opts.mode === "jwt") return send(res, 404, { errors: [{ message: "Not Found" }] });
        await new Promise((r) => setTimeout(r, opts.loginDelayMs ?? 0));
        if (!loginOk) {
          state.foreign.push(`${line}: wrong credentials`);
          return send(res, 401, { error: "Invalid credentials" });
        }
        state.logins++;
        return send(res, 200, { authenticated: true }, {
          "Set-Cookie": [
            `__Host-cms_session=${session}; Path=/; HttpOnly; Secure; SameSite=Lax`,
            `__Host-cms_csrf=${csrf}; Path=/; Secure; SameSite=Lax`,
          ],
        });
      }
      if (req.method === "POST" && path === "/api/users/login" && opts.mode === "jwt") {
        await new Promise((r) => setTimeout(r, opts.loginDelayMs ?? 0));
        if (!loginOk) {
          state.foreign.push(`${line}: wrong credentials`);
          return send(res, 401, { errors: [{ message: "Invalid credentials" }] });
        }
        state.logins++;
        return send(res, 200, { token, exp: Math.floor(Date.now() / 1000) + 3600, user: { email: opts.email } });
      }

      const authed = opts.mode === "jwt" ? auth === `JWT ${token}` : cookie.includes(`__Host-cms_session=${session}`);
      if (!authed) return send(res, 401, { errors: [{ message: "Unauthorized" }] });
      if (opts.mode === "cookie" && req.method !== "GET" && req.headers["x-csrf-token"] !== csrf) {
        return send(res, 403, { errors: [{ message: "CSRF" }] });
      }

      if (req.method === "GET" && path === "/api/auth/session") {
        const now = Date.now();
        return send(res, 200, {
          idleExpiresAt: new Date(now + 600_000).toISOString(),
          absoluteExpiresAt: new Date(now + 3_600_000).toISOString(),
        });
      }
      if (req.method === "GET" && path === "/api/users/me") {
        return send(res, 200, { user: { email: opts.email, stand: opts.name } });
      }
      if (req.method === "GET" && path === "/api/tags") {
        const docs = [...state.tags].map(([id, title]) => ({ id, title, stand: opts.name }));
        return send(res, 200, { docs, totalDocs: docs.length, stand: opts.name });
      }
      if (req.method === "POST" && path === "/api/tags") {
        const id = nextId++;
        state.tags.set(id, String(body.title));
        return send(res, 201, { doc: { id, title: body.title, stand: opts.name }, message: "Tag successfully created." });
      }
      const del = /^\/api\/tags\/(\d+)$/.exec(path);
      if (req.method === "DELETE" && del) {
        if (!state.tags.delete(Number(del[1]))) return send(res, 404, { errors: [{ message: "Not Found" }] });
        return send(res, 200, { doc: { id: Number(del[1]) }, message: "Deleted successfully." });
      }
      return send(res, 404, { errors: [{ message: "Not Found" }] });
    });
  });

  const host = opts.host ?? "127.0.0.1";
  await new Promise<void>((r) => server.listen(0, host, () => r()));
  const port = (server.address() as AddressInfo).port;
  return {
    baseUrl: `http://${host}:${port}`,
    get hits() {
      return state.hits;
    },
    get logins() {
      return state.logins;
    },
    get foreign() {
      return state.foreign;
    },
    tags: state.tags,
    close() {
      // No process.exit() in tests: on Windows exiting with live sockets crashes libuv.
      server.closeAllConnections();
      server.close();
    },
  };
}
