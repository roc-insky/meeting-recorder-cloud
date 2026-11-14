// Minimal static file server for the cloud browser tests.
//
// IndexedDB is origin-scoped and file:// origins are opaque in Chromium, so the
// capture/buffer suites need a real http:// origin. Serving the repository root
// also lets the pages import /web/cloud/*.mjs directly.
import { createServer } from "node:http";
import { readFile, stat } from "node:fs/promises";
import { extname, join, normalize, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(fileURLToPath(new URL("../../..", import.meta.url)));
const PORT = Number(process.argv[process.argv.indexOf("--port") + 1] ?? 43119);
const HOST = "127.0.0.1";

const TYPES = new Map([
  [".html", "text/html; charset=utf-8"],
  [".mjs", "text/javascript; charset=utf-8"],
  [".js", "text/javascript; charset=utf-8"],
  [".css", "text/css; charset=utf-8"],
  [".json", "application/json; charset=utf-8"],
  [".webm", "audio/webm"],
  [".svg", "image/svg+xml"],
]);

function insideRoot(path) {
  const normalized = normalize(path);
  return normalized === ROOT || normalized.startsWith(ROOT + sep);
}

export function createStaticServer() {
  return createServer(async (request, response) => {
    try {
      const url = new URL(request.url ?? "/", `http://${HOST}`);
      const pathname = decodeURIComponent(url.pathname);
      const relative = pathname === "/" ? "tests/browser/cloud/fixture.html" : pathname.slice(1);
      const target = resolve(join(ROOT, relative));
      if (!insideRoot(target)) {
        response.writeHead(403, { "Content-Type": "text/plain" });
        response.end("forbidden");
        return;
      }
      const info = await stat(target);
      if (!info.isFile()) {
        response.writeHead(404, { "Content-Type": "text/plain" });
        response.end("not found");
        return;
      }
      const body = await readFile(target);
      response.writeHead(200, {
        "Content-Type": TYPES.get(extname(target)) ?? "application/octet-stream",
        "Content-Length": body.byteLength,
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
      });
      response.end(body);
    } catch (error) {
      const missing = error && error.code === "ENOENT";
      response.writeHead(missing ? 404 : 500, { "Content-Type": "text/plain" });
      response.end(missing ? "not found" : "server error");
    }
  });
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const server = createStaticServer();
  server.listen(PORT, HOST, () => {
    process.stdout.write(`cloud test server listening on http://${HOST}:${PORT}\n`);
  });
  for (const signal of ["SIGINT", "SIGTERM"]) {
    process.on(signal, () => server.close(() => process.exit(0)));
  }
}
