/** Small shared helpers for the MCP tool layer. */

/** CMS slug rule: lowercase latin letters, digits, single dashes. */
export function slugify(input: string): string {
  const s = input
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "") // strip diacritics
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .replace(/-{2,}/g, "-");
  return s;
}

export function ensureSlug(explicit: string | undefined, from: string): string {
  if (explicit && explicit.trim()) return explicit.trim();
  const s = slugify(from);
  return s || `item-${Date.now().toString(36)}`;
}

const MIME: Record<string, string> = {
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".png": "image/png",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".avif": "image/avif",
  ".svg": "image/svg+xml",
  ".pdf": "application/pdf",
  ".mp4": "video/mp4",
  ".webm": "video/webm",
};

export function mimeFromFilename(name: string): string {
  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot).toLowerCase() : "";
  return MIME[ext] || "application/octet-stream";
}

/** Accept an object or a JSON string; return a plain object (or {}). */
export function coerceObject(input: unknown): Record<string, unknown> {
  if (input == null) return {};
  if (typeof input === "string") {
    const t = input.trim();
    if (!t) return {};
    try {
      const parsed = JSON.parse(t);
      return typeof parsed === "object" && parsed ? (parsed as Record<string, unknown>) : {};
    } catch {
      return {};
    }
  }
  if (typeof input === "object") return input as Record<string, unknown>;
  return {};
}

/** Drop undefined values so we never send empty keys to the API. */
export function clean<T extends Record<string, unknown>>(obj: T): T {
  for (const k of Object.keys(obj)) {
    if (obj[k] === undefined) delete obj[k];
  }
  return obj;
}
