import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { parseEnv } from "node:util";

export function serverEnvironment(contents, environment) {
  if (!["development", "production"].includes(environment)) throw new Error("Unknown environment");
  const env = parseEnv(contents);
  if (env.NODE_ENV !== environment) throw new Error("NODE_ENV must match the selected environment");
  // ponytail: one local database boundary; add a separate test profile if remote development is needed.
  if (environment === "development" && (env.MYSQL_HOST !== "127.0.0.1" ||
      env.MYSQL_PORT !== "23306" || env.MYSQL_DATABASE !== "wechat_relay_dev" ||
      env.DATA_DIR !== "data/development" || env.REDIS_URL)) {
    throw new Error("Development requires its isolated local database and data directory, without Redis");
  }
  let origin;
  try { origin = new URL(env.PUBLIC_ORIGIN); } catch { throw new Error("Invalid PUBLIC_ORIGIN"); }
  if (origin.username || origin.password || origin.pathname !== "/" || origin.search || origin.hash) {
    throw new Error("PUBLIC_ORIGIN must be an origin without credentials, path, query or fragment");
  }
  if (environment === "production" && origin.protocol !== "https:") {
    throw new Error("Production requires an HTTPS PUBLIC_ORIGIN");
  }
  for (const name of ["MYSQL_HOST", "MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD", "TEST_TOKEN", "STORAGE_KEY", "VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "PUBLIC_ORIGIN", "DATA_DIR"]) {
    if (!env[name]) throw new Error(`${name} is required in .env.${environment}`);
  }
  delete env.MYSQL_DEV_ROOT_PASSWORD;
  return env;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const environment = process.argv[2];
  if (!["development", "production"].includes(environment)) throw new Error("Choose development or production");
  const env = serverEnvironment(readFileSync(new URL(`../.env.${environment}`, import.meta.url), "utf8"), environment);
  // Read only this file so a production shell environment cannot select another database or key.
  const child = spawn(process.execPath, ["dist/server.js"], {
    cwd: new URL("..", import.meta.url), env, stdio: "inherit"
  });
  process.on("SIGINT", () => child.kill("SIGINT"));
  process.on("SIGTERM", () => child.kill("SIGTERM"));
  child.on("error", () => { console.error("Could not start server"); process.exitCode = 1; });
  child.on("exit", (code) => { process.exitCode = code ?? 1; });
}
