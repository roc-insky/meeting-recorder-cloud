// Browser-side session helper for the cloud meeting recorder.
//
// Deliberately small: the server owns the session (HttpOnly cookie), this module
// only mirrors the CSRF cookie into the X-CSRF-Token header and forwards requests
// with credentials. No tokens are ever stored in localStorage or URLs.
//
// Node 22 compatible: global fetch, Headers, URL, and Response only.

export const CSRF_COOKIE = "cloud_csrf";
export const CSRF_HEADER = "X-CSRF-Token";
export const SESSION_PATH = "/api/cloud/auth/session";
export const LOGIN_PATH = "/api/cloud/auth/login";
export const LOGOUT_PATH = "/api/cloud/auth/logout";

const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export class CloudRequestError extends Error {
  constructor(status, code, body) {
    super(`cloud request failed: ${status} ${code ?? "unknown"}`);
    this.name = "CloudRequestError";
    this.status = status;
    this.code = code;
    this.body = body;
  }
}

export function readCookie(name, cookieString) {
  for (const part of String(cookieString ?? "").split(";")) {
    const separator = part.indexOf("=");
    if (separator === -1) continue;
    if (part.slice(0, separator).trim() !== name) continue;
    return decodeURIComponent(part.slice(separator + 1).trim());
  }
  return null;
}

export function createCloudSession(options = {}) {
  const {
    baseUrl = "",
    fetchImpl = (...args) => globalThis.fetch(...args),
    cookieSource = () => globalThis.document?.cookie ?? "",
  } = options;

  function url(path) {
    return new URL(path, baseUrl || globalThis.location?.origin || "http://localhost");
  }

  async function request(path, init = {}) {
    const method = (init.method ?? "GET").toUpperCase();
    const headers = new Headers(init.headers ?? {});
    headers.set("Accept", "application/json");
    if (init.body !== undefined && init.body !== null && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    if (UNSAFE_METHODS.has(method)) {
      const csrf = readCookie(CSRF_COOKIE, cookieSource());
      if (!csrf) throw new CloudRequestError(403, "csrf_missing", null);
      headers.set(CSRF_HEADER, csrf);
    }
    const response = await fetchImpl(url(path).toString(), {
      ...init,
      method,
      headers,
      credentials: "same-origin",
      body:
        init.body !== undefined && init.body !== null && typeof init.body !== "string"
          ? JSON.stringify(init.body)
          : init.body,
    });
    const text = await response.text();
    const body = text ? safeJson(text) : null;
    if (!response.ok) {
      throw new CloudRequestError(response.status, body?.error ?? null, body);
    }
    return body;
  }

  return {
    request,
    session: () => request(SESSION_PATH),
    loginUrl: (email) => {
      const target = url(LOGIN_PATH);
      if (email) target.searchParams.set("email", email);
      return target.toString();
    },
    async logout() {
      await request(LOGOUT_PATH, { method: "POST" });
      return null;
    },
  };
}

function safeJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}
