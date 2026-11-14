// Safe, dependency-free Markdown renderer for the cloud meeting recorder.
//
// Security contract:
// * only DOM nodes are built with createElement/createTextNode, never innerHTML;
// * raw HTML, script, iframe, img, svg, event handlers and style attributes in the
//   source are always plain text;
// * a link becomes an anchor only when its URL resolves to http: or https:;
//   javascript:, data:, file: and every other scheme stay inert text;
// * every link opens in a new tab with rel="noopener noreferrer".
//
// The renderer is intentionally small: headings #..###, paragraphs, unordered and
// ordered lists, fenced code, inline code, strong/em, Markdown links and bare
// http(s) URLs. Anything else is rendered as literal text.

export const MAX_MARKDOWN_CHARS = 40000;

export function renderSafeMarkdown(target, markdown) {
  if (!target || typeof target.replaceChildren !== "function") {
    throw new TypeError("renderSafeMarkdown requires a DOM element");
  }
  const source = typeof markdown === "string" ? markdown : "";
  if (source.length > MAX_MARKDOWN_CHARS) {
    throw new RangeError("markdown_too_long");
  }
  const doc = target.ownerDocument || globalThis.document;
  if (!doc) throw new Error("renderSafeMarkdown requires a document");

  target.replaceChildren();
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (line.trim() === "") {
      index += 1;
      continue;
    }

    const fence = /^\s*(```+|~~~+)\s*([A-Za-z0-9_+.-]*)\s*$/.exec(line);
    if (fence) {
      const marker = fence[1][0];
      const language = fence[2];
      const codeLines = [];
      index += 1;
      while (index < lines.length) {
        const candidate = lines[index];
        if (new RegExp("^\\s*" + marker + "{3,}\\s*$").test(candidate)) {
          index += 1;
          break;
        }
        codeLines.push(candidate);
        index += 1;
      }
      const pre = doc.createElement("pre");
      const code = doc.createElement("code");
      if (language) code.className = "language-" + language;
      code.textContent = codeLines.join("\n");
      pre.append(code);
      target.append(pre);
      continue;
    }

    const heading = /^(#{1,3})\s+(.+?)\s*$/.exec(line);
    if (heading) {
      const level = heading[1].length;
      const node = doc.createElement("h" + level);
      appendInline(node, heading[2]);
      target.append(node);
      index += 1;
      continue;
    }

    const unordered = /^\s*[-*+]\s+(.+?)\s*$/.exec(line);
    const ordered = /^\s*(\d{1,9})[.)]\s+(.+?)\s*$/.exec(line);
    if (unordered || ordered) {
      const orderedList = Boolean(ordered);
      const list = doc.createElement(orderedList ? "ol" : "ul");
      if (orderedList) {
        const start = Number(ordered[1]);
        if (Number.isInteger(start) && start !== 1) list.setAttribute("start", String(start));
      }
      while (index < lines.length) {
        const candidate = lines[index];
        const item = orderedList
          ? /^\s*(\d{1,9})[.)]\s+(.+?)\s*$/.exec(candidate)
          : /^\s*[-*+]\s+(.+?)\s*$/.exec(candidate);
        if (!item) break;
        const li = doc.createElement("li");
        appendInline(li, orderedList ? item[2] : item[1]);
        list.append(li);
        index += 1;
      }
      target.append(list);
      continue;
    }

    const paragraphLines = [];
    while (index < lines.length) {
      const candidate = lines[index];
      if (candidate.trim() === "" || isBlockStart(candidate)) break;
      paragraphLines.push(candidate.trim());
      index += 1;
    }
    if (paragraphLines.length === 0) {
      // Defensive: a block-looking line that failed its own parser is emitted as
      // text instead of being dropped or retried forever.
      paragraphLines.push(line.trim());
      index += 1;
    }
    const paragraph = doc.createElement("p");
    appendInline(paragraph, paragraphLines.join("\n"));
    target.append(paragraph);
  }
}

function isBlockStart(line) {
  return (
    /^\s*(```+|~~~+)/.test(line) ||
    /^(#{1,3})\s+/.test(line) ||
    /^\s*[-*+]\s+/.test(line) ||
    /^\s*\d{1,9}[.)]\s+/.test(line)
  );
}

function appendInline(parent, text) {
  const doc = parent.ownerDocument;
  let buffer = "";
  const flush = () => {
    if (buffer) {
      parent.append(doc.createTextNode(buffer));
      buffer = "";
    }
  };

  let index = 0;
  while (index < text.length) {
    const rest = text.slice(index);

    if (rest[0] === "`") {
      const end = text.indexOf("`", index + 1);
      if (end > index + 1) {
        flush();
        const code = doc.createElement("code");
        code.textContent = text.slice(index + 1, end).replace(/\n/g, " ");
        parent.append(code);
        index = end + 1;
        continue;
      }
    }

    if (rest.startsWith("**")) {
      const end = text.indexOf("**", index + 2);
      if (end > index + 2) {
        flush();
        const strong = doc.createElement("strong");
        appendInline(strong, text.slice(index + 2, end));
        parent.append(strong);
        index = end + 2;
        continue;
      }
    }

    if (rest[0] === "*" && !rest.startsWith("**")) {
      const end = text.indexOf("*", index + 1);
      if (end > index + 1) {
        flush();
        const em = doc.createElement("em");
        appendInline(em, text.slice(index + 1, end));
        parent.append(em);
        index = end + 1;
        continue;
      }
    }

    if (rest[0] === "[") {
      const link = /^\[([^\]\n]{1,500})\]\(([^)\s]{1,2000})\)/.exec(rest);
      if (link) {
        flush();
        appendLink(parent, link[1], link[2]);
        index += link[0].length;
        continue;
      }
    }

    if (rest.startsWith("http://") || rest.startsWith("https://")) {
      const previous = index > 0 ? text[index - 1] : "";
      if (previous === "" || /[\s(（【，。；：\u201c"']/.test(previous)) {
        const candidate = /^https?:\/\/[^\s<>()（）【】]+/.exec(rest);
        if (candidate) {
          flush();
          const rawUrl = trimTrailingPunctuation(candidate[0]);
          appendLink(parent, rawUrl, rawUrl);
          index += rawUrl.length;
          continue;
        }
      }
    }

    buffer += text[index];
    index += 1;
  }
  flush();
}

function trimTrailingPunctuation(value) {
  let url = value;
  while (url.length > 0 && /[.,;:!?，。；：！？、]/.test(url[url.length - 1])) {
    url = url.slice(0, -1);
  }
  // A closing parenthesis belongs to the URL only when it balances one inside it.
  while (url.endsWith(")") && (url.match(/\)/g) || []).length > (url.match(/\(/g) || []).length) {
    url = url.slice(0, -1);
  }
  return url;
}

function appendLink(parent, label, rawUrl) {
  const doc = parent.ownerDocument;
  const href = safeHref(rawUrl);
  if (!href) {
    // Unsafe or malformed target: keep visible text and discard the URL entirely.
    parent.append(doc.createTextNode(label));
    return;
  }
  const anchor = doc.createElement("a");
  anchor.setAttribute("href", href);
  anchor.setAttribute("target", "_blank");
  anchor.setAttribute("rel", "noopener noreferrer");
  anchor.textContent = label;
  parent.append(anchor);
}

function safeHref(rawUrl) {
  if (typeof rawUrl !== "string" || rawUrl.trim() === "") return null;
  try {
    const base =
      parentBase() ||
      globalThis.location?.href ||
      "http://localhost/";
    const url = new URL(rawUrl, base);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    return url.toString();
  } catch {
    return null;
  }
}

function parentBase() {
  return globalThis.document?.baseURI || null;
}
