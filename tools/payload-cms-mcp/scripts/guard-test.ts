/**
 * Offline test of the write guard (src/guard.ts): no network, no CMS.
 *   npm test
 */
import { checkWrite, parseHostList } from "../src/guard.js";

// [baseUrl, CMS_ALLOW_WRITE_HOSTS, CMS_READONLY_HOSTS, writes allowed?]
const cases: Array<[string, string, string, boolean]> = [
  ["http://localhost:3000", "", "", true],
  ["http://127.0.0.1:3000", "", "", true],
  ["http://[::1]:3000", "", "", true],
  ["https://cms-qa1.example.com", "", "", false],
  ["https://cms-qa1.example.com", "cms-qa1.example.com", "", true],
  ["https://cms-qa2.example.com", "cms-qa*.example.com", "", true],
  ["https://cms-qa2.x.example.com", "cms-qa*.example.com", "", false],
  ["https://cms.example.com", "*.example.com", "", true],
  ["https://example.com", "*.example.com", "", true],
  ["https://a.b.example.com", "*.example.com", "", true],
  ["https://badexample.com", "*.example.com", "", false],
  ["https://cms.prod.example.com", "*.example.com", "*.prod.example.com", false],
  ["https://prod.example.com", "*.example.com", "prod.example.com", false],
  ["http://localhost:3000", "", "localhost", false],
  ["https://cms-qa1.example.com", "https://cms-qa1.example.com:8443/admin", "", true],
  ["https://cms-qa1.example.com", " CMS-QA1.EXAMPLE.COM ", "", true],
  ["not a url", "*", "", false],
  ["https://cmsxqa1.example.com", "cms.qa1.example.com", "", false],
];

let bad = 0;
for (const [url, allow, ro, expected] of cases) {
  const r = checkWrite(url, { allow: parseHostList(allow), readonly: parseHostList(ro) });
  const ok = r.allowed === expected;
  if (!ok) bad++;
  console.log(`${ok ? "ok  " : "FAIL"} ${url} allow=[${allow}] readonly=[${ro}] -> ${r.allowed} (${r.reason})`);
}
console.log(`\n${cases.length - bad}/${cases.length} ok`);
process.exit(bad ? 1 : 0);
