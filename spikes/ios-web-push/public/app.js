const DB_NAME = "aurora-relay-phase0";
const STORE_NAME = "state";
const MESSAGE_STORE = "messages";
const PAIR_MESSAGE_STORE = "pairMessages";
const DEVICE_ID = "phase0-ios";
let selectedSender = null;
let loadingOlder = false;
let activeBlobUrls = [];
let viewerBlobUrl = null;
let visibleConversationMessages = 20;
let foregroundSyncInFlight = null;
let foregroundSyncPending = false;
let inboxStreamController = null;
let inboxStreamRestartRequested = false;
let inboxStreamRetryCount = 0;
const INBOX_RETRY_DELAYS = [0, 500, 1500, 3000];
const INBOX_STREAM_RETRY_DELAYS = [1000, 5000, 15000];
const REPLY_STATUS_DELAYS = [0, 1000, 2000, 4000, 8000, 16000];

class ApiError extends Error {
  constructor(status, code) {
    super(code);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.retryable = status === 429 || status >= 500;
  }
}

const elements = Object.fromEntries(
  ["imageDialog", "saveImage", "closeImage", "fullImage", "messages", "emptyMessages", "clearMessages", "settingsDialog", "openSettings", "closeSettings", "relayPolicyStatus", "relayEnabled", "relayScheduleEnabled", "relayScheduleFields", "relayStart", "relayEnd", "saveRelayPolicy", "status", "token", "body", "delay", "scenario", "network", "screen", "ios", "events", "subscribe", "createPairing", "pairingStatus", "copyPairing", "pairingCode", "send", "resend", "refresh", "unregister", "fault", "clearKey"]
    .map((id) => [id, document.getElementById(id)])
);

elements.token.value = sessionStorage.getItem("testToken") ?? "";
elements.token.addEventListener("input", () => {
  sessionStorage.setItem("testToken", elements.token.value);
  elements.token.removeAttribute("aria-invalid");
  if (elements.pairingStatus.dataset.state === "error") setPairingStatus("idle", "重复生成会直接替换旧配对.");
});
elements.subscribe.addEventListener("click", () => run(subscribe));
elements.createPairing.addEventListener("click", () => void runPairing());
elements.copyPairing.addEventListener("click", () => run(copyPairing));
elements.send.addEventListener("click", () => run(() => sendTest(false)));
elements.resend.addEventListener("click", () => run(() => sendTest(true)));
elements.refresh.addEventListener("click", () => run(refresh));
elements.unregister.addEventListener("click", () => run(unregisterWorker));
elements.fault.addEventListener("click", () => run(toggleFault));
elements.clearKey.addEventListener("click", () => run(clearKey));
elements.clearMessages.addEventListener("click", () => run(clearMessages));
elements.openSettings.addEventListener("click", () => {
  elements.settingsDialog.showModal();
  void run(loadRelayPolicy);
});
elements.closeSettings.addEventListener("click", () => elements.settingsDialog.close());
elements.relayScheduleEnabled.addEventListener("change", updateRelayScheduleControls);
elements.saveRelayPolicy.addEventListener("click", () => run(saveRelayPolicy));
elements.closeImage.addEventListener("click", () => elements.imageDialog.close());
elements.imageDialog.addEventListener("close", () => {
  if (viewerBlobUrl) URL.revokeObjectURL(viewerBlobUrl);
  viewerBlobUrl = null;
  elements.fullImage.removeAttribute("src");
  elements.saveImage.removeAttribute("href");
});
window.addEventListener("popstate", () => { selectedSender = null; void refresh(); });
window.visualViewport?.addEventListener("resize", updateChatViewport);
window.visualViewport?.addEventListener("scroll", updateChatViewport);
window.addEventListener("resize", updateChatViewport);
window.addEventListener("hashchange", () => void run(requestForegroundSync));
window.addEventListener("pageshow", () => { void run(requestForegroundSync); resumeInboxWatch(); });
window.addEventListener("focus", () => { void run(requestForegroundSync); resumeInboxWatch(); });
navigator.serviceWorker?.addEventListener("message", (event) => {
  if (event.data?.type === "INBOX_UPDATED") void run(async () => { if (event.data.pairId === await activePairId()) await refresh(); });
  if (event.data?.type === "INBOX_SYNC") void run(async () => { if (event.data.pairId === await activePairId()) await requestForegroundSync(); });
});

void navigator.serviceWorker?.register("/sw.js", { scope: "/" }).then((registration) => registration.update()).catch(() => undefined);
await run(markOpenedFromUrl);
await run(refresh);
await run(requestForegroundSync);
resumeInboxWatch();
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    void run(requestForegroundSync);
    resumeInboxWatch();
  } else {
    inboxStreamController?.abort();
  }
});

updateRelayScheduleControls();

async function loadRelayPolicy() {
  const pairId = await activePairId();
  const ackToken = await pairGet("ackToken", pairId);
  assertFeature(ackToken && pairId, "PAIRING_MISSING");
  renderRelayPolicy(await api("/api/v1/relay-policy", { headers: pairHeaders(ackToken, pairId) }));
}

async function saveRelayPolicy() {
  const pairId = await activePairId();
  const ackToken = await pairGet("ackToken", pairId);
  assertFeature(ackToken && pairId, "PAIRING_MISSING");
  const weekdays = [...document.querySelectorAll('[name="relayWeekday"]:checked')].map((input) => Number(input.value));
  if (elements.relayScheduleEnabled.checked) assertFeature(weekdays.length > 0, "请选择至少一天");
  const policy = await api("/api/v1/relay-policy", {
    method: "PUT",
    headers: pairHeaders(ackToken, pairId),
    body: {
      enabled: elements.relayEnabled.checked,
      scheduleEnabled: elements.relayScheduleEnabled.checked,
      weekdays: weekdays.length ? weekdays : [1, 2, 3, 4, 5],
      start: elements.relayStart.value,
      end: elements.relayEnd.value,
      timezone: "Asia/Shanghai"
    }
  });
  renderRelayPolicy(policy);
}

function renderRelayPolicy(policy) {
  elements.relayEnabled.checked = policy.enabled === true;
  elements.relayScheduleEnabled.checked = policy.scheduleEnabled === true;
  elements.relayStart.value = policy.start;
  elements.relayEnd.value = policy.end;
  const weekdays = new Set(policy.weekdays);
  for (const input of document.querySelectorAll('[name="relayWeekday"]')) input.checked = weekdays.has(Number(input.value));
  updateRelayScheduleControls();
  elements.relayPolicyStatus.dataset.state = policy.active ? "success" : "loading";
  elements.relayPolicyStatus.textContent = policy.active
    ? "当前正在转发."
    : policy.enabled ? `当前按计划暂停, ${policy.end} 后恢复.` : "当前已手动暂停.";
}

function updateRelayScheduleControls() {
  elements.relayScheduleFields.disabled = !elements.relayScheduleEnabled.checked;
}

function resumeInboxWatch() {
  inboxStreamRetryCount = 0;
  inboxStreamRestartRequested = true;
  if (inboxStreamController) inboxStreamController.abort();
  else {
    inboxStreamRestartRequested = false;
    void watchInbox();
  }
}

async function requestForegroundSync() {
  if (foregroundSyncInFlight) {
    foregroundSyncPending = true;
    return foregroundSyncInFlight;
  }
  foregroundSyncInFlight = (async () => {
    do {
      foregroundSyncPending = false;
      await syncForeground();
    } while (foregroundSyncPending);
  })();
  try {
    await foregroundSyncInFlight;
  } finally {
    foregroundSyncInFlight = null;
  }
}

function waitUntilVisible() {
  if (document.visibilityState === "visible") return Promise.resolve();
  return new Promise((resolve) => {
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        document.removeEventListener("visibilitychange", onVisibilityChange);
        resolve();
      }
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
  });
}

async function watchInbox() {
  await waitUntilVisible();
  if (inboxStreamController) return;
  const controller = new AbortController();
  inboxStreamController = controller;
  let reconnect = false;
  try {
    const pairId = await activePairId();
    const ackToken = await pairGet("ackToken", pairId);
    if (!ackToken || !pairId) return;
    const messages = await dbMessages(pairId);
    const afterSeq = messages.reduce((latest, message) => Math.max(latest, Number(message?.seq) || 0), 0);
    const response = await fetch(`/api/v1/messages/stream?afterSeq=${afterSeq}`, {
      signal: controller.signal,
      headers: { "X-AWR-Ack-Token": ackToken, "X-AWR-Pair-Id": pairId }
    });
    if (!response.ok) await responseJson(response);
    if (!response.body) throw new Error("INBOX_STREAM_UNAVAILABLE");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) throw new Error("INBOX_STREAM_CLOSED");
      inboxStreamRetryCount = 0;
      buffer += decoder.decode(chunk.value, { stream: true });
      const events = buffer.split("\n\n");
      buffer = events.pop() ?? "";
      for (const event of events) {
        if (event.split("\n").some((line) => line.startsWith("data:"))) await requestForegroundSync();
      }
    }
  } catch (error) {
    if (error?.name !== "AbortError" && isRetryableError(error) && inboxStreamRetryCount < INBOX_STREAM_RETRY_DELAYS.length) {
      const delay = INBOX_STREAM_RETRY_DELAYS[inboxStreamRetryCount++];
      reconnect = true;
      await new Promise((resolve) => setTimeout(resolve, delay));
    }
  } finally {
    controller.abort();
    inboxStreamController = null;
    const shouldRestart = reconnect || inboxStreamRestartRequested;
    inboxStreamRestartRequested = false;
    if (shouldRestart && document.visibilityState === "visible") void watchInbox();
  }
}

async function syncForeground() {
  let lastError;
  for (const delay of INBOX_RETRY_DELAYS) {
    if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
    if (document.visibilityState === "hidden") return;
    try {
      await markOpenedFromUrl();
      await syncInbox();
      await syncReplyStatuses();
      return;
    } catch (error) {
      if (!isRetryableError(error)) throw error;
      lastError = error;
    }
  }
  throw lastError;
}

async function subscribe() {
  const subscription = await ensurePushSubscription();
  const pairId = await activePairId();
  let key = await pairGet("messageKey", pairId);
  if (!key) {
    const raw = crypto.getRandomValues(new Uint8Array(32));
    key = await crypto.subtle.importKey("raw", raw, "AES-GCM", false, ["encrypt", "decrypt"]);
    if (pairId) await pairSet("messageKey", key, pairId);
    else await dbSet("pendingMessageKey", key);
    raw.fill(0);
  }

  const ackToken = await pairGet("ackToken", pairId);
  const result = await updateSubscription(subscription, ackToken, pairId);
  if (pairId) await pairSet("ackToken", result.ackToken, pairId);
  else await dbSet("pendingAckToken", result.ackToken);
  if (pairId) await refresh();
}

async function ensurePushSubscription() {
  assertFeature("serviceWorker" in navigator && "PushManager" in window && "Notification" in window, "PUSH_UNSUPPORTED");
  const permission = await Notification.requestPermission();
  assertFeature(permission === "granted", "NOTIFICATION_DENIED");

  const registration = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
  await navigator.serviceWorker.ready;
  const config = await api("/api/config");
  const pushManager = window.pushManager ?? registration.pushManager;
  let subscription = await pushManager.getSubscription();
  if (!subscription) {
    subscription = await pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: base64urlBytes(config.vapidPublicKey)
    });
  }
  return subscription;
}

async function updateSubscription(subscription, ackToken, pairId) {
  return api("/api/subscription", {
    method: "POST",
    body: subscription.toJSON(),
    token: true,
    headers: pairHeaders(ackToken, pairId)
  });
}

async function createPairing() {
  const subscription = await ensurePushSubscription();
  const a2iRaw = crypto.getRandomValues(new Uint8Array(32));
  const i2aRaw = crypto.getRandomValues(new Uint8Array(32));
  try {
    const a2iKey = await crypto.subtle.importKey("raw", a2iRaw, "AES-GCM", false, ["encrypt", "decrypt"]);
    const i2aKey = await crypto.subtle.importKey("raw", i2aRaw, "AES-GCM", false, ["encrypt", "decrypt"]);
    const previousPairId = await activePairId();
    let ackToken = previousPairId
      ? await pairGet("ackToken", previousPairId)
      : await dbGet("pendingAckToken");
    if (previousPairId) {
      assertFeature(ackToken, "ACK_TOKEN_MISSING");
    }
    if (!ackToken) {
      const session = await updateSubscription(subscription);
      ackToken = session.ackToken;
      await dbSet("pendingAckToken", ackToken);
    }
    assertFeature(ackToken, "ACK_TOKEN_MISSING");
    const pairing = await api("/api/v1/pairings", {
      method: "POST",
      token: true,
      headers: { "X-AWR-Ack-Token": ackToken },
      body: {}
    });
    await dbSetAll([
      ["activePairId", pairing.pairId],
      [pairStateKey(pairing.pairId, "messageKey"), a2iKey],
      [pairStateKey(pairing.pairId, "replyKey"), i2aKey],
      [pairStateKey(pairing.pairId, "ackToken"), ackToken]
    ]);
    await dbDelete("pendingMessageKey");
    await dbDelete("pendingAckToken");
    selectedSender = null;
    resumeInboxWatch();
    const code = `AWR1:${bytesBase64url(new TextEncoder().encode(JSON.stringify({
      v: 1,
      origin: location.origin,
      pairId: pairing.pairId,
      pairSecret: pairing.pairSecret,
      expiresAt: pairing.expiresAt,
      kA2I: bytesBase64url(a2iRaw),
      kI2A: bytesBase64url(i2aRaw)
    })))}`;
    elements.pairingCode.value = code;
    elements.pairingCode.hidden = false;
    elements.copyPairing.hidden = false;
    try {
      const updated = await updateSubscription(subscription, ackToken, pairing.pairId);
      await pairSet("ackToken", updated.ackToken, pairing.pairId);
      setPairingStatus("success", `配对码已生成, ${new Date(pairing.expiresAt).toLocaleTimeString("zh-CN")} 前有效. 请复制到 Android 设备.`);
    } catch (error) {
      setPairingStatus("error", `配对码已生成, 但通知订阅刷新失败: ${error instanceof Error ? error.message : String(error)}. 请先复制此码到 Android, 再重试订阅.`);
    }
  } finally {
    a2iRaw.fill(0);
    i2aRaw.fill(0);
  }
}

async function copyPairing() {
  assertFeature(elements.pairingCode.value.startsWith("AWR1:"), "PAIRING_CODE_MISSING");
  await navigator.clipboard.writeText(elements.pairingCode.value);
  setPairingStatus("success", "配对码已复制, 请立即粘贴到 Android 设备.");
}

async function runPairing() {
  if (!elements.token.value) {
    elements.token.setAttribute("aria-invalid", "true");
    setPairingStatus("error", "请先填写开发配对 Token, 再生成配对码.");
    elements.token.focus();
    return;
  }
  elements.createPairing.disabled = true;
  elements.createPairing.setAttribute("aria-busy", "true");
  setPairingStatus("loading", "正在开启通知、创建密钥并生成新配对码...");
  try {
    await createPairing();
  } catch (error) {
    setPairingStatus("error", `生成失败: ${error instanceof Error ? error.message : String(error)}`);
  } finally {
    elements.createPairing.disabled = false;
    elements.createPairing.removeAttribute("aria-busy");
  }
}

function setPairingStatus(state, message) {
  elements.pairingStatus.dataset.state = state;
  elements.pairingStatus.textContent = message;
}

async function sendTest(reuse) {
  const pairId = await activePairId();
  const ackToken = await pairGet("ackToken", pairId);
  assertFeature(ackToken && pairId, "PAIRING_MISSING");
  if (reuse) {
    const previous = await pairGet("lastPushRequest");
    assertFeature(previous, "NO_PREVIOUS_PUSH");
    const result = await api("/api/test-push", { method: "POST", token: true, headers: pairHeaders(ackToken, pairId), body: previous });
    elements.events.textContent = `已重发 ${result.messageId}, payload ${result.payloadBytes} bytes`;
    return refresh();
  }
  const key = await pairGet("messageKey");
  assertFeature(key instanceof CryptoKey, "PWA_KEY_MISSING");
  const messageId = uuidv7();
  const createdAt = Date.now();
  const seq = Number(await pairGet("seq") ?? 0) + 1;
  await pairSet("seq", seq);
  const aad = `AWR1|A2I|${messageId}|${DEVICE_ID}|${seq}|${createdAt}`;
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const plaintext = encodePreview(elements.body.value);
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv, additionalData: new TextEncoder().encode(aad), tagLength: 128 },
    key,
    plaintext
  );

  const request = {
    messageId,
    previewEnvelope: {
      alg: "A256GCM",
      kid: "phase0",
      iv: bytesBase64url(iv),
      ct: bytesBase64url(new Uint8Array(ciphertext)),
      aad
    },
    delaySeconds: Number(elements.delay.value || 0),
    context: {
      scenario: elements.scenario.value,
      network: elements.network.value,
      screenState: elements.screen.value,
      iosVersion: elements.ios.value || "unknown"
    }
  };
  await pairSet("lastPushRequest", request);
  const result = await api("/api/test-push", {
    method: "POST",
    token: true,
    headers: pairHeaders(ackToken, pairId),
    body: request
  });
  elements.events.textContent = `已排队 ${result.messageId}, payload ${result.payloadBytes} bytes`;
  await refresh();
}

async function refresh() {
  const registration = await navigator.serviceWorker?.getRegistration();
  const pushManager = window.pushManager ?? registration?.pushManager;
  const subscription = pushManager ? await pushManager.getSubscription() : null;
  const key = await pairGet("messageKey");
  const fault = Boolean(await pairGet("forceFailure"));
  const pairId = await activePairId();
  const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  renderMessages(await dbMessages());
  const rows = {
    "Home Screen": standalone ? "是" : "否",
    "通知权限": globalThis.Notification?.permission ?? "unsupported",
    "Service Worker": registration ? "已注册" : "未注册",
    "Push Subscription": subscription ? "存在" : "缺失",
    "当前 Pair": pairId ? pairId.slice(0, 8) : "未配对",
    "本地 CryptoKey": key instanceof CryptoKey ? `${key.extractable ? "可导出" : "不可导出"}` : "缺失",
    "SW 故障模式": fault ? "开启" : "关闭"
  };
  let serverStatus = null;
  if (elements.token.value) {
    const ackToken = await pairGet("ackToken", pairId);
    assertFeature(ackToken && pairId, "PAIRING_MISSING");
    serverStatus = await api("/api/status", { token: true, headers: pairHeaders(ackToken, pairId) });
    rows["Server Subscription"] = serverStatus.subscription;
  }
  elements.status.replaceChildren(...Object.entries(rows).flatMap(([name, value]) => {
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = name;
    dd.textContent = value;
    return [dt, dd];
  }));
  if (serverStatus) {
    elements.events.textContent = JSON.stringify({ events: serverStatus.events.slice(-20), messages: serverStatus.messages.slice(0, 20) }, null, 2);
  }
}

function updateChatViewport() {
  const viewport = window.visualViewport;
  // Pinch zoom keeps native browser panning; only follow the keyboard at normal scale.
  if (viewport && viewport.scale !== 1) return;
  const list = elements.messages.querySelector(".bubble-list");
  const atEnd = list && list.scrollHeight - list.scrollTop - list.clientHeight < 60;
  document.documentElement.style.setProperty("--chat-height", `${viewport?.height ?? window.innerHeight}px`);
  document.documentElement.style.setProperty("--chat-top", `${viewport?.offsetTop ?? 0}px`);
  // ponytail: a 100px viewport reduction identifies the keyboard; revisit if browser chrome exceeds this.
  document.body.classList.toggle("keyboard-open", Boolean(viewport && window.innerHeight - viewport.height > 100));
  if (atEnd) scrollChatToEnd();
}

function scrollChatToEnd() {
  const list = elements.messages.querySelector(".bubble-list");
  if (list) list.scrollTop = list.scrollHeight;
}

function renderMessages(value) {
  for (const url of activeBlobUrls) URL.revokeObjectURL(url);
  activeBlobUrls = [];
  const messages = Array.isArray(value) ? value : [];
  const groups = new Map();
  for (const message of messages) {
    const sender = String(message?.sender ?? "工作微信");
    const id = conversationId(message);
    const group = groups.get(id) ?? { id, sender, messages: [] };
    group.messages.push(message);
    groups.set(id, group);
  }
  const receivedAt = (message) => Number.isFinite(Number(message?.receivedAt)) ? Number(message.receivedAt) : 0;
  const orderedGroups = [...groups.values()]
    .map((group) => ({ ...group, messages: group.messages.sort((left, right) => receivedAt(right) - receivedAt(left)) }))
    .sort((left, right) => receivedAt(right.messages[0]) - receivedAt(left.messages[0]));
  document.body.classList.toggle("in-conversation", Boolean(selectedSender));
  updateChatViewport();
  if (selectedSender) return renderConversation(messages.filter((message) => conversationId(message) === selectedSender));
  elements.messages.replaceChildren(...orderedGroups.map((group) => {
    const latest = group.messages[0];
    const row = document.createElement("button");
    row.className = "conversation-row";
    row.type = "button";
    const avatar = document.createElement("span"); avatar.className = "avatar"; avatar.textContent = group.sender.slice(0, 1);
    const avatarAsset = (latest.assets ?? []).find((asset) => asset.kind === "avatar");
    if (avatarAsset) { const image = document.createElement("img"); image.alt = ""; image.dataset.assetId = avatarAsset.id; image.dataset.messageId = latest.messageId; image.dataset.deviceId = latest.deviceId; image.dataset.seq = String(latest.seq); image.dataset.createdAt = String(latest.createdAt ?? latest.receivedAt); image.dataset.wechatUserId = String(profileId(latest)); lazyAsset(image); avatar.replaceChildren(image); }
    const copy = document.createElement("span"); copy.className = "conversation-copy";
    const title = document.createElement("strong"); title.textContent = group.sender;
    const profile = document.createElement("span"); profile.className = "profile-badge"; profile.textContent = profileLabel(latest);
    const preview = document.createElement("small"); preview.textContent = String(latest.body ?? "");
    const time = document.createElement("time"); const at = receivedAt(latest); time.textContent = at ? new Date(at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }) : "";
    copy.append(title, preview, profile); row.append(avatar, copy, time);
    row.addEventListener("click", () => { selectedSender = group.id; visibleConversationMessages = 20; history.pushState({}, "", "#conversation"); renderMessages(messages); requestAnimationFrame(scrollChatToEnd); });
    return row;
  }));
  elements.emptyMessages.hidden = messages.length > 0;
  elements.clearMessages.hidden = messages.length === 0;
}

function renderConversation(messages) {
  const previousList = elements.messages.querySelector(".bubble-list");
  const previousInput = elements.messages.querySelector(".reply-composer input");
  const wasTyping = previousInput === document.activeElement;
  const draft = previousInput?.value ?? "";
  const selection = previousInput ? [previousInput.selectionStart, previousInput.selectionEnd] : null;
  const atEnd = !previousList || previousList.scrollHeight - previousList.scrollTop - previousList.clientHeight < 60;
  const previousScroll = previousList?.scrollTop ?? 0;
  const ordered = [...messages].sort((a, b) => messageTime(a) - messageTime(b)).slice(-visibleConversationMessages);
  const sender = String(messages[0]?.sender ?? "工作微信");
  const header = document.createElement("button"); header.className = "conversation-back"; header.type = "button"; header.textContent = `‹ ${sender} · ${profileLabel(messages[0])}`;
  header.addEventListener("click", () => { selectedSender = null; history.back(); });
  const list = document.createElement("div"); list.className = "bubble-list";
  list.addEventListener("scroll", () => { if (list.scrollTop < 80) void run(loadOlderForSelectedSender); }, { passive: true });
  let previousTime = 0;
  for (const message of ordered) {
    const at = messageTime(message);
    if (at && (!previousTime || at - previousTime > 5 * 60 * 1000)) {
      const time = document.createElement("time"); time.className = "chat-time";
      time.dateTime = new Date(at).toISOString();
      time.textContent = new Date(at).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
      list.append(time);
    }
    previousTime = at;
    const bubble = document.createElement("article");
    bubble.className = message.direction === "outgoing" ? "outgoing-bubble" : "incoming-bubble";
    const text = document.createElement("span"); text.textContent = String(message.body ?? ""); bubble.append(text);
    if (message.direction === "outgoing") {
      const state = document.createElement("small"); state.className = "reply-state"; state.dataset.replyId = message.messageId; state.textContent = replyStatusLabel(message.replyStatus); bubble.append(state);
    }
    list.append(bubble);
    for (const asset of (message.assets ?? []).filter((asset) => asset.kind !== "avatar")) {
      const image = document.createElement("img"); image.className = "message-media"; image.alt = asset.kind === "sticker" ? "表情" : "图片"; image.width = asset.width; image.height = asset.height;
      image.dataset.assetId = asset.id; image.dataset.messageId = message.messageId; image.dataset.deviceId = message.deviceId; image.dataset.seq = String(message.seq); image.dataset.createdAt = String(message.createdAt ?? message.receivedAt); image.dataset.wechatUserId = String(profileId(message));
      const media = document.createElement("div"); media.className = "media-entry";
      const open = document.createElement("button"); open.type = "button"; open.className = "open-image"; open.disabled = true; open.setAttribute("aria-label", "打开图片");
      const status = document.createElement("small"); status.className = "media-status"; status.textContent = "图片加载中...";
      open.append(image); media.append(open, status); list.append(media);
      lazyAsset(image, open, status);
    }
    if (!(message.assets ?? []).some((asset) => asset.kind !== "avatar") && /\[图片\]|\[动画表情\]/.test(String(message.body ?? ""))) {
      const hint = document.createElement("small"); hint.className = "media-status";
      hint.textContent = "微信通知未附带图片内容.";
      list.append(hint);
    }
  }
  const incoming = [...messages].filter((message) => message.direction !== "outgoing" && message.deviceId).sort((a, b) => messageTime(b) - messageTime(a));
  const latestIncoming = incoming[0];
  const conversationTarget = incoming.find((message) => message.replyCapable === true && message.conversationSendCapable === true);
  const target = conversationTarget ?? (latestIncoming?.replyCapable === true ? latestIncoming : null);
  const form = document.createElement("form"); form.className = "reply-composer";
  const input = document.createElement("input"); input.type = "text"; input.maxLength = 1000; input.placeholder = conversationTarget ? `发送到既有会话 ${sender}` : target ? `回复 ${sender}` : latestIncoming ? "当前微信通知未提供快捷回复" : "暂无可回复的消息"; input.autocomplete = "off"; input.enterKeyHint = "send"; input.value = draft; input.setAttribute("aria-label", conversationTarget ? `发送到既有会话 ${sender}` : `回复 ${sender}`); input.disabled = !target;
  const send = document.createElement("button"); send.type = "submit"; send.textContent = "发送"; send.disabled = !target;
  const feedback = document.createElement("p"); feedback.className = "reply-feedback"; feedback.setAttribute("aria-live", "polite");
  if (latestIncoming && !target) feedback.textContent = "当前微信通知未提供快捷回复";
  form.append(input, send, feedback);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = input.value.trim();
    if (!body || !target) return;
    input.disabled = true; send.disabled = true; feedback.textContent = "正在加密发送...";
    try {
      const reply = await queueReply(target, body);
      input.value = "";
      renderMessages(await dbMessages());
      requestAnimationFrame(scrollChatToEnd);
      void pollReplyStatus(reply.replyId);
    } catch (error) {
      renderMessages(await dbMessages());
      const currentFeedback = elements.messages.querySelector(".reply-feedback");
      if (currentFeedback) currentFeedback.textContent = `发送失败: ${error instanceof Error ? error.message : String(error)}`;
    }
  });
  elements.messages.replaceChildren(header, list, form);
  list.scrollTop = atEnd ? list.scrollHeight : previousScroll;
  if (wasTyping && !input.disabled) {
    input.focus({ preventScroll: true });
    if (selection) input.setSelectionRange(...selection);
  }
  elements.emptyMessages.hidden = true;
}

function messageTime(message) {
  const value = Number(message?.receivedAt ?? message?.createdAt ?? 0);
  return Number.isFinite(value) ? value : 0;
}

async function queueReply(target, body) {
  const pairId = await activePairId();
  const key = await pairGet("replyKey", pairId);
  const ackToken = await pairGet("ackToken", pairId);
  assertFeature(key instanceof CryptoKey, "REPLY_KEY_MISSING");
  assertFeature(ackToken && pairId, "PAIRING_MISSING");
  const id = uuidv7();
  const createdAt = Date.now();
  const wechatUserId = profileId(target);
  const conversationSend = target.conversationSendCapable === true;
  const aad = conversationSend
    ? `AWR1|I2A|3|CONVERSATION_SEND|${pairId}|${id}|${target.deviceId}|${target.messageId}|${createdAt}|${wechatUserId}`
    : `AWR1|I2A|${id}|${target.deviceId}|${target.messageId}|${createdAt}|${wechatUserId}`;
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv, additionalData: new TextEncoder().encode(aad), tagLength: 128 },
    key,
    new TextEncoder().encode(conversationSend ? JSON.stringify({ body, conversationTitle: String(target.sender) }) : JSON.stringify({ body }))
  );
  const request = {
    v: conversationSend ? 3 : 2,
    id,
    targetMessageId: target.messageId,
    deviceId: target.deviceId,
    wechatUserId,
    createdAt,
    replyEnvelope: { alg: "A256GCM", kid: "phase1-reply", iv: bytesBase64url(iv), aad, ct: bytesBase64url(new Uint8Array(ciphertext)) }
  };
  const local = { messageId: id, targetMessageId: target.messageId, deviceId: target.deviceId, wechatUserId, sender: target.sender, body, direction: "outgoing", replyStatus: "SUBMITTING", replyRequest: request, createdAt, receivedAt: createdAt };
  await dbPutMessage(local);
  try {
    const result = await submitStoredReply(request, ackToken, pairId);
    const accepted = { ...local, replyStatus: result.status }; delete accepted.replyRequest;
    await dbPutMessage(accepted);
    return { replyId: id, status: result.status, createdAt };
  } catch (error) {
    if (isRetryableError(error)) return { replyId: id, status: "SUBMITTING", createdAt };
    const failed = { ...local, replyStatus: permanentReplyFailure(error) };
    delete failed.replyRequest;
    await dbPutMessage(failed);
    throw error;
  }
}

async function syncReplyStatuses() {
  const messages = await dbMessages();
  const pending = messages.filter((message) => message.direction === "outgoing" && !terminalReplyStatus(message.replyStatus));
  for (const message of pending) {
    let current = message;
    if (current.replyStatus === "SUBMITTING" && current.replyRequest) {
      const pairId = await activePairId(); const ackToken = await pairGet("ackToken", pairId);
      if (!ackToken || !pairId) continue;
      try {
        const result = await submitStoredReply(current.replyRequest, ackToken, pairId);
        current = { ...current, replyStatus: result.status }; delete current.replyRequest;
        await dbPutMessage(current);
        const state = document.querySelector(`[data-reply-id="${current.messageId}"]`); if (state) state.textContent = replyStatusLabel(current.replyStatus);
      } catch (error) {
        if (isRetryableError(error)) continue;
        current = { ...current, replyStatus: permanentReplyFailure(error) };
        delete current.replyRequest;
        await dbPutMessage(current);
        const state = document.querySelector(`[data-reply-id="${current.messageId}"]`); if (state) state.textContent = replyStatusLabel(current.replyStatus);
        continue;
      }
    }
    if (!terminalReplyStatus(current.replyStatus)) {
      try {
        await syncReplyStatus(current.messageId);
      } catch (error) {
        if (isRetryableError(error)) continue;
        current = { ...current, replyStatus: permanentReplyFailure(error) };
        delete current.replyRequest;
        await dbPutMessage(current);
        const state = document.querySelector(`[data-reply-id="${current.messageId}"]`); if (state) state.textContent = replyStatusLabel(current.replyStatus);
      }
    }
  }
}

async function submitStoredReply(request, ackToken, pairId) {
  return api("/api/v1/replies", { method: "POST", headers: { "X-AWR-Ack-Token": ackToken, "X-AWR-Pair-Id": pairId }, body: request });
}

async function syncReplyStatus(replyId) {
  const pairId = await activePairId();
  const ackToken = await pairGet("ackToken", pairId);
  if (!ackToken || !pairId) return null;
  const response = await fetch(`/api/v1/replies/${encodeURIComponent(replyId)}`, { headers: { "X-AWR-Ack-Token": ackToken, "X-AWR-Pair-Id": pairId } });
  const reply = await responseJson(response);
  const message = await dbMessage(replyId);
  if (message && message.replyStatus !== reply.status) await dbPutMessage({ ...message, replyStatus: reply.status });
  const state = document.querySelector(`[data-reply-id="${replyId}"]`);
  if (state) state.textContent = replyStatusLabel(reply.status);
  return reply.status;
}

async function pollReplyStatus(replyId) {
  for (const delay of REPLY_STATUS_DELAYS) {
    if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
    await syncReplyStatuses().catch(() => undefined);
    const message = await dbMessage(replyId);
    if (terminalReplyStatus(message?.replyStatus)) return;
  }
}

function terminalReplyStatus(status) {
  return Boolean(status && status !== "SUBMITTING" && status !== "QUEUED" && status !== "DELIVERED_TO_ANDROID");
}

function replyStatusLabel(status) {
  return ({
    SUBMITTING: "等待网络",
    QUEUED: "正在发送",
    DELIVERED_TO_ANDROID: "Android 已接收",
    SENT_TO_WECHAT: "已交给微信",
    NOTIFICATION_NOT_ACTIVE: "原通知已失效",
    WECHAT_ACTION_CHANGED: "微信回复入口已变化",
    REMOTE_INPUT_UNSUPPORTED: "该通知不支持回复",
    REPLY_UNSUPPORTED: "当前微信通知未提供快捷回复",
    PENDING_INTENT_CANCELED: "微信已取消回复入口",
    INVALID_REPLY: "回复数据无效",
    FAILED: "发送失败"
  })[status] ?? "正在发送";
}

function lazyAsset(image, open, status) {
  const fail = (message) => { if (status) status.textContent = message; };
  const load = async () => {
    const pairId = await activePairId();
    const ackToken = await pairGet("ackToken", pairId);
    if (!ackToken || !pairId) return fail("请先连接设备后重试");
    const response = await fetch(`/api/v1/assets/${encodeURIComponent(image.dataset.assetId)}`, { headers: pairHeaders(ackToken, pairId) });
    if (!response.ok) return fail(response.status === 404 ? "图片已过期或不可用" : "图片加载失败, 请重新进入会话重试");
    const asset = await response.json();
    const legacyAad = `AWR1|A2I_ASSET|${image.dataset.assetId}|${image.dataset.deviceId}|${image.dataset.seq}|${image.dataset.createdAt}`;
    const expectedAad = `${legacyAad}|${Number(image.dataset.wechatUserId) === 999 ? 999 : 0}`;
    if (asset.id !== image.dataset.assetId || !["image/jpeg", "image/png", "image/webp"].includes(asset.mimeType) || asset.envelope.alg !== "A256GCM" || asset.envelope.kid !== "phase1-asset" || (asset.envelope.aad !== legacyAad && asset.envelope.aad !== expectedAad)) return fail("图片校验失败");
    const key = await pairGet("messageKey", pairId);
    if (!(key instanceof CryptoKey)) return fail("图片解密密钥不可用");
    const bytes = await crypto.subtle.decrypt({ name: "AES-GCM", iv: base64urlBytes(asset.envelope.iv), additionalData: new TextEncoder().encode(asset.envelope.aad), tagLength: 128 }, key, base64urlBytes(asset.envelope.ct));
    if (!image.isConnected || await activePairId() !== pairId) return;
    const blob = new Blob([bytes], { type: asset.mimeType });
    const url = URL.createObjectURL(blob); activeBlobUrls.push(url);
    image.onload = () => { if (status) status.textContent = ""; if (open) open.disabled = false; };
    image.onerror = () => fail("图片格式无法显示");
    image.src = url;
    if (open) open.onclick = () => {
      if (viewerBlobUrl) URL.revokeObjectURL(viewerBlobUrl);
      viewerBlobUrl = URL.createObjectURL(blob);
      elements.fullImage.src = viewerBlobUrl;
      elements.saveImage.href = viewerBlobUrl;
      elements.saveImage.download = `relay-image.${asset.mimeType === "image/png" ? "png" : asset.mimeType === "image/webp" ? "webp" : "jpg"}`;
      elements.imageDialog.showModal();
    };
  };
  const tryLoad = () => load().catch(() => fail("图片加载或解密失败, 请重新进入会话重试"));
  if (!("IntersectionObserver" in window)) { void tryLoad(); return; }
  const observer = new IntersectionObserver((entries) => { if (entries.some((entry) => entry.isIntersecting)) { observer.disconnect(); void tryLoad(); } }, { rootMargin: "160px" });
  observer.observe(image);
}

async function unregisterWorker() {
  const registration = await navigator.serviceWorker.getRegistration();
  if (registration) await registration.unregister();
  await refresh();
}

async function toggleFault() {
  await pairSet("forceFailure", !Boolean(await pairGet("forceFailure")));
  await refresh();
}

async function clearKey() {
  await pairDelete("messageKey");
  await refresh();
}

async function clearMessages() {
  await dbClearMessages();
  renderMessages([]);
}

async function markOpenedFromUrl() {
  const url = new URL(location.href);
  const fragment = new URLSearchParams(url.hash.slice(1));
  const messageId = fragment.get("messageId") ?? url.searchParams.get("messageId");
  if (!messageId) return;
  const pairId = fragment.get("pairId");
  await assertActivePushPair(pairId);
  const wechatUserId = parseWechatUserId(fragment.get("wechatUserId")) ?? 0;
  const serializedEnvelope = fragment.get("encryptedPreview") ?? fragment.get("previewEnvelope");
  if (serializedEnvelope) {
    const envelope = JSON.parse(serializedEnvelope);
    assertEnvelopeBinding(messageId, envelope, wechatUserId);
    const preview = await decryptPreview(envelope);
    await storeMessage({
      messageId,
      sender: String(preview.sender ?? "工作微信").slice(0, 120),
      body: String(preview.body ?? "").slice(0, 2000),
      wechatUserId,
      receivedAt: Date.now()
    });
    renderMessages(await dbMessages());
    url.hash = "";
    history.replaceState(null, "", `${url.pathname}${url.search}`);
  }
  const ackToken = await pairGet("ackToken", pairId);
  if (!ackToken || !pairId) return;
  await fetch("/api/acks", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...pairHeaders(ackToken, pairId) },
    body: JSON.stringify({ messageId, type: "OPENED" })
  });
}

async function syncInbox() {
  const pairId = await activePairId();
  const ackToken = await pairGet("ackToken", pairId);
  if (!ackToken || !pairId) return;
  const headers = pairHeaders(ackToken, pairId);
  const response = await fetch("/api/v1/messages", {
    headers
  });
  const body = await responseJson(response);
  if (body.pairId !== pairId) throw new Error("PAIRING_MISMATCH");
  assertFeature(Array.isArray(body.messages), "INVALID_MESSAGE_LIST");
  let decrypted = false;
  let failed = false;
  for (const message of [...body.messages].reverse()) {
    try {
      assertEnvelopeBinding(message.messageId, message.previewEnvelope, profileId(message));
      const preview = await decryptPreview(message.previewEnvelope);
      await storeMessage({
        messageId: message.messageId,
        deviceId: message.deviceId,
        createdAt: message.createdAt,
        sender: String(preview.sender ?? "工作微信").slice(0, 120),
        body: String(preview.body ?? "").slice(0, 2000),
        wechatUserId: profileId(message),
        replyCapable: message.replyCapable === true,
        conversationSendCapable: message.conversationSendCapable === true,
        seq: message.seq,
        receivedAt: message.receivedAt ?? message.createdAt,
        assets: message.assets ?? []
      });
      decrypted = true;
    } catch {
      failed = true;
    }
  }
  if (body.nextCursor !== undefined) await pairSet("nextCursor", body.nextCursor, pairId);
  renderMessages(await dbMessages());
  if (failed) throw new Error("MESSAGE_DECRYPT_FAILED");
}

function assertEnvelopeBinding(messageId, envelope, wechatUserId) {
  const parts = typeof envelope?.aad === "string" ? envelope.aad.split("|") : [];
  const legacy = parts.length === 6 && parts[0] === "AWR1" && parts[1] === "A2I" && parts[2] === messageId && parts[3] && parts[4] && parts[5] && (wechatUserId === undefined || wechatUserId === 0);
  const profile = parts.length === 7 && parts[0] === "AWR1" && parts[1] === "A2I" && parts[2] === messageId && parts[3] && parts[4] && parts[5] && (parts[6] === "0" || parts[6] === "999") && (wechatUserId === undefined || parts[6] === String(wechatUserId));
  if (envelope?.alg !== "A256GCM" || envelope?.kid !== "phase1" || (!legacy && !profile)) {
    throw new Error("INVALID_ENVELOPE_BINDING");
  }
}

async function decryptPreview(envelope) {
  const key = await pairGet("messageKey");
  assertFeature(key instanceof CryptoKey, "PWA_KEY_MISSING");
  const plaintext = await crypto.subtle.decrypt(
    {
      name: "AES-GCM",
      iv: base64urlBytes(envelope.iv),
      additionalData: new TextEncoder().encode(envelope.aad),
      tagLength: 128
    },
    key,
    base64urlBytes(envelope.ct)
  );
  return JSON.parse(new TextDecoder().decode(plaintext));
}

async function storeMessage(message) {
  await dbPutMessage(message);
}

async function loadOlderForSelectedSender() {
  if (loadingOlder || !selectedSender) return;
  loadingOlder = true;
  const selectedAtStart = selectedSender;
  try {
    const currentMessages = await dbMessages();
    if (selectedSender !== selectedAtStart) return;
    if (currentMessages.filter((message) => conversationId(message) === selectedSender).length > visibleConversationMessages) {
      const list = elements.messages.querySelector(".bubble-list");
      const anchor = list.scrollHeight - list.scrollTop;
      visibleConversationMessages += 20;
      renderMessages(currentMessages);
      const updated = elements.messages.querySelector(".bubble-list");
      updated.scrollTop = updated.scrollHeight - anchor;
      return;
    }
    const pairId = await activePairId();
    const cursor = await pairGet("nextCursor", pairId);
    if (!cursor) return;
    if (selectedSender !== selectedAtStart) return;
    const sender = selectedSender;
    const list = elements.messages.querySelector(".bubble-list");
    const anchor = list.scrollHeight - list.scrollTop;
    let beforeSeq = cursor;
    let added = 0;
    while (beforeSeq && added < 20) {
      const ackToken = await pairGet("ackToken", pairId);
      if (!ackToken || !pairId) throw new Error("PAIRING_MISSING");
      const response = await fetch(`/api/v1/messages?beforeSeq=${encodeURIComponent(beforeSeq)}`, { headers: pairHeaders(ackToken, pairId) });
      const page = await responseJson(response);
      if (page.pairId !== pairId) throw new Error("PAIRING_MISMATCH");
      for (const message of [...page.messages].reverse()) { try { assertEnvelopeBinding(message.messageId, message.previewEnvelope, profileId(message)); const preview = await decryptPreview(message.previewEnvelope); const sender = String(preview.sender ?? "工作微信").slice(0, 120); const local = { messageId: message.messageId, deviceId: message.deviceId, seq: message.seq, createdAt: message.createdAt, sender, body: String(preview.body ?? "").slice(0, 2000), wechatUserId: profileId(message), replyCapable: message.replyCapable === true, conversationSendCapable: message.conversationSendCapable === true, receivedAt: message.receivedAt ?? message.createdAt, assets: message.assets ?? [] }; await storeMessage(local); if (conversationId(local) === selectedSender) added++; } catch {} }
      beforeSeq = page.nextCursor; await pairSet("nextCursor", beforeSeq, pairId);
    }
    if (selectedSender !== sender) return;
    visibleConversationMessages += Math.min(20, added);
    const messages = await dbMessages();
    if (selectedSender !== sender) return;
    renderMessages(messages);
    const updated = elements.messages.querySelector(".bubble-list");
    updated.scrollTop = Math.max(0, updated.scrollHeight - anchor);
  } finally { loadingOlder = false; }
}

function encodePreview(body) {
  const encoder = new TextEncoder();
  const characters = [...body];
  while (characters.length) {
    const encoded = encoder.encode(JSON.stringify({ sender: "Phase 0 测试", body: characters.join(""), wechatUserId: 0 }));
    if (encoded.byteLength <= 600) return encoded;
    characters.pop();
  }
  return encoder.encode(JSON.stringify({ sender: "Phase 0 测试", body: "", wechatUserId: 0 }));
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers ?? {}) };
  if (options.token) {
    assertFeature(elements.token.value, "TEST_TOKEN_MISSING");
    headers["X-AWR-Test-Token"] = elements.token.value;
  }
  const response = await fetch(path, {
    method: options.method ?? "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body)
  });
  return responseJson(response);
}

async function responseJson(response) {
  const status = response.status;
  const text = await response.text();
  let body = null;
  if (text) {
    try { body = JSON.parse(text); } catch {}
  }
  if (!response.ok) throw new ApiError(status, typeof body?.error === "string" ? body.error : `HTTP_${status}`);
  if (!body || typeof body !== "object") throw new Error("INVALID_JSON_RESPONSE");
  return body;
}

function isRetryableError(error) {
  return !(error instanceof ApiError) || error.retryable;
}

function permanentReplyFailure(error) {
  return error instanceof ApiError && error.code === "REPLY_UNSUPPORTED" ? "REPLY_UNSUPPORTED" : "FAILED";
}

async function run(action) {
  try {
    await action();
  } catch (error) {
    elements.events.textContent = `错误: ${error instanceof Error ? error.message : String(error)}`;
  }
}

function assertFeature(condition, code) {
  if (!condition) throw new Error(code);
}

function base64urlBytes(value) {
  const padding = "=".repeat((4 - value.length % 4) % 4);
  return Uint8Array.from(atob((value + padding).replaceAll("-", "+").replaceAll("_", "/")), (c) => c.charCodeAt(0));
}

function bytesBase64url(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function uuidv7() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  let timestamp = BigInt(Date.now());
  for (let index = 5; index >= 0; index--) {
    bytes[index] = Number(timestamp & 0xffn);
    timestamp >>= 8n;
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x70;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function pairStateKey(pairId, key) { return `pair:${pairId}:${key}`; }
function messageStorageId(pairId, messageId) { return `${pairId}:${messageId}`; }
function profileId(message) { return Number(message?.wechatUserId) === 999 ? 999 : 0; }
function profileLabel(message) { return profileId(message) === 999 ? "微信 999" : "微信 0"; }
function parseWechatUserId(value) {
  if (value === undefined || value === null || value === "") return undefined;
  if (value === 0 || value === "0") return 0;
  if (value === 999 || value === "999") return 999;
  throw new Error("INVALID_PROFILE");
}
function conversationId(message) { return `${profileId(message)}:${String(message?.sender ?? "工作微信")}`; }
function pairHeaders(ackToken, pairId) {
  return ackToken && pairId ? { "X-AWR-Ack-Token": ackToken, "X-AWR-Pair-Id": pairId } : {};
}
async function activePairId() {
  const pairId = await dbGet("activePairId");
  return typeof pairId === "string" ? pairId : undefined;
}
async function pairGet(key, pairId) {
  const targetPairId = pairId ?? await activePairId();
  return targetPairId ? dbGet(pairStateKey(targetPairId, key)) : undefined;
}
async function pairSet(key, value, pairId) {
  const targetPairId = pairId ?? await activePairId();
  if (!targetPairId) throw new Error("PAIRING_MISSING");
  return dbSet(pairStateKey(targetPairId, key), value);
}
async function pairDelete(key, pairId) {
  const targetPairId = pairId ?? await activePairId();
  if (!targetPairId) return;
  return dbDelete(pairStateKey(targetPairId, key));
}
async function assertActivePushPair(pairId) {
  const activePair = await activePairId();
  if (!pairId || pairId !== activePair) throw new Error("PAIRING_MISMATCH");
  return pairId;
}

function openDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 3);
    request.onupgradeneeded = () => {
      const db = request.result;
      const state = db.objectStoreNames.contains(STORE_NAME) ? request.transaction.objectStore(STORE_NAME) : db.createObjectStore(STORE_NAME);
      const messages = db.objectStoreNames.contains(MESSAGE_STORE) ? request.transaction.objectStore(MESSAGE_STORE) : db.createObjectStore(MESSAGE_STORE, { keyPath: "messageId" });
      const pairMessages = db.objectStoreNames.contains(PAIR_MESSAGE_STORE) ? request.transaction.objectStore(PAIR_MESSAGE_STORE) : db.createObjectStore(PAIR_MESSAGE_STORE, { keyPath: "id" });
      if (request.oldVersion < 2) { const legacy = state.get("inbox"); legacy.onsuccess = () => { if (Array.isArray(legacy.result)) for (const item of legacy.result) if (item?.messageId) messages.put(item); state.delete("inbox"); }; }
      if (request.oldVersion < 3) {
        const active = state.get("activePairId");
        active.onsuccess = () => {
          const pairId = active.result;
          if (typeof pairId !== "string") return;
          for (const key of ["ackToken", "messageKey", "replyKey", "nextCursor", "seq", "lastPushRequest", "forceFailure"]) {
            const value = state.get(key);
            value.onsuccess = () => { if (value.result !== undefined) state.put(value.result, pairStateKey(pairId, key)); };
          }
          const legacyMessages = messages.getAll();
          legacyMessages.onsuccess = () => {
            for (const message of legacyMessages.result) {
              if (message?.messageId) pairMessages.put({ ...message, id: messageStorageId(pairId, message.messageId), pairId, wechatUserId: profileId(message) });
            }
          };
        };
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function dbMessages(pairId) { const targetPairId = pairId ?? await activePairId(); if (!targetPairId) return []; const db = await openDb(); return new Promise((resolve, reject) => { const request = db.transaction(PAIR_MESSAGE_STORE).objectStore(PAIR_MESSAGE_STORE).getAll(); request.onsuccess = () => resolve(request.result.filter((message) => message.pairId === targetPairId)); request.onerror = () => reject(request.error); }); }
async function dbMessage(messageId, pairId) { const targetPairId = pairId ?? await activePairId(); if (!targetPairId) return undefined; const db = await openDb(); return new Promise((resolve, reject) => { const request = db.transaction(PAIR_MESSAGE_STORE).objectStore(PAIR_MESSAGE_STORE).get(messageStorageId(targetPairId, messageId)); request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); }); }
async function dbPutMessage(message, pairId) { const targetPairId = pairId ?? await activePairId(); if (!targetPairId) throw new Error("PAIRING_MISSING"); const db = await openDb(); return new Promise((resolve, reject) => { const tx = db.transaction(PAIR_MESSAGE_STORE, "readwrite"); tx.objectStore(PAIR_MESSAGE_STORE).put({ ...message, id: messageStorageId(targetPairId, message.messageId), pairId: targetPairId, wechatUserId: profileId(message) }); tx.oncomplete = () => resolve(); tx.onerror = () => reject(tx.error); }); }
async function dbClearMessages(pairId) { const targetPairId = pairId ?? await activePairId(); if (!targetPairId) return; const messages = await dbMessages(targetPairId); const db = await openDb(); return new Promise((resolve, reject) => { const tx = db.transaction(PAIR_MESSAGE_STORE, "readwrite"); const store = tx.objectStore(PAIR_MESSAGE_STORE); for (const message of messages) store.delete(message.id); tx.oncomplete = () => resolve(); tx.onerror = () => reject(tx.error); }); }

async function dbGet(key) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const request = db.transaction(STORE_NAME).objectStore(STORE_NAME).get(key);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function dbSet(key, value) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(STORE_NAME, "readwrite");
    transaction.objectStore(STORE_NAME).put(value, key);
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error);
  });
}

async function dbSetAll(entries) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(STORE_NAME, "readwrite");
    const store = transaction.objectStore(STORE_NAME);
    for (const [key, value] of entries) store.put(value, key);
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error);
  });
}

async function dbDelete(key) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(STORE_NAME, "readwrite");
    transaction.objectStore(STORE_NAME).delete(key);
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error);
  });
}
