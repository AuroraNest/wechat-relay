import assert from "node:assert/strict";
import test from "node:test";
import { DEFAULT_RELAY_POLICY, relayActive, relayPolicyJson, validateRelayPolicy } from "./relay-policy.js";

test("relay policy pauses only inside the configured China-time window", () => {
  const policy = validateRelayPolicy({
    enabled: true,
    scheduleEnabled: true,
    weekdays: [1, 2, 3, 4, 5],
    start: "09:30",
    end: "18:00",
    timezone: "Asia/Shanghai",
  }, 123);
  assert.equal(relayActive(policy, new Date("2026-09-07T01:29:00Z")), true);
  assert.equal(relayActive(policy, new Date("2026-09-07T01:30:00Z")), false);
  assert.equal(relayActive(policy, new Date("2026-09-07T10:00:00Z")), true);
  assert.equal(relayActive(policy, new Date("2026-09-06T02:00:00Z")), true);
  assert.deepEqual(relayPolicyJson(policy, new Date("2026-09-07T02:00:00Z")), {
    enabled: true, scheduleEnabled: true, weekdays: [1, 2, 3, 4, 5], start: "09:30", end: "18:00",
    timezone: "Asia/Shanghai", active: false, updatedAt: 123,
  });
});

test("relay policy validates its trust-boundary fields", () => {
  assert.equal(relayActive(DEFAULT_RELAY_POLICY), true);
  assert.throws(() => validateRelayPolicy({ enabled: true, scheduleEnabled: true, weekdays: [1], start: "18:00", end: "09:30" }), /INVALID_RELAY_POLICY/);
  assert.throws(() => validateRelayPolicy({ enabled: true, scheduleEnabled: true, weekdays: [0], start: "09:30", end: "18:00" }), /INVALID_RELAY_POLICY/);
});
