/**
 * Serialize a nested object into Payload/qs-style bracket query parameters.
 *
 *   { where: { title: { equals: "x" } }, locale: "en" }
 *   -> where[title][equals]=x&locale=en
 *
 *   { where: { and: [ { a: { equals: 1 } }, { b: { in: ["x","y"] } } ] } }
 *   -> where[and][0][a][equals]=1&where[and][1][b][in][0]=x&where[and][1][b][in][1]=y
 */
export function buildQueryString(params: Record<string, unknown>): string {
  const parts: string[] = [];

  const add = (key: string, value: unknown): void => {
    if (value === undefined || value === null) return;
    if (Array.isArray(value)) {
      value.forEach((v, i) => add(`${key}[${i}]`, v));
    } else if (typeof value === "object") {
      for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
        add(`${key}[${k}]`, v);
      }
    } else {
      parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`);
    }
  };

  for (const [k, v] of Object.entries(params)) add(k, v);
  return parts.join("&");
}
