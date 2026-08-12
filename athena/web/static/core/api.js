const JSON_MEDIA_TYPE = "application/json";

export class ApiError extends Error {
  constructor({ status, code, message }) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code || "REQUEST_FAILED";
  }
}

function isJsonResponse(response) {
  return response.headers.get("content-type")?.includes(JSON_MEDIA_TYPE);
}

async function readPayload(response) {
  if (response.status === 204) return null;
  if (isJsonResponse(response)) return response.json();

  const text = await response.text();
  return text ? { message: text } : null;
}

function errorFromResponse(response, payload) {
  const message =
    typeof payload?.message === "string" && payload.message.trim()
      ? payload.message
      : response.statusText || "Request failed";
  return new ApiError({
    status: response.status,
    code: typeof payload?.error_code === "string" ? payload.error_code : undefined,
    message,
  });
}

function unwrapEnvelope(payload) {
  if (
    payload &&
    typeof payload === "object" &&
    Object.prototype.hasOwnProperty.call(payload, "data")
  ) {
    return payload.data;
  }
  return payload;
}

function normalizeBody(body, headers) {
  if (body === undefined || body === null || typeof body === "string") return body;
  if (
    (typeof FormData !== "undefined" && body instanceof FormData) ||
    (typeof URLSearchParams !== "undefined" && body instanceof URLSearchParams)
  ) {
    return body;
  }
  if (!headers.has("Content-Type")) headers.set("Content-Type", JSON_MEDIA_TYPE);
  return JSON.stringify(body);
}

/**
 * A small API client deliberately keeps credentials and task facts outside the
 * browser. Authentication belongs to the server-managed session boundary.
 */
export function createApiClient({ fetchImpl = globalThis.fetch, baseUrl = "" } = {}) {
  if (typeof fetchImpl !== "function") {
    throw new TypeError("A fetch implementation is required");
  }

  async function request(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Accept", JSON_MEDIA_TYPE);
    const response = await fetchImpl(`${baseUrl}${path}`, {
      ...options,
      headers,
      body: normalizeBody(options.body, headers),
    });
    const payload = await readPayload(response);
    if (!response.ok) throw errorFromResponse(response, payload);
    return unwrapEnvelope(payload);
  }

  return Object.freeze({
    request,
    get: (path, options) => request(path, { ...options, method: "GET" }),
    post: (path, body, options) => request(path, { ...options, method: "POST", body }),
    patch: (path, body, options) => request(path, { ...options, method: "PATCH", body }),
    delete: (path, options) => request(path, { ...options, method: "DELETE" }),
  });
}

export const api = createApiClient();
