package com.aurora.tabletrelay;

import android.app.Instrumentation;
import android.app.KeyguardManager;
import android.app.UiAutomation;
import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.AccessibilityServiceInfo;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Rect;
import android.os.Bundle;
import android.os.Process;
import android.os.SystemClock;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import android.view.accessibility.AccessibilityManager;
import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;

/** Shell-owned instrumentation. Payloads stay in private app files, never am arguments. */
public final class ReplyInstrumentation extends Instrumentation {
    private static final String WECHAT = "com.tencent.mm";
    private static final String EXPECTED_VERSION = "8.0.78";
    private UiAutomation automation;
    private boolean clicked;
    private long deadline;
    private int originalAccessibilityFlags;
    private AccessibilityNodeInfo draftComposer;
    private String draftBody;
    private String draftTitle;
    private String stage = "request";
    private String diagnosticAlias = "";
    private String diagnosticTitle = "";

    @Override public void onCreate(Bundle arguments) { super.onCreate(arguments); start(); }

    @Override public void onStart() {
        JSONObject result;
        String operation = "";
        try {
            File input = new File(getContext().getFilesDir(), "command.json");
            if (input.length() < 2 || input.length() > 16384) throw new Gate("INVALID_REQUEST");
            byte[] bytes;
            try (FileInputStream stream = new FileInputStream(input)) { bytes = stream.readAllBytes(); }
            JSONObject command = new JSONObject(new String(bytes, StandardCharsets.UTF_8));
            deadline = SystemClock.uptimeMillis() + 60000;
            require(command.optInt("v") == 1, "INVALID_REQUEST");
            require(Process.myUid() / 100000 == 0, "AUTOMATION_NOT_READY");
            require(EXPECTED_VERSION.equals(getContext().getPackageManager().getPackageInfo(WECHAT, 0).versionName), "AUTOMATION_NOT_READY");
            operation = command.optString("operation");
            if (operation.equals("probe")) {
                result = new JSONObject().put("status", "READY").put("protocol", 2).put("recipientProof", "wechat-profile-alias");
            } else {
                stage = "window";
                automation = getUiAutomation();
                AccessibilityServiceInfo info = automation.getServiceInfo();
                originalAccessibilityFlags = info.flags;
                info.flags |= AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS | AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
                    | AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS;
                automation.setServiceInfo(info);
                automation.clearCache();
                KeyguardManager keyguard = (KeyguardManager) getContext().getSystemService(Context.KEYGUARD_SERVICE);
                require(keyguard != null && !keyguard.isKeyguardLocked(), "AUTOMATION_NOT_READY");
                if (operation.equals("inspect")) {
                    List<AccessibilityNodeInfo> nodes = nodes();
                    Set<String> ids = new HashSet<>();
                    int editable = 0;
                    for (AccessibilityNodeInfo node : nodes) {
                        if (node.getViewIdResourceName() != null) ids.add(node.getViewIdResourceName());
                        if (node.isEditable()) editable++;
                    }
                    // Counts describe availability without exposing titles, account IDs or message text.
                    result = new JSONObject().put("status", "INSPECTED").put("nodes", nodes.size())
                        .put("resourceIds", ids.size()).put("editable", editable).put("clicked", false);
                    AccessibilityNodeInfo current = root();
                    Rect bounds = new Rect();
                    current.getBoundsInScreen(bounds);
                    result.put("rootVisible", current.isVisibleToUser()).put("rootChildren", current.getChildCount())
                        .put("rootWidth", bounds.width()).put("rootHeight", bounds.height());
                    AccessibilityManager manager = (AccessibilityManager) getContext().getSystemService(Context.ACCESSIBILITY_SERVICE);
                    result.put("touchExplorationEnabled", manager != null && manager.isTouchExplorationEnabled());
                } else if (operation.equals("inspect-recipient")) {
                    stage = "recipient_request";
                    String alias = command.optString("alias");
                    String title = command.optString("conversationName");
                    diagnosticAlias = alias;
                    diagnosticTitle = title;
                    require(!alias.isEmpty() && alias.length() <= 128 && !title.isEmpty(), "INVALID_REQUEST");
                    // This diagnostic stops after the same recipient gates used by send.
                    navigate(alias, title);
                    result = result("RECIPIENT_VERIFIED", false);
                } else if (operation.equals("download-image")) {
                    result = downloadImage(command, digest(new String(bytes, StandardCharsets.UTF_8)));
                } else {
                    require(operation.equals("send"), "INVALID_REQUEST");
                    result = send(command, digest(new String(bytes, StandardCharsets.UTF_8)));
                }
            }
        } catch (Gate failure) {
            String failedStage = stage;
            clearUnsentDraft();
            result = result(clicked ? operation.equals("download-image") ? "DOWNLOAD_UNCONFIRMED" : "SEND_UNCONFIRMED" : failure.code, clicked, failedStage);
            attachRecipientDiagnostic(result, operation);
        } catch (Exception failure) {
            String failedStage = stage;
            clearUnsentDraft();
            result = result(clicked ? operation.equals("download-image") ? "DOWNLOAD_UNCONFIRMED" : "SEND_UNCONFIRMED" : "AUTOMATION_NOT_READY", clicked, failedStage);
            attachRecipientDiagnostic(result, operation);
        } finally {
            if (automation != null) {
                AccessibilityServiceInfo info = automation.getServiceInfo();
                info.flags = originalAccessibilityFlags;
                automation.setServiceInfo(info);
            }
        }
        try {
            File output = new File(getContext().getFilesDir(), "result.json");
            try (FileOutputStream stream = new FileOutputStream(output)) {
                stream.write(result.toString().getBytes(StandardCharsets.UTF_8));
                stream.getFD().sync();
            }
        } catch (Exception ignored) { }
        finish(0, new Bundle());
    }

    private void attachRecipientDiagnostic(JSONObject result, String operation) {
        if (!operation.equals("inspect-recipient")) return;
        try {
            AccessibilityNodeInfo current = automation.getRootInActiveWindow();
            boolean wechatRoot = current != null && WECHAT.contentEquals(current.getPackageName());
            List<AccessibilityWindowInfo> windows = automation.getWindows();
            int wechatWindows = 0;
            for (AccessibilityWindowInfo window : windows) {
                AccessibilityNodeInfo windowRoot = window.getRoot();
                if (windowRoot != null && WECHAT.contentEquals(windowRoot.getPackageName())) wechatWindows++;
            }
            Rect rootBounds = new Rect();
            if (current != null) current.getBoundsInScreen(rootBounds);
            result.put("diagnosticRoot", new JSONObject().put("rootNull", current == null)
                .put("packageIsWeChat", wechatRoot).put("windowCount", windows.size()).put("wechatWindowCount", wechatWindows)
                .put("rootChildren", current == null ? 0 : current.getChildCount())
                .put("width", rootBounds.width()).put("height", rootBounds.height())
                .put("visible", current != null && current.isVisibleToUser()));
            JSONArray diagnostic = new JSONArray();
            List<AccessibilityNodeInfo> pending = new ArrayList<>();
            if (wechatRoot) pending.add(current);
            for (int index = 0; index < pending.size(); index++) {
                AccessibilityNodeInfo node = pending.get(index);
                Rect bounds = new Rect();
                node.getBoundsInScreen(bounds);
                diagnostic.put(new JSONObject()
                    .put("class", node.getClassName() == null ? "" : node.getClassName().toString())
                    .put("resourceId", node.getViewIdResourceName() == null ? "" : node.getViewIdResourceName())
                    .put("bounds", new JSONArray().put(bounds.left).put(bounds.top).put(bounds.right).put(bounds.bottom))
                    .put("clickable", node.isClickable()).put("editable", node.isEditable()).put("visible", node.isVisibleToUser())
                    .put("text", diagnosticText(text(node)))
                    .put("description", diagnosticText(node.getContentDescription() == null ? "" : node.getContentDescription().toString())));
                for (int child = 0; child < node.getChildCount() && pending.size() < 300; child++) {
                    AccessibilityNodeInfo entry = node.getChild(child);
                    if (entry != null) pending.add(entry);
                }
            }
            result.put("diagnostic", diagnostic);
        } catch (Exception ignored) {
            // Diagnostic availability cannot replace the original failure or its stage.
        }
    }

    private Object diagnosticText(String value) throws Exception {
        switch (value) {
            case "搜索": case "返回": case "更多信息": case "聊天信息": case "聊天详情":
            case "发消息": case "微信": case "通讯录": case "发送":
            case "Search": case "Back": case "More Information": case "Chat Info":
            case "Message": case "Send Message": case "WeChat": case "Contacts": case "Send":
                return value;
            default:
                return new JSONObject().put("textLength", value.length())
                    .put("isExpectedAlias", !diagnosticAlias.isEmpty() && value.equals(diagnosticAlias))
                    .put("isExpectedTitle", !diagnosticTitle.isEmpty() && value.equals(diagnosticTitle));
        }
    }

    private JSONObject downloadImage(JSONObject command, String hash) throws Exception {
        // The guest opens a fresh official viewer using verified source IDs.
        // This button is pinned to WeChat 8.0.78; no label or screen-index fallback.
        AccessibilityNodeInfo button = null;
        for (int attempt = 0; attempt < 12; attempt++) {
            List<AccessibilityNodeInfo> candidates = new ArrayList<>();
            for (AccessibilityNodeInfo node : nodes()) {
                if ("com.tencent.mm:id/cnb".equals(node.getViewIdResourceName())
                    && node.isEnabled() && node.isClickable()
                    && "android.widget.Button".contentEquals(node.getClassName())) candidates.add(node);
            }
            require(candidates.size() <= 1, "WECHAT_ACTION_CHANGED");
            if (candidates.size() == 1) { button = candidates.get(0); break; }
            pause();
        }
        if (button == null) return result("IMAGE_DOWNLOAD_NOT_AVAILABLE", false);
        int window = button.getWindowId();
        JSONObject grant = sourceGrant(command, hash);
        require(button.refresh() && button.getWindowId() == window && button.isVisibleToUser()
            && button.isEnabled() && button.isClickable()
            && "com.tencent.mm:id/cnb".equals(button.getViewIdResourceName()), "WECHAT_ACTION_CHANGED");
        require(grant.getLong("expiresAt") > System.currentTimeMillis(), "AUTOMATION_NOT_READY");
        clicked = true;
        require(button.performAction(AccessibilityNodeInfo.ACTION_CLICK), "DOWNLOAD_UNCONFIRMED");
        // A click is only a request. The reader must later verify the original bytes.
        return result("DOWNLOAD_REQUESTED", true);
    }

    private JSONObject send(JSONObject command, String hash) throws Exception {
        stage = "send_request";
        String talker = command.optString("conversationId");
        String alias = command.optString("alias");
        String title = command.optString("conversationName");
        String body = command.optString("body");
        String replyId = command.optString("replyId");
        require(!talker.isEmpty() && !talker.endsWith("@chatroom") && !alias.isEmpty() && alias.length() <= 128,
            "WECHAT_ACTION_CHANGED");
        require(!title.isEmpty() && !body.trim().isEmpty() && body.codePointCount(0, body.length()) <= 1000
            && replyId.matches("[0-9a-f-]{36}"), "INVALID_REQUEST");
        SharedPreferences journal = getContext().getSharedPreferences("send-journal", Context.MODE_PRIVATE);
        stage = "send_journal";
        String prior = journal.getString(replyId, null);
        if (prior != null) {
            require(prior.equals(hash), "WECHAT_ACTION_CHANGED");
            return result("SEND_UNCONFIRMED", true);
        }
        AccessibilityNodeInfo composer = navigate(alias, title);
        stage = "draft_guard";
        require(text(composer).isEmpty(), "WECHAT_ACTION_CHANGED");
        require(command.optLong("expiresAt") > System.currentTimeMillis(), "AUTOMATION_NOT_READY");
        int window = composer.getWindowId();
        Bundle arguments = new Bundle();
        arguments.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, body);
        stage = "compose_text";
        require(composer.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments), "WECHAT_ACTION_CHANGED");
        draftComposer = composer;
        draftBody = body;
        draftTitle = title;
        pause();
        stage = "compose_verify";
        AccessibilityNodeInfo current = composer();
        require(current != null && current.getWindowId() == window && current.equals(composer)
            && current.refresh() && text(current).equals(body) && titleMatches(title), "WECHAT_ACTION_CHANGED");
        Rect inputBounds = new Rect();
        current.getBoundsInScreen(inputBounds);
        List<AccessibilityNodeInfo> sends = new ArrayList<>();
        stage = "send_button";
        for (AccessibilityNodeInfo node : nodes()) {
            if (!node.isEnabled() || !node.isClickable() || !(label(node).equals("发送") || label(node).equals("Send"))) continue;
            Rect bounds = new Rect();
            node.getBoundsInScreen(bounds);
            if (bounds.left >= inputBounds.right && Math.min(bounds.bottom, inputBounds.bottom) > Math.max(bounds.top, inputBounds.top)) sends.add(node);
        }
        require(sends.size() == 1, "WECHAT_ACTION_CHANGED");
        stage = "source_grant";
        JSONObject grant = sourceGrant(command, hash);
        stage = "send_revalidate";
        current = composer();
        require(current != null && current.getWindowId() == window && current.equals(composer)
            && current.refresh() && text(current).equals(body) && titleMatches(title)
            && sends.get(0).refresh() && sends.get(0).getWindowId() == window
            && sends.get(0).isEnabled() && sends.get(0).isClickable(), "WECHAT_ACTION_CHANGED");
        require(grant.getLong("expiresAt") > System.currentTimeMillis(), "AUTOMATION_NOT_READY");
        stage = "send_journal_commit";
        require(journal.edit().putString(replyId, hash).commit(), "AUTOMATION_NOT_READY");
        // After this durable point neither a crash nor a repeated command may issue another click.
        clicked = true;
        long clickedAt = System.currentTimeMillis();
        stage = "send_click";
        require(grant.getLong("expiresAt") > clickedAt, "SEND_UNCONFIRMED");
        require(sends.get(0).performAction(AccessibilityNodeInfo.ACTION_CLICK), "SEND_UNCONFIRMED");
        stage = "send_acceptance";
        for (int attempt = 0; attempt < 10; attempt++) {
            pause();
            current = composer();
            require(current != null && current.getWindowId() == window && current.equals(composer) && titleMatches(title), "SEND_UNCONFIRMED");
            if (text(current).isEmpty()) return result("UI_ACCEPTED", true)
                .put("sourceMaxId", grant.getLong("sourceMaxId")).put("clickedAt", clickedAt);
            require(text(current).equals(body), "SEND_UNCONFIRMED");
        }
        return result("SEND_UNCONFIRMED", true);
    }

    private JSONObject sourceGrant(JSONObject command, String hash) throws Exception {
        String nonce = UUID.randomUUID().toString();
        JSONObject request = new JSONObject().put("replyId", command.getString("replyId"))
            .put("commandHash", hash).put("nonce", nonce);
        File directory = getContext().getFilesDir();
        File temporary = new File(directory, "source-check.new");
        try (FileOutputStream stream = new FileOutputStream(temporary)) {
            stream.write(request.toString().getBytes(StandardCharsets.UTF_8));
            stream.getFD().sync();
        }
        require(temporary.renameTo(new File(directory, "source-check.json")), "AUTOMATION_NOT_READY");
        File authorization = new File(directory, "authorization.json");
        while (SystemClock.uptimeMillis() < deadline && command.getLong("expiresAt") > System.currentTimeMillis()) {
            if (authorization.isFile()) {
                require(authorization.length() >= 2 && authorization.length() <= 4096, "AUTOMATION_NOT_READY");
                JSONObject grant;
                try (FileInputStream stream = new FileInputStream(authorization)) {
                    grant = new JSONObject(new String(stream.readAllBytes(), StandardCharsets.UTF_8));
                }
                require(grant.getString("replyId").equals(command.getString("replyId"))
                    && grant.getString("commandHash").equals(hash) && grant.getString("nonce").equals(nonce)
                    && grant.getLong("sourceMaxId") >= command.getLong("sourceMaxId")
                    && grant.getLong("expiresAt") <= command.getLong("expiresAt")
                    && grant.getLong("expiresAt") > System.currentTimeMillis(), "WECHAT_ACTION_CHANGED");
                return grant;
            }
            SystemClock.sleep(100);
        }
        throw new Gate("AUTOMATION_NOT_READY");
    }

    private void clearUnsentDraft() {
        if (clicked || draftComposer == null) return;
        long originalDeadline = deadline;
        deadline = SystemClock.uptimeMillis() + 2000;
        try {
            AccessibilityNodeInfo current = composer();
            if (current != null && current.equals(draftComposer) && current.getWindowId() == draftComposer.getWindowId()
                && current.refresh() && text(current).equals(draftBody) && titleMatches(draftTitle)) {
                Bundle empty = new Bundle();
                empty.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, "");
                current.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, empty);
            }
        } catch (Exception ignored) { }
        finally { deadline = originalDeadline; }
    }

    private AccessibilityNodeInfo navigate(String alias, String title) throws Exception {
        stage = "launch";
        Intent launch = getContext().getPackageManager().getLaunchIntentForPackage(WECHAT);
        require(launch != null, "AUTOMATION_NOT_READY");
        launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        long rootDeadline = Math.min(deadline, SystemClock.uptimeMillis() + 3000);
        getContext().startActivity(launch);
        pause();
        stage = "launch_root_ready";
        boolean rootReady = false;
        while (SystemClock.uptimeMillis() <= rootDeadline) {
            automation.clearCache();
            AccessibilityNodeInfo current = automation.getRootInActiveWindow();
            if (current != null && WECHAT.contentEquals(current.getPackageName())) {
                Rect bounds = new Rect();
                current.getBoundsInScreen(bounds);
                if (current.isVisibleToUser() && current.getChildCount() > 0 && bounds.width() > 0 && bounds.height() > 0) {
                    rootReady = true;
                    break;
                }
            }
            long remaining = rootDeadline - SystemClock.uptimeMillis();
            if (remaining <= 0) break;
            SystemClock.sleep(Math.min(100, remaining));
        }
        require(rootReady, "AUTOMATION_NOT_READY");
        AccessibilityNodeInfo search = null;
        stage = "search_open";
        for (int attempt = 0; attempt < 3; attempt++) {
            search = uniqueClickable("搜索", "Search");
            if (search != null) break;
            require(uniqueClickable("返回", "Back") != null || composer() != null, "WECHAT_ACTION_CHANGED");
            back();
        }
        require(search != null && search.performAction(AccessibilityNodeInfo.ACTION_CLICK), "WECHAT_ACTION_CHANGED");
        pause();
        stage = "search_input";
        List<AccessibilityNodeInfo> inputs = new ArrayList<>();
        for (AccessibilityNodeInfo node : nodes()) if (node.isEnabled() && node.isEditable()) inputs.add(node);
        require(inputs.size() == 1, "WECHAT_ACTION_CHANGED");
        Bundle query = new Bundle();
        query.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, alias);
        stage = "search_query";
        require(inputs.get(0).performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, query), "WECHAT_ACTION_CHANGED");
        pause();
        stage = "search_result";
        AccessibilityNodeInfo candidate = uniqueClickable(alias, title);
        require(candidate != null && candidate.performAction(AccessibilityNodeInfo.ACTION_CLICK), "WECHAT_ACTION_CHANGED");
        pause();
        stage = "chat_composer";
        AccessibilityNodeInfo original = composer();
        if (original == null) {
            // A friend with no chat history opens the official contact profile.
            stage = "initial_profile_alias";
            require(profileAliasMatches(alias), "WECHAT_ACTION_CHANGED");
            stage = "initial_profile_message";
            AccessibilityNodeInfo start = uniqueClickable("发消息", "Message", "Send Message");
            require(start != null && start.performAction(AccessibilityNodeInfo.ACTION_CLICK), "WECHAT_ACTION_CHANGED");
            pause();
            original = composer();
        }
        stage = "chat_title";
        require(original != null && text(original).isEmpty() && titleMatches(title), "WECHAT_ACTION_CHANGED");
        stage = "chat_info_open";
        AccessibilityNodeInfo more = uniqueClickable("更多信息", "Chat Info");
        require(more != null && more.performAction(AccessibilityNodeInfo.ACTION_CLICK), "WECHAT_ACTION_CHANGED");
        pause();
        stage = "chat_info_title";
        require(hasLabel("聊天信息") || hasLabel("聊天详情") || hasLabel("Chat Info"), "WECHAT_ACTION_CHANGED");
        stage = "chat_info_member";
        Set<AccessibilityNodeInfo> members = new HashSet<>();
        for (AccessibilityNodeInfo node : nodes()) {
            if ("com.tencent.mm:id/m7b".equals(node.getViewIdResourceName())) {
                AccessibilityNodeInfo member = clickable(node);
                if (member != null) members.add(member);
            }
        }
        require(members.size() == 1, "WECHAT_ACTION_CHANGED");
        require(members.iterator().next().performAction(AccessibilityNodeInfo.ACTION_CLICK), "WECHAT_ACTION_CHANGED");
        pause();
        stage = "recipient_alias";
        require(profileAliasMatches(alias), "WECHAT_ACTION_CHANGED");
        stage = "chat_info_return";
        back();
        require(hasLabel("聊天信息") || hasLabel("聊天详情") || hasLabel("Chat Info"), "WECHAT_ACTION_CHANGED");
        stage = "chat_return";
        back();
        AccessibilityNodeInfo current = composer();
        require(current != null && current.equals(original) && current.getWindowId() == original.getWindowId()
            && text(current).isEmpty() && titleMatches(title), "WECHAT_ACTION_CHANGED");
        return current;
    }

    private boolean profileAliasMatches(String expected) throws Gate {
        int matches = 0;
        for (AccessibilityNodeInfo node : nodes()) {
            String value = text(node).replace('\uFF1A', ':').trim();
            if (value.equals("微信号: " + expected) || value.equals("微信号:" + expected)
                || value.equals("WeChat ID: " + expected)) matches++;
            if (value.equals("微信号") || value.equals("微信号:") || value.equals("WeChat ID")) {
                AccessibilityNodeInfo parent = node.getParent();
                if (parent != null && parent.getChildCount() <= 4) {
                    int exact = 0;
                    for (int index = 0; index < parent.getChildCount(); index++) {
                        AccessibilityNodeInfo sibling = parent.getChild(index);
                        if (sibling != null && sibling.isVisibleToUser() && text(sibling).equals(expected)) exact++;
                    }
                    if (exact == 1) matches++;
                }
            }
        }
        return matches == 1;
    }

    private boolean titleMatches(String expected) throws Gate {
        int count = 0;
        Rect rootBounds = new Rect();
        root().getBoundsInScreen(rootBounds);
        for (AccessibilityNodeInfo node : nodes()) {
            Rect bounds = new Rect();
            node.getBoundsInScreen(bounds);
            if (bounds.bottom < rootBounds.top + rootBounds.height() / 4 && label(node).equals(expected)) count++;
        }
        return count == 1;
    }

    private AccessibilityNodeInfo composer() throws Gate {
        List<AccessibilityNodeInfo> candidates = new ArrayList<>();
        Rect rootBounds = new Rect();
        root().getBoundsInScreen(rootBounds);
        for (AccessibilityNodeInfo node : nodes()) {
            Rect bounds = new Rect();
            node.getBoundsInScreen(bounds);
            if (node.isEnabled() && node.isEditable() && bounds.top > rootBounds.top + rootBounds.height() / 2) candidates.add(node);
        }
        require(candidates.size() <= 1, "WECHAT_ACTION_CHANGED");
        return candidates.isEmpty() ? null : candidates.get(0);
    }

    private AccessibilityNodeInfo uniqueClickable(String... labels) throws Gate {
        Set<AccessibilityNodeInfo> candidates = new HashSet<>();
        for (AccessibilityNodeInfo node : nodes()) {
            if (node.isEditable()) continue;
            for (String expected : labels) {
                if (!expected.isEmpty() && label(node).equals(expected)) {
                    AccessibilityNodeInfo candidate = clickable(node);
                    if (candidate != null) candidates.add(candidate);
                }
            }
        }
        require(candidates.size() <= 1, "WECHAT_ACTION_CHANGED");
        return candidates.isEmpty() ? null : candidates.iterator().next();
    }

    private static AccessibilityNodeInfo clickable(AccessibilityNodeInfo node) {
        for (int depth = 0; node != null && depth < 5; depth++, node = node.getParent()) {
            if (node.isVisibleToUser() && node.isEnabled() && node.isClickable()) return node;
        }
        return null;
    }

    private boolean hasLabel(String expected) throws Gate {
        for (AccessibilityNodeInfo node : nodes()) if (label(node).equals(expected)) return true;
        return false;
    }

    private AccessibilityNodeInfo root() throws Gate {
        require(SystemClock.uptimeMillis() <= deadline, "WECHAT_WINDOW_TIMEOUT");
        AccessibilityNodeInfo root = automation.getRootInActiveWindow();
        require(root != null && WECHAT.contentEquals(root.getPackageName()), "WECHAT_ACTION_CHANGED");
        int windows = 0;
        for (AccessibilityWindowInfo window : automation.getWindows()) {
            AccessibilityNodeInfo candidate = window.getRoot();
            if (candidate != null && WECHAT.contentEquals(candidate.getPackageName())) windows++;
        }
        require(windows == 1, "WECHAT_ACTION_CHANGED");
        return root;
    }

    private List<AccessibilityNodeInfo> nodes() throws Gate {
        List<AccessibilityNodeInfo> pending = new ArrayList<>();
        List<AccessibilityNodeInfo> result = new ArrayList<>();
        pending.add(root());
        for (int index = 0; index < pending.size(); index++) {
            require(pending.size() <= 3000, "WECHAT_ACTION_CHANGED");
            AccessibilityNodeInfo node = pending.get(index);
            if (node.isVisibleToUser()) result.add(node);
            for (int child = 0; child < node.getChildCount(); child++) {
                AccessibilityNodeInfo entry = node.getChild(child);
                if (entry != null) pending.add(entry);
            }
        }
        return result;
    }

    private void back() throws Gate {
        require(automation.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK), "WECHAT_ACTION_CHANGED");
        pause();
    }

    private static String text(AccessibilityNodeInfo node) { return node.getText() == null ? "" : node.getText().toString(); }
    private static String label(AccessibilityNodeInfo node) {
        String value = text(node);
        return value.isEmpty() && node.getContentDescription() != null ? node.getContentDescription().toString() : value;
    }
    private static void pause() { SystemClock.sleep(650); }
    private static void require(boolean condition, String code) throws Gate { if (!condition) throw new Gate(code); }
    private static JSONObject result(String status, boolean clicked) {
        return result(status, clicked, null);
    }
    private static JSONObject result(String status, boolean clicked, String reason) {
        try {
            JSONObject value = new JSONObject().put("status", status).put("clicked", clicked);
            if (reason != null) value.put("reason", reason);
            return value;
        }
        catch (Exception impossible) { throw new IllegalStateException(); }
    }
    private static String digest(String value) throws Exception {
        byte[] bytes = MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
        StringBuilder result = new StringBuilder();
        for (byte item : bytes) result.append(String.format("%02x", item & 255));
        return result.toString();
    }
    private static final class Gate extends Exception {
        final String code;
        Gate(String code) { super(code); this.code = code; }
    }
}
