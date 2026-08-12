export const ROUTES = Object.freeze([
  Object.freeze({ name: "overview", pattern: "/overview" }),
  Object.freeze({ name: "operations", pattern: "/operations" }),
  Object.freeze({ name: "operation-detail", pattern: "/operations/:taskId" }),
  Object.freeze({ name: "alerts", pattern: "/alerts" }),
  Object.freeze({ name: "connections", pattern: "/connections" }),
  Object.freeze({ name: "approvals", pattern: "/approvals" }),
  Object.freeze({ name: "audit", pattern: "/audit" }),
  Object.freeze({ name: "model-settings", pattern: "/settings/models" }),
  Object.freeze({ name: "onboarding", pattern: "/onboarding" }),
]);

function normalizePath(path) {
  const value = String(path || "").replace(/^#/, "").trim();
  if (!value || value === "/") return "/overview";
  return value.startsWith("/") ? value : `/${value}`;
}

function splitPath(path) {
  return normalizePath(path)
    .split("/")
    .filter(Boolean)
    .map((segment) => decodeURIComponent(segment));
}

export function resolveRoute(path, routes = ROUTES) {
  const normalizedPath = normalizePath(path);
  const segments = splitPath(normalizedPath);

  for (const route of routes) {
    const patternSegments = splitPath(route.pattern);
    if (patternSegments.length !== segments.length) continue;

    const params = {};
    const matches = patternSegments.every((segment, index) => {
      if (!segment.startsWith(":")) return segment === segments[index];
      params[segment.slice(1)] = segments[index];
      return true;
    });
    if (matches) {
      return Object.freeze({
        ...route,
        path: normalizedPath,
        params: Object.freeze(params),
        matched: true,
      });
    }
  }

  return Object.freeze({
    name: "not-found",
    pattern: null,
    path: normalizedPath,
    params: Object.freeze({}),
    matched: false,
  });
}

/**
 * Hash routing keeps API endpoints and static file hosting unchanged. It
 * contains no persistence, so a refresh always rebuilds facts from APIs.
 */
export function createHashRouter({
  windowObject = globalThis.window,
  routes = ROUTES,
} = {}) {
  if (!windowObject) throw new TypeError("A browser window is required");

  const subscribers = new Set();
  let started = false;
  let currentRoute = resolveRoute(windowObject.location.hash, routes);

  function publish() {
    currentRoute = resolveRoute(windowObject.location.hash, routes);
    subscribers.forEach((listener) => listener(currentRoute));
    return currentRoute;
  }

  function onHashChange() {
    publish();
  }

  function start() {
    if (started) return currentRoute;
    started = true;
    windowObject.addEventListener("hashchange", onHashChange);
    return publish();
  }

  function stop() {
    if (!started) return;
    windowObject.removeEventListener("hashchange", onHashChange);
    started = false;
  }

  function navigate(path, { replace = false } = {}) {
    const nextPath = normalizePath(path);
    const hash = `#${nextPath}`;
    if (windowObject.location.hash === hash) return publish();
    if (replace) {
      const url = new URL(windowObject.location.href);
      url.hash = hash;
      windowObject.history.replaceState(null, "", url);
      return publish();
    }
    windowObject.location.hash = hash;
    return resolveRoute(nextPath, routes);
  }

  function subscribe(listener, { immediate = true } = {}) {
    if (typeof listener !== "function") throw new TypeError("Listener must be a function");
    subscribers.add(listener);
    if (immediate) listener(currentRoute);
    return () => subscribers.delete(listener);
  }

  return Object.freeze({
    start,
    stop,
    navigate,
    subscribe,
    get current() {
      return currentRoute;
    },
  });
}
