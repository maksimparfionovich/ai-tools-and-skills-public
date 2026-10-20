#!/usr/bin/env node
/**
 * Payload CMS MCP server.
 *
 * Exposes tools that let Claude Code / Claude Cowork create and edit CMS
 * entities (articles, authors, tags, categories, media) via the Payload REST
 * API. Connects over stdio.
 */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { readFileSync } from "node:fs";
import { basename } from "node:path";

import { loadConfig } from "./config.js";
import { PayloadClient, CmsError } from "./client.js";
import { toLexical } from "./lexical.js";
import { doctor } from "./schema.js";
import { coerceObject, clean, ensureSlug, mimeFromFilename } from "./util.js";

let cfg: ReturnType<typeof loadConfig>;
try {
  cfg = loadConfig();
} catch (e: any) {
  // stderr only: stdout belongs to the MCP stdio transport.
  console.error(`[payload-cms-mcp] ${e?.message ?? e}`);
  process.exit(1);
}
const client = new PayloadClient(cfg);

const VERSION = "0.3.1";

const server = new McpServer({
  name: "payload-cms-mcp",
  version: VERSION,
});

// ---------------------------------------------------------------- helpers ----

type ToolResult = { content: Array<{ type: "text"; text: string }>; isError?: boolean };

function ok(data: unknown): ToolResult {
  const text = typeof data === "string" ? data : JSON.stringify(data, null, 2);
  return { content: [{ type: "text", text }] };
}

function fail(e: unknown): ToolResult {
  let msg: string;
  if (e instanceof CmsError) {
    msg = `CMS error (${e.status}): ${e.message}`;
    if (e.body) msg += `\nDetails: ${JSON.stringify(e.body)}`;
  } else if (e instanceof Error) {
    msg = e.message;
  } else {
    msg = String(e);
  }
  return { content: [{ type: "text", text: msg }], isError: true };
}

/** For an article: returns { data, opts } given common inputs. */
function buildArticleData(input: {
  title?: string;
  dek?: string;
  content?: string;
  contentMode?: "markdown" | "plain";
  status?: string;
  author?: string | number;
  editor?: string | number;
  category?: string | number;
  subCategory?: string | number;
  topic?: string | number;
  tags?: Array<string | number>;
  slug?: string;
  metaTitle?: string;
  metaDescription?: string;
  extra?: unknown;
}): Record<string, unknown> {
  const data: Record<string, unknown> = clean({
    title: input.title,
    dek: input.dek,
    status: input.status,
    author: input.author,
    editor: input.editor,
    category: input.category,
    subCategory: input.subCategory,
    topic: input.topic,
    tags: input.tags,
    slug: input.slug,
    metaTitle: input.metaTitle,
    metaDescription: input.metaDescription,
  });
  if (input.content !== undefined) {
    data.text = toLexical(input.content, input.contentMode ?? "markdown");
  }
  return { ...data, ...coerceObject(input.extra) };
}

// ------------------------------------------------------------------ tools ----

server.registerTool(
  "cms_whoami",
  {
    title: "Who am I",
    description:
      "Verify connectivity and return the target stand, whether it accepts writes (only localhost and hosts in CMS_ALLOW_WRITE_HOSTS do; hosts in CMS_READONLY_HOSTS such as prod never do), and the authenticated CMS user (id, email, roles, languageAccess). Use this first, and again before any write: the target resets when the server restarts.",
    inputSchema: {},
  },
  async (): Promise<ToolResult> => {
    try {
      const me = await client.me();
      return ok({
        version: VERSION,
        baseUrl: client.baseUrl,
        writes: client.writeStatus(),
        settingsFile: cfg.settingsFile ?? null,
        user: me?.user ?? me,
      });
    } catch (e) {
      return fail(e);
    }
  },
);

server.registerTool(
  "cms_list_locales",
  {
    title: "List locales",
    description: "List the locales configured for this CMS (CMS_LOCALES) and the default locale (CMS_DEFAULT_LOCALE).",
    inputSchema: {},
  },
  async (): Promise<ToolResult> =>
    ok({
      defaultLocale: cfg.defaultLocale,
      locales: cfg.locales.length ? cfg.locales : [cfg.defaultLocale],
      ...(cfg.locales.length ? {} : { note: "CMS_LOCALES is not set; only the default locale is known." }),
    }),
);

server.registerTool(
  "cms_set_target",
  {
    title: "Set target stand (base URL)",
    description:
      "Switch the CMS base URL (and optionally credentials) for the rest of this session, overriding the default CMS_BASE_URL from env. All subsequent cms_* calls hit this URL. The override is in-memory and resets when the server restarts. Verifies by logging in and returns the authenticated user and whether the target accepts writes (only localhost and hosts in CMS_ALLOW_WRITE_HOSTS do; hosts in CMS_READONLY_HOSTS such as prod never do). Example baseUrl: https://cms-qa1.example.com or http://localhost:3000.",
    inputSchema: {
      baseUrl: z.string().describe("Target stand base URL (no trailing slash needed)."),
      email: z.string().optional().describe("Override login email if the stand uses different credentials."),
      password: z.string().optional().describe("Override password if needed."),
    },
  },
  async (args): Promise<ToolResult> => {
    try {
      const t = client.setTarget(args);
      const me = await client.me();
      return ok({
        target: t.baseUrl,
        writes: client.writeStatus(),
        loggedInAs: (me?.user ?? me)?.email,
        note: "Active for this session until the server restarts.",
      });
    } catch (e) {
      return fail(e);
    }
  },
);

server.registerTool(
  "cms_find",
  {
    title: "Find documents",
    description:
      "Query any collection (articles, authors, tags, categories, media, ...). `where` uses Payload query syntax, e.g. {\"title\":{\"contains\":\"btc\"}} or {\"and\":[...]}. Returns paginated docs.",
    inputSchema: {
      collection: z.string().describe("Collection slug, e.g. 'articles', 'authors', 'tags', 'categories'."),
      where: z.union([z.record(z.any()), z.string()]).optional().describe("Payload where filter (object or JSON string)."),
      locale: z.string().optional(),
      depth: z.number().int().min(0).max(5).optional(),
      limit: z.number().int().min(1).max(200).optional(),
      page: z.number().int().min(1).optional(),
      sort: z.string().optional().describe("Field to sort by, prefix with '-' for descending."),
    },
  },
  async (args): Promise<ToolResult> => {
    try {
      const res = await client.find(args.collection, {
        where: args.where ? coerceObject(args.where) : undefined,
        locale: args.locale,
        depth: args.depth,
        limit: args.limit,
        page: args.page,
        sort: args.sort,
      });
      return ok(res);
    } catch (e) {
      return fail(e);
    }
  },
);

server.registerTool(
  "cms_get",
  {
    title: "Get document by ID",
    description: "Fetch a single document by collection slug and id.",
    inputSchema: {
      collection: z.string(),
      id: z.union([z.string(), z.number()]),
      locale: z.string().optional(),
      depth: z.number().int().min(0).max(5).optional(),
      draft: z.boolean().optional().describe("Return the draft version if available."),
    },
  },
  async (args): Promise<ToolResult> => {
    try {
      return ok(await client.findByID(args.collection, args.id, args));
    } catch (e) {
      return fail(e);
    }
  },
);

server.registerTool(
  "cms_create",
  {
    title: "Create document (generic)",
    description:
      "Create a document in any collection by passing raw `data`. Schema-agnostic: keeps working even if fields change. For articles/authors/tags/categories prefer the dedicated tools.",
    inputSchema: {
      collection: z.string(),
      data: z.union([z.record(z.any()), z.string()]).describe("Field values (object or JSON string)."),
      locale: z.string().optional(),
      draft: z.boolean().optional().describe("Create as draft (skips required-field validation)."),
      depth: z.number().int().min(0).max(5).optional(),
    },
  },
  async (args): Promise<ToolResult> => {
    try {
      return ok(await client.create(args.collection, coerceObject(args.data), args));
    } catch (e) {
      return fail(e);
    }
  },
);

server.registerTool(
  "cms_update",
  {
    title: "Update document (generic)",
    description: "Update a document in any collection by id with raw `data` (partial update).",
    inputSchema: {
      collection: z.string(),
      id: z.union([z.string(), z.number()]),
      data: z.union([z.record(z.any()), z.string()]),
      locale: z.string().optional(),
      draft: z.boolean().optional(),
      depth: z.number().int().min(0).max(5).optional(),
    },
  },
  async (args): Promise<ToolResult> => {
    try {
      return ok(await client.update(args.collection, args.id, coerceObject(args.data), args));
    } catch (e) {
      return fail(e);
    }
  },
);

server.registerTool(
  "cms_delete",
  {
    title: "Delete document",
    description: "Delete a document by collection slug and id.",
    inputSchema: {
      collection: z.string(),
      id: z.union([z.string(), z.number()]),
    },
  },
  async (args): Promise<ToolResult> => {
    try {
      return ok(await client.remove(args.collection, args.id));
    } catch (e) {
      return fail(e);
    }
  },
);

server.registerTool(
  "cms_create_article",
  {
    title: "Create article",
    description:
      "Create an article. With no status it is created with the CMS default editorial status 'new', so minimal input works — title alone is enough. `content` is Markdown by default and is converted to the CMS rich-text format. Most fields (author, category, slug, cover, text, ...) are validated by the CMS only when status='published'. Relationship fields (author/category/topic/tags) take document IDs. Use `extra` for any field not listed.",
    inputSchema: {
      title: z.string().optional(),
      locale: z.string().optional().describe("Defaults to CMS_DEFAULT_LOCALE (en)."),
      status: z
        .enum(["new", "in_progress", "ready_to_review", "published", "unpublished"])
        .optional()
        .describe("Editorial status. Omit to use the CMS default 'new'. Required fields are enforced only when 'published'."),
      content: z.string().optional().describe("Article body as Markdown (default) or plain text."),
      contentMode: z.enum(["markdown", "plain"]).optional(),
      dek: z.string().optional().describe("Subtitle / standfirst."),
      author: z.union([z.string(), z.number()]).optional().describe("Author document id."),
      editor: z.union([z.string(), z.number()]).optional(),
      category: z.union([z.string(), z.number()]).optional().describe("Category document id."),
      subCategory: z.union([z.string(), z.number()]).optional(),
      topic: z.union([z.string(), z.number()]).optional().describe("Topic (tag) document id."),
      tags: z.array(z.union([z.string(), z.number()])).optional().describe("Tag document ids."),
      slug: z.string().optional().describe("URL slug (lowercase latin/digits/dashes). Auto-derived by the CMS if omitted."),
      metaTitle: z.string().optional(),
      metaDescription: z.string().optional(),
      extra: z.union([z.record(z.any()), z.string()]).optional().describe("Any additional article fields."),
      draft: z.boolean().optional().describe("Payload version-draft flag (only if the collection uses drafts). Usually leave unset."),
    },
  },
  async (args): Promise<ToolResult> => {
    try {
      const data = buildArticleData(args);
      const res = await client.create("articles", data, { locale: args.locale, draft: args.draft });
      return ok(res);
    } catch (e) {
      return fail(e);
    }
  },
);

server.registerTool(
  "cms_update_article",
  {
    title: "Update article",
    description:
      "Update an existing article by id. Only provided fields are changed. `content` (Markdown) replaces the body. Set status='published' to publish (CMS then validates required fields).",
    inputSchema: {
      id: z.union([z.string(), z.number()]),
      locale: z.string().optional(),
      status: z
        .enum(["new", "in_progress", "ready_to_review", "published", "unpublished"])
        .optional(),
      title: z.string().optional(),
      content: z.string().optional(),
      contentMode: z.enum(["markdown", "plain"]).optional(),
      dek: z.string().optional(),
      author: z.union([z.string(), z.number()]).optional(),
      editor: z.union([z.string(), z.number()]).optional(),
      category: z.union([z.string(), z.number()]).optional(),
      subCategory: z.union([z.string(), z.number()]).optional(),
      topic: z.union([z.string(), z.number()]).optional(),
      tags: z.array(z.union([z.string(), z.number()])).optional(),
      slug: z.string().optional(),
      metaTitle: z.string().optional(),
      metaDescription: z.string().optional(),
      extra: z.union([z.record(z.any()), z.string()]).optional(),
      draft: z.boolean().optional().describe("Payload version-draft flag. Usually leave unset."),
    },
  },
  async (args): Promise<ToolResult> => {
    try {
      const data = buildArticleData(args);
      const res = await client.update("articles", args.id, data, { locale: args.locale, draft: args.draft });
      return ok(res);
    } catch (e) {
      return fail(e);
    }
  },
);

server.registerTool(
  "cms_create_author",
  {
    title: "Create author",
    description: "Create an author. `name` is required; `slug` is auto-derived from the name if omitted.",
    inputSchema: {
      name: z.string(),
      slug: z.string().optional(),
      locale: z.string().optional(),
      position: z.string().optional(),
      description: z.string().optional(),
      email: z.string().optional(),
      isPublished: z.boolean().optional(),
      isEditor: z.boolean().optional(),
      extra: z.union([z.record(z.any()), z.string()]).optional(),
    },
  },
  async (args): Promise<ToolResult> => {
    try {
      const data = clean({
        name: args.name,
        slug: ensureSlug(args.slug, args.name),
        position: args.position,
        description: args.description,
        email: args.email,
        isPublished: args.isPublished,
        isEditor: args.isEditor,
        ...coerceObject(args.extra),
      });
      return ok(await client.create("authors", data, { locale: args.locale }));
    } catch (e) {
      return fail(e);
    }
  },
);

server.registerTool(
  "cms_create_tag",
  {
    title: "Create tag",
    description: "Create a tag. `title` is required; `slug` is auto-derived if omitted. Set isTopic=true for a topic.",
    inputSchema: {
      title: z.string(),
      slug: z.string().optional(),
      locale: z.string().optional(),
      isTopic: z.boolean().optional(),
      isPublished: z.boolean().optional(),
      description: z.string().optional(),
      extra: z.union([z.record(z.any()), z.string()]).optional(),
    },
  },
  async (args): Promise<ToolResult> => {
    try {
      const data = clean({
        title: args.title,
        slug: ensureSlug(args.slug, args.title),
        isTopic: args.isTopic,
        isPublished: args.isPublished,
        description: args.description,
        ...coerceObject(args.extra),
      });
      return ok(await client.create("tags", data, { locale: args.locale }));
    } catch (e) {
      return fail(e);
    }
  },
);

server.registerTool(
  "cms_create_category",
  {
    title: "Create category",
    description: "Create a category. `name` is required; `slug` is auto-derived if omitted. `parentId` nests under another category.",
    inputSchema: {
      name: z.string(),
      slug: z.string().optional(),
      locale: z.string().optional(),
      parentId: z.union([z.string(), z.number()]).optional(),
      isPublished: z.boolean().optional(),
      description: z.string().optional(),
      extra: z.union([z.record(z.any()), z.string()]).optional(),
    },
  },
  async (args): Promise<ToolResult> => {
    try {
      const data = clean({
        name: args.name,
        slug: ensureSlug(args.slug, args.name),
        parentId: args.parentId,
        isPublished: args.isPublished,
        description: args.description,
        ...coerceObject(args.extra),
      });
      return ok(await client.create("categories", data, { locale: args.locale }));
    } catch (e) {
      return fail(e);
    }
  },
);

server.registerTool(
  "cms_upload",
  {
    title: "Upload media",
    description:
      "Upload a local file to an upload-enabled collection: 'media', 'article-covers', 'author-images', or 'tag-images'. Returns the created document (use its id in article cover / author cover fields).",
    inputSchema: {
      collection: z
        .enum(["media", "article-covers", "author-images", "tag-images"])
        .describe("Upload collection."),
      filePath: z.string().describe("Absolute path to a local file."),
      alt: z.string().optional().describe("Alt text / caption, if the collection supports it."),
      locale: z.string().optional(),
      extra: z.union([z.record(z.any()), z.string()]).optional(),
    },
  },
  async (args): Promise<ToolResult> => {
    try {
      const buffer = readFileSync(args.filePath);
      const filename = basename(args.filePath);
      const extraData = clean({ alt: args.alt, ...coerceObject(args.extra) });
      const res = await client.upload(
        args.collection,
        { buffer, filename, mimetype: mimeFromFilename(filename) },
        extraData,
        { locale: args.locale },
      );
      return ok(res);
    } catch (e) {
      return fail(e);
    }
  },
);

server.registerTool(
  "cms_request",
  {
    title: "Raw REST request",
    description:
      "Escape hatch: call any Payload REST endpoint directly. Useful for endpoints not covered by other tools, or if helpers drift from the schema. Example: method='GET', path='/api/articles', query={\"limit\":1}.",
    inputSchema: {
      method: z.enum(["GET", "POST", "PATCH", "PUT", "DELETE"]),
      path: z.string().describe("Path starting with /api/..."),
      body: z.union([z.record(z.any()), z.string()]).optional(),
      query: z.union([z.record(z.any()), z.string()]).optional(),
    },
  },
  async (args): Promise<ToolResult> => {
    try {
      const res = await client.request(args.method, args.path, {
        body: args.body !== undefined ? coerceObject(args.body) : undefined,
        query: args.query !== undefined ? coerceObject(args.query) : undefined,
      });
      return ok(res);
    } catch (e) {
      return fail(e);
    }
  },
);

server.registerTool(
  "cms_schema_doctor",
  {
    title: "Check schema drift",
    description:
      "Compare the stored schema snapshot against the current payload-types.ts of the CMS repo (CMS_PAYLOAD_TYPES_PATH). Reports added/removed fields per collection so you know when to refresh helpers. Run `npm run sync-schema` to update the baseline.",
    inputSchema: {
      typesPath: z.string().optional().describe("Override path to payload-types.ts."),
    },
  },
  async (args): Promise<ToolResult> => {
    try {
      return ok(doctor(args.typesPath ?? cfg.payloadTypesPath));
    } catch (e) {
      return fail(e);
    }
  },
);

// ------------------------------------------------------------------- main ----

async function main(): Promise<void> {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  // Never write to stdout; stdio transport owns it. Logs go to stderr.
  console.error(
    `[payload-cms-mcp ${VERSION}] connected. Target: ${cfg.baseUrl || "(none — set via cms_set_target)"}`,
  );
  if (cfg.settingsFile) {
    console.error(`[payload-cms-mcp] settings from ${cfg.settingsFile.path}: ${cfg.settingsFile.keys.join(", ") || "(nothing new)"}`);
    if (cfg.settingsFile.ignoredSecrets.length) {
      console.error(`[payload-cms-mcp] ignored in settings file (keep credentials in env): ${cfg.settingsFile.ignoredSecrets.join(", ")}`);
    }
  }
}

main().catch((e) => {
  console.error("[payload-cms-mcp] fatal:", e);
  process.exit(1);
});
