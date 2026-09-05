import { randomBytes } from "node:crypto";
import { spawn } from "node:child_process";
import webpush from "web-push";

const keys = webpush.generateVAPIDKeys();
const child = spawn(process.execPath, ["dist/server.js"], {
  env: {
    ...process.env,
    PORT: "18080",
    PUBLIC_ORIGIN: "http://127.0.0.1:18080",
    PUBLIC_DIR: new URL("../public", import.meta.url).pathname,
    DATA_DIR: new URL("../data/local", import.meta.url).pathname,
    TEST_TOKEN: "local-test-token",
    STORAGE_KEY: randomBytes(32).toString("base64url"),
    VAPID_PUBLIC_KEY: keys.publicKey,
    VAPID_PRIVATE_KEY: keys.privateKey,
    BUILD_VERSION: "local"
  },
  stdio: "inherit"
});

process.on("SIGINT", () => child.kill("SIGINT"));
process.on("SIGTERM", () => child.kill("SIGTERM"));
child.on("exit", (code) => process.exit(code ?? 0));
