package com.changy.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.IBinder;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.HashSet;
import java.util.Set;

/**
 * 前台常驻服务（2026-11-04）：轮询电脑 /api/triggered_alerts → 发系统通知。
 * 背景：预警推送走页面内 WebSocket，App 切后台（刷抖音）时 WebView 被系统冻结、
 * WS 断线，页面内弹卡收不到——"已连接"也变"未连接"。原生层轮询不受冻结影响，
 * 通知走 NotificationManager，任何界面都能弹出（heads-up）。
 * 服务端零改动：复用既有 GET /api/triggered_alerts（字段冻结原则，只读）。
 */
public class AlertService extends Service {

    private static final String CH_ID = "alerts";
    private static final int FG_ID = 1001;
    private static final long POLL_MS = 20_000;     // 20 秒一轮：预警本身 5 秒粒度触发，通知滞后 ≤20s 足够

    private volatile boolean running;
    private Thread worker;
    private SharedPreferences prefs;
    private final Set<String> seen = new HashSet<>();   // 已通知过的触发预警 id
    private boolean firstRound = true;

    @Override
    public IBinder onBind(Intent i) { return null; }

    @Override
    public void onCreate() {
        super.onCreate();
        prefs = getSharedPreferences("chan", MODE_PRIVATE);
        NotificationManager nm = getSystemService(NotificationManager.class);
        NotificationChannel ch = new NotificationChannel(CH_ID, "价格预警",
                NotificationManager.IMPORTANCE_HIGH);   // heads-up 横幅+响铃
        ch.setDescription("价格预警线触发通知");
        nm.createNotificationChannel(ch);
        // 常驻低存在感通知：告诉系统这是前台服务（保活不被冻结），点它也无动作
        Notification fg = new Notification.Builder(this, CH_ID)
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentTitle("缠论预警运行中")
                .setContentText("预警触发会在此弹出通知")
                .setOngoing(true)
                .setPriority(Notification.PRIORITY_MIN)
                .build();
        startForeground(FG_ID, fg);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        startWorker();
        return START_STICKY;
    }

    private synchronized void startWorker() {
        if (running && worker != null && worker.isAlive()) return;
        running = true;
        worker = new Thread(this::loop, "alert-poll");
        worker.start();
    }

    private void loop() {
        while (running) {
            poll();
            try { Thread.sleep(POLL_MS); } catch (Exception e) { if (!running) return; }
        }
    }

    private void poll() {
        // base_url 由 MainActivity.succeed() 在连上电脑后写入；未连上就空转
        String base = prefs.getString("base_url", "");
        if (base.isEmpty()) return;
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(base + "/api/triggered_alerts").openConnection();
            c.setConnectTimeout(4000);
            c.setReadTimeout(4000);
            c.setRequestProperty("Connection", "close");
            if (c.getResponseCode() != 200) return;
            StringBuilder sb = new StringBuilder();
            try (BufferedReader r = new BufferedReader(new InputStreamReader(c.getInputStream(), "UTF-8"))) {
                String line;
                while ((line = r.readLine()) != null) sb.append(line);
            }
            JSONArray arr = new JSONObject(sb.toString()).optJSONArray("alerts");
            if (arr == null) return;
            for (int i = 0; i < arr.length(); i++) {
                JSONObject a = arr.optJSONObject(i);
                if (a == null) continue;
                String id = a.optString("id", "");
                if (id.isEmpty() || seen.contains(id)) continue;
                seen.add(id);
                if (firstRound) continue;   // 首轮只建档不弹：避免服务启动把历史触发全弹一遍
                notifyAlert(a);
            }
            firstRound = false;
        } catch (Exception ignore) {
            // 电脑不在/网络抖动：静默，下一轮再试（seen 只增，不漏不重）
        } finally {
            if (c != null) try { c.disconnect(); } catch (Exception ignore2) {}
        }
    }

    private void notifyAlert(JSONObject a) {
        String name = a.optString("name", a.optString("symbol", ""));
        String dir = "down".equals(a.optString("direction")) ? "下穿" : "上穿";
        double price = a.optDouble("triggered_price", a.optDouble("price", 0));
        double line = a.optDouble("price", 0);
        String when = a.optString("triggered_at", "");
        String text = "下穿".equals(dir)
                ? String.format("已跌穿 %.1f（现价 %.1f）", line, price)
                : String.format("已涨穿 %.1f（现价 %.1f）", line, price);
        Intent open = new Intent(this, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pi = PendingIntent.getActivity(this, idHash(a.optString("id")), open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification n = new Notification.Builder(this, CH_ID)
                .setSmallIcon(android.R.drawable.ic_dialog_alert)
                .setContentTitle("🔔 " + name + " 预警")
                .setContentText(text)
                .setSubText(when)
                .setContentIntent(pi)
                .setAutoCancel(true)
                .setPriority(Notification.PRIORITY_HIGH)
                .build();
        getSystemService(NotificationManager.class).notify(idHash(a.optString("id")), n);
    }

    private int idHash(String id) {
        return ("alert" + id).hashCode();
    }

    @Override
    public void onDestroy() {
        running = false;
        if (worker != null) worker.interrupt();
        super.onDestroy();
    }
}
