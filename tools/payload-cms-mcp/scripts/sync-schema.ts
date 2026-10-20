/**
 * Regenerate the schema snapshot from the CMS repo's payload-types.ts.
 *
 * Usage:
 *   npm run sync-schema -- <path-to-payload-types.ts>
 *   # or set CMS_PAYLOAD_TYPES_PATH and run: npm run sync-schema
 */
import { writeSnapshot, SNAPSHOT_PATH } from "../src/schema.js";

const path = process.argv[2] || process.env.CMS_PAYLOAD_TYPES_PATH;

if (!path) {
  console.error(
    "Provide the path to payload-types.ts:\n" +
      "  npm run sync-schema -- /path/to/<cms-repo>/src/payload-types.ts\n" +
      "or set CMS_PAYLOAD_TYPES_PATH.",
  );
  process.exit(1);
}

const snap = writeSnapshot(path);
const collections = Object.keys(snap.collections);
console.log(`Snapshot written to ${SNAPSHOT_PATH}`);
console.log(`Source: ${snap.source}`);
console.log(`Collections (${collections.length}): ${collections.join(", ")}`);
