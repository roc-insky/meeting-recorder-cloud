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

// Durable meeting event feed. EventSource reconnects on its own and echoes the
// last SSE id as Last-Event-ID, so the server resumes from the durable cursor.
// No token is ever placed in the URL: the HttpOnly session cookie is used.
export const MEETING_EVENT_NAMES = Object.freeze([
  "snapshot",
  "meeting.created",
  "meeting.updated",
  "upload.created",
  "upload.updated",
  "upload.progress",
  "upload.sealed",
  "task.updated",
  "transcript.published",
]);

export function meetingEventsUrl(meetingId, { after = null, baseUrl = "" } = {}) {
  if (typeof meetingId !== "string" || meetingId.length === 0) {
    throw new TypeError("meetingId is required");
  }
  const target = new URL(
    `/api/cloud/meetings/${encodeURIComponent(meetingId)}/events/stream`,
    baseUrl || globalThis.location?.origin || "http://localhost",
  );
  if (Number.isInteger(after) && after >= 0) target.searchParams.set("after", String(after));
  return target;
}

export function openMeetingEvents(options = {}) {
  const {
    meetingId,
    after = null,
    baseUrl = "",
    eventSourceFactory = (url, init) => new globalThis.EventSource(url, init),
    onEvent = () => {},
    onError = () => {},
  } = options;

  const target = meetingEventsUrl(meetingId, { after, baseUrl });
  const source = eventSourceFactory(target.toString(), { withCredentials: true });
  let closed = false;

  function close() {
    if (closed) return;
    closed = true;
    try {
      source?.close?.();
    } catch {
      // Closing a stream never throws into logout or session-expiry handling.
    }
  }

  function dispatch(name) {
    return (message) => {
      if (closed) return;
      const data = safeJson(message?.data);
      if (!data || typeof data !== "object" || Array.isArray(data)) {
        onError({ code: "invalid_event_data", event: name });
        return;
      }
      const parsedId = Number(message?.lastEventId);
      const cursor = Number.isInteger(parsedId) && parsedId >= 0 ? parsedId : null;
      try {
        onEvent({ event: name, cursor, data });
      } catch {
        // A listener must not break the stream for later events.
      }
    };
  }

  for (const name of MEETING_EVENT_NAMES) {
    source.addEventListener(name, dispatch(name));
  }
  source.onerror = (error) => {
    if (!closed) onError(error ?? { code: "stream_error" });
  };

  return { close };
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
