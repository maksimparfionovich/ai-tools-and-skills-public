/**
 * Convert plain text / lightweight Markdown into a Payload Lexical rich-text
 * value suitable for the Articles `text` field.
 *
 * Supported Markdown:
 *   # / ## / ### / #### headings
 *   paragraphs (blank-line separated)
 *   - or * bullet lists, 1. ordered lists
 *   inline: **bold**, *italic* / _italic_, `code`, [text](url)
 *
 * The output matches the @payloadcms/richtext-lexical default node shapes.
 * If the content field already drifts to other features (blocks, uploads),
 * callers can still pass a raw lexical object straight through.
 */

const FORMAT_BOLD = 1;
const FORMAT_ITALIC = 2;
const FORMAT_CODE = 16;

interface LexNode {
  [k: string]: unknown;
}

function textNode(text: string, format = 0): LexNode {
  return { type: "text", version: 1, text, format, detail: 0, mode: "normal", style: "" };
}

function linkNode(url: string, children: LexNode[]): LexNode {
  return {
    type: "link",
    version: 3,
    fields: { linkType: "custom", newTab: false, url },
    format: "",
    indent: 0,
    direction: "ltr",
    children,
  };
}

/** Parse inline markdown (bold/italic/code/links) into lexical inline nodes. */
function parseInline(input: string): LexNode[] {
  const nodes: LexNode[] = [];
  let i = 0;
  const n = input.length;
  let buf = "";

  const flush = (format = 0) => {
    if (buf) {
      nodes.push(textNode(buf, format));
      buf = "";
    }
  };

  while (i < n) {
    const rest = input.slice(i);

    // Link: [text](url)
    const link = /^\[([^\]]+)\]\(([^)\s]+)\)/.exec(rest);
    if (link) {
      flush();
      nodes.push(linkNode(link[2], parseInline(link[1])));
      i += link[0].length;
      continue;
    }
    // Bold: **text**
    const bold = /^\*\*([^*]+)\*\*/.exec(rest);
    if (bold) {
      flush();
      nodes.push(textNode(bold[1], FORMAT_BOLD));
      i += bold[0].length;
      continue;
    }
    // Italic: *text* or _text_
    const italic = /^(?:\*([^*]+)\*|_([^_]+)_)/.exec(rest);
    if (italic) {
      flush();
      nodes.push(textNode(italic[1] ?? italic[2], FORMAT_ITALIC));
      i += italic[0].length;
      continue;
    }
    // Inline code: `code`
    const code = /^`([^`]+)`/.exec(rest);
    if (code) {
      flush();
      nodes.push(textNode(code[1], FORMAT_CODE));
      i += code[0].length;
      continue;
    }
    buf += input[i];
    i += 1;
  }
  flush();
  return nodes.length ? nodes : [textNode("")];
}

function paragraph(children: LexNode[]): LexNode {
  return { type: "paragraph", version: 1, format: "", indent: 0, direction: "ltr", textFormat: 0, children };
}

function heading(tag: string, children: LexNode[]): LexNode {
  return { type: "heading", tag, version: 1, format: "", indent: 0, direction: "ltr", children };
}

function listItem(children: LexNode[], value: number): LexNode {
  return { type: "listitem", version: 1, value, format: "", indent: 0, direction: "ltr", children };
}

function list(listType: "bullet" | "number", items: LexNode[]): LexNode {
  return {
    type: "list",
    version: 1,
    listType,
    tag: listType === "bullet" ? "ul" : "ol",
    start: 1,
    format: "",
    indent: 0,
    direction: "ltr",
    children: items,
  };
}

function root(children: LexNode[]): { root: LexNode } {
  return {
    root: {
      type: "root",
      version: 1,
      format: "",
      indent: 0,
      direction: "ltr",
      children: children.length ? children : [paragraph([textNode("")])],
    },
  };
}

/** Convert lightweight Markdown to a Payload lexical value. */
export function markdownToLexical(md: string): { root: LexNode } {
  const lines = md.replace(/\r\n/g, "\n").split("\n");
  const blocks: LexNode[] = [];
  let para: string[] = [];
  let bullets: LexNode[] = [];
  let ordered: LexNode[] = [];

  const flushPara = () => {
    if (para.length) {
      blocks.push(paragraph(parseInline(para.join(" ").trim())));
      para = [];
    }
  };
  const flushBullets = () => {
    if (bullets.length) {
      blocks.push(list("bullet", bullets));
      bullets = [];
    }
  };
  const flushOrdered = () => {
    if (ordered.length) {
      blocks.push(list("number", ordered));
      ordered = [];
    }
  };
  const flushAll = () => {
    flushPara();
    flushBullets();
    flushOrdered();
  };

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) {
      flushAll();
      continue;
    }
    const h = /^(#{1,4})\s+(.*)$/.exec(trimmed);
    if (h) {
      flushAll();
      blocks.push(heading(`h${h[1].length}`, parseInline(h[2])));
      continue;
    }
    const bullet = /^[-*]\s+(.*)$/.exec(trimmed);
    if (bullet) {
      flushPara();
      flushOrdered();
      bullets.push(listItem(parseInline(bullet[1]), bullets.length + 1));
      continue;
    }
    const ol = /^\d+\.\s+(.*)$/.exec(trimmed);
    if (ol) {
      flushPara();
      flushBullets();
      ordered.push(listItem(parseInline(ol[1]), ordered.length + 1));
      continue;
    }
    flushBullets();
    flushOrdered();
    para.push(trimmed);
  }
  flushAll();
  return root(blocks);
}

/** Plain text -> lexical (each blank-line block becomes a paragraph). */
export function plainTextToLexical(text: string): { root: LexNode } {
  const paras = text
    .replace(/\r\n/g, "\n")
    .split(/\n{2,}/)
    .map((p) => p.trim())
    .filter(Boolean);
  return root(paras.map((p) => paragraph([textNode(p)])));
}

/**
 * Normalize a `text` input into a lexical value.
 * Accepts: an already-lexical object ({root}), or a string treated as markdown.
 */
export function toLexical(input: unknown, mode: "markdown" | "plain" = "markdown"): unknown {
  if (input && typeof input === "object" && "root" in (input as Record<string, unknown>)) {
    return input; // already lexical
  }
  if (typeof input === "string") {
    return mode === "plain" ? plainTextToLexical(input) : markdownToLexical(input);
  }
  return root([]);
}
