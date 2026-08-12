function copy(value) {
  if (typeof structuredClone === "function") return structuredClone(value);
  return JSON.parse(JSON.stringify(value));
}

function freeze(value) {
  if (!value || typeof value !== "object" || Object.isFrozen(value)) return value;
  Object.values(value).forEach(freeze);
  return Object.freeze(value);
}

/**
 * Stores are in-memory projections only. They intentionally have no browser
 * persistence: refresh and SSE recovery must reconstruct facts from the API.
 */
export function createStore(initialState) {
  let state = freeze(copy(initialState));
  const listeners = new Set();

  function emit() {
    const snapshot = copy(state);
    listeners.forEach((listener) => listener(snapshot));
  }

  function replace(nextState) {
    state = freeze(copy(nextState));
    emit();
    return copy(state);
  }

  function patch(partialState) {
    return replace({ ...state, ...partialState });
  }

  function subscribe(listener, { immediate = true } = {}) {
    if (typeof listener !== "function") throw new TypeError("Listener must be a function");
    listeners.add(listener);
    if (immediate) listener(copy(state));
    return () => listeners.delete(listener);
  }

  return Object.freeze({
    getState: () => copy(state),
    replace,
    patch,
    subscribe,
  });
}

function createEntityStore(idOf = (item) => item?.id) {
  const base = createStore({
    items: [],
    byId: {},
    fetchedAt: null,
    status: "idle",
    errorCode: null,
    selection: { item: null, status: "idle", errorCode: null },
  });

  function replace(items, { status, errorCode = null } = {}) {
    const safeItems = Array.isArray(items) ? items : [];
    const byId = Object.fromEntries(
      safeItems
        .filter((item) => item && typeof idOf(item) === "string")
        .map((item) => [idOf(item), item]),
    );
    return base.replace({
      ...base.getState(),
      items: safeItems,
      byId,
      fetchedAt: new Date().toISOString(),
      status: status || (safeItems.length ? "ready" : "empty"),
      errorCode,
    });
  }

  function upsert(item) {
    const id = idOf(item);
    if (!item || typeof id !== "string") throw new TypeError("Entity id is required");
    const current = base.getState().items;
    const index = current.findIndex((candidate) => idOf(candidate) === id);
    const next = [...current];
    if (index === -1) next.unshift(item);
    else next[index] = item;
    return replace(next, { status: "ready" });
  }

  function setLoading() {
    return base.patch({ status: "loading", errorCode: null });
  }

  function setFailure({ status = "error", errorCode = "PROJECTION_UNAVAILABLE" } = {}) {
    return base.patch({ status, errorCode });
  }

  function setSelection(item, { status = "ready", errorCode = null } = {}) {
    return base.patch({ selection: { item, status, errorCode } });
  }

  return Object.freeze({
    ...base,
    replace,
    upsert,
    clear: () => replace([]),
    setLoading,
    setFailure,
    setSelection,
  });
}

// appState deliberately contains only identity, routing, and global health.
export const appState = createStore({
  identity: null,
  route: { name: "overview", path: "/overview", params: {} },
  health: { status: "unknown", components: [] },
});

export const taskStore = createEntityStore();
export const connectionStore = createEntityStore();
export const operationPlanStore = createEntityStore();
export const approvalStore = createEntityStore();
export const sessionStore = createEntityStore((item) => item?.session_id);
export const modelStore = createEntityStore((item) => item?.configId);
