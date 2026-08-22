import { api } from "./core/api.js";
import { createDemoApiClient } from "./core/demo-api.js";
import { mountRuntimeConsole } from "./runtime-console.js";

export function bootstrapFrontend() {
  const root = document.getElementById("runtime-console-root");
  if (!root) return null;
  const params = new URLSearchParams(window.location.search);
  const demoMode = params.get("demo") === "1";
  const client = demoMode ? createDemoApiClient() : api;
  const runtimeConsole = mountRuntimeConsole(root, {
    api: client,
    initialView: params.get("view") === "skills" ? "skills" : "tasks",
    initialInspector: params.get("inspector") || "run",
    initialSkillSection: params.get("section") || "candidate",
  });
  const frontend = Object.freeze({ api: client, runtimeConsole, demoMode });
  globalThis.window.AthenaFrontend = frontend;
  return frontend;
}

if (typeof document !== "undefined") {
  bootstrapFrontend();
}
