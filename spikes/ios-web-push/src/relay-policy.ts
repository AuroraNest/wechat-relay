export const DEFAULT_RELAY_POLICY: RelayPolicy = {
  enabled: true,
  scheduleEnabled: false,
  weekdaysMask: 31,
  startMinutes: 9 * 60 + 30,
  endMinutes: 18 * 60,
  updatedAt: 0,
};

export interface RelayPolicy {
  enabled: boolean;
  scheduleEnabled: boolean;
  weekdaysMask: number;
  startMinutes: number;
  endMinutes: number;
  updatedAt: number;
}

export interface RelayPolicyJson {
  enabled: boolean;
  scheduleEnabled: boolean;
  weekdays: number[];
  start: string;
  end: string;
  timezone: "Asia/Shanghai";
  active: boolean;
  updatedAt: number;
}

export function validateRelayPolicy(value: unknown, updatedAt = Date.now()): RelayPolicy {
  if (!value || typeof value !== "object") throw new Error("INVALID_RELAY_POLICY");
  const candidate = value as Record<string, unknown>;
  if (typeof candidate.enabled !== "boolean" || typeof candidate.scheduleEnabled !== "boolean")
    throw new Error("INVALID_RELAY_POLICY");
  if (!Array.isArray(candidate.weekdays) || candidate.weekdays.length === 0)
    throw new Error("INVALID_RELAY_POLICY");
  const weekdays = [...new Set(candidate.weekdays)];
  if (weekdays.some((day) => !Number.isInteger(day) || Number(day) < 1 || Number(day) > 7))
    throw new Error("INVALID_RELAY_POLICY");
  const startMinutes = parseTime(candidate.start);
  const endMinutes = parseTime(candidate.end);
  if (startMinutes >= endMinutes || (candidate.timezone !== undefined && candidate.timezone !== "Asia/Shanghai"))
    throw new Error("INVALID_RELAY_POLICY");
  return {
    enabled: candidate.enabled,
    scheduleEnabled: candidate.scheduleEnabled,
    weekdaysMask: weekdays.reduce((mask, day) => mask | (1 << (Number(day) - 1)), 0),
    startMinutes,
    endMinutes,
    updatedAt,
  };
}

export function relayActive(policy: RelayPolicy, now = new Date()): boolean {
  if (!policy.enabled) return false;
  if (!policy.scheduleEnabled) return true;
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Shanghai",
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(now).map(({ type, value }) => [type, value]));
  const day = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].indexOf(parts.weekday!) + 1;
  const minutes = Number(parts.hour) * 60 + Number(parts.minute);
  return (policy.weekdaysMask & (1 << (day - 1))) === 0 || minutes < policy.startMinutes || minutes >= policy.endMinutes;
}

export function relayPolicyJson(policy: RelayPolicy, now = new Date()): RelayPolicyJson {
  return {
    enabled: policy.enabled,
    scheduleEnabled: policy.scheduleEnabled,
    weekdays: Array.from({ length: 7 }, (_, index) => index + 1).filter((day) => (policy.weekdaysMask & (1 << (day - 1))) !== 0),
    start: formatTime(policy.startMinutes),
    end: formatTime(policy.endMinutes),
    timezone: "Asia/Shanghai",
    active: relayActive(policy, now),
    updatedAt: policy.updatedAt,
  };
}

function parseTime(value: unknown): number {
  if (typeof value !== "string" || !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value)) throw new Error("INVALID_RELAY_POLICY");
  const [hour, minute] = value.split(":").map(Number);
  return hour! * 60 + minute!;
}

function formatTime(minutes: number): string {
  return `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
}
