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
    const body = encodeBody(init.body);
    if (body !== undefined && body !== null && !headers.has("Content-Type") && isJsonBody(init.body)) {
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
      body,
    });
    const text = await response.text();
    const payload = text ? safeJson(text) : null;
    if (!response.ok) {
      throw new CloudRequestError(response.status, payload?.error ?? null, payload);
    }
    return payload;
  }

  return {
    request,
    session: () => request(SESSION_PATH),
    loginUrl: (email) => {
      const target = url(LOGIN_PATH);
      if (email) target.searchParams.set("email", email);
      return target.toString();
    },
    async logout(options = {}) {
      // The hook runs before the request so the caller can stop capture and
      // record the final tail chunk while the session is still valid.
      const beforeLogout = options.beforeLogout ?? null;
      if (beforeLogout) await beforeLogout();
      await request(LOGOUT_PATH, { method: "POST" });
      return null;
    },
  };
}

// True only for the server's own unauthenticated response, so a transport or
// unrelated error is never mistaken for an expired session.
export function isSessionExpired(error) {
  return error instanceof CloudRequestError && error.status === 401;
}

function isJsonBody(body) {
  if (body === undefined || body === null) return false;
  if (typeof body === "string") return false;
  return !isRawBody(body);
}

function isRawBody(body) {
  if (typeof body !== "object") return false;
  if (typeof Blob !== "undefined" && body instanceof Blob) return true;
  if (typeof ArrayBuffer !== "undefined" && body instanceof ArrayBuffer) return true;
  if (typeof ArrayBuffer !== "undefined" && ArrayBuffer.isView(body)) return true;
  if (typeof FormData !== "undefined" && body instanceof FormData) return true;
  if (typeof URLSearchParams !== "undefined" && body instanceof URLSearchParams) return true;
  if (typeof ReadableStream !== "undefined" && body instanceof ReadableStream) return true;
  return false;
}

function encodeBody(body) {
  if (body === undefined || body === null) return body;
  if (typeof body === "string") return body;
  if (isRawBody(body)) return body;
  return JSON.stringify(body);
}

function safeJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}
