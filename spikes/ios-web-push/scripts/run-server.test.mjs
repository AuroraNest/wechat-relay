import assert from "node:assert/strict";
import test from "node:test";
import { serverEnvironment } from "./run-server.mjs";

test("development rejects production targets and keeps file keys stable", () => {
  const config = {
    NODE_ENV: "development", MYSQL_HOST: "127.0.0.1", MYSQL_PORT: "23306",
    MYSQL_DATABASE: "wechat_relay_dev", DATA_DIR: "data/development", PUBLIC_ORIGIN: "http://127.0.0.1:18080",
    MYSQL_USER: "development", MYSQL_PASSWORD: "test-only", TEST_TOKEN: "test-only",
    STORAGE_KEY: "test-only", VAPID_PUBLIC_KEY: "test-only", VAPID_PRIVATE_KEY: "test-only"
  };
  const encode = (env) => Object.entries(env).map(([key, value]) => `${key}=${value}`).join("\n");
  assert.deepEqual(serverEnvironment(encode(config), "development"), config);
  assert.deepEqual(serverEnvironment(encode({ ...config, MYSQL_DEV_ROOT_PASSWORD: "test-only" }), "development"), config);
  assert.deepEqual(serverEnvironment(encode(config), "development"), serverEnvironment(encode(config), "development"));
  for (const change of [
    { NODE_ENV: "production" }, { MYSQL_HOST: "db.example.com" }, { MYSQL_PORT: "3306" },
    { MYSQL_DATABASE: "production" }, { DATA_DIR: "/data" }, { REDIS_URL: "redis://localhost" },
    { STORAGE_KEY: "" }, { MYSQL_PASSWORD: "" }
  ]) assert.throws(() => serverEnvironment(encode({ ...config, ...change }), "development"));
  const production = { ...config, NODE_ENV: "production", PUBLIC_ORIGIN: "https://relay.example.com" };
  assert.deepEqual(serverEnvironment(encode(production), "production"), production);
  assert.throws(() => serverEnvironment(encode(production), "development"));
  assert.throws(() => serverEnvironment(encode(config), "production"));
  assert.throws(() => serverEnvironment(encode(config), "unknown"));
  assert.throws(() => serverEnvironment(encode({ ...production, PUBLIC_ORIGIN: "http://relay.example.com" }), "production"));
});
