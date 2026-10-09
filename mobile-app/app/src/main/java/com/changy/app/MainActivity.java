package com.changy.app;

import android.app.Activity;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Bundle;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.Toast;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.net.Socket;
import java.net.URL;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Enumeration;
import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;

public class MainActivity extends Activity {

    private static final int PORT = 8000;
    private static final String DEFAULT_HOST = "ggydeMacBook-Pro-3.local";   // Mac 的 mDNS 机器名，换网/热点都不用改
    private static final String PAGE = "/m";

    private WebView web;
    private FrameLayout root;
    private SharedPreferences prefs;
    private String baseUrl;          // 例如 http://192.168.1.5:8000
    private boolean offlineShown;    // 当前处于离线壳页

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        prefs = getSharedPreferences("chan", MODE_PRIVATE);

        root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#131722"));

        web = new WebView(this);
        WebSettings ws = web.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setAllowFileAccess(true);
        ws.setAllowUniversalAccessFromFileURLs(true);   // 离线壳(file://)直连 Gitee API
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView v, android.webkit.WebResourceRequest req, android.webkit.WebResourceError err) {
                // 主页加载失败（电脑不在/地址失效）：不弹全屏提示，直接切离线壳（秒开），后台继续找电脑
                if (req.isForMainFrame() && !offlineShown) showOffline();
            }
        });
        web.setScrollBarStyle(View.SCROLLBARS_INSIDE_OVERLAY);
        web.addJavascriptInterface(new Bridge(), "Android");
        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        setContentView(root);
        // 秒开：不等任何探测，直接按已知地址进页面（失败由 onReceivedError 静默切离线壳），
        // 连接/扫描全部放后台，找到电脑后自动切换或刷新。
        String saved = prefs.getString("base_url", "");
        web.loadUrl((saved.isEmpty() ? "http://" + DEFAULT_HOST + ":" + PORT : saved) + PAGE);
        resolveInBackground();
    }

    // ============ 后台地址解析链：上次成功 → mDNS 默认 → 子网扫描（全部无界面） ============

    private void resolveInBackground() {
        new Thread(() -> {
            String saved = prefs.getString("base_url", "");
            List<String> bases = new ArrayList<>();
            if (!saved.isEmpty()) bases.add(saved);
            String def = "http://" + DEFAULT_HOST + ":" + PORT;
            if (!def.equals(saved)) bases.add(def);
            for (String base : bases) {
                if (probe(base)) { succeed(base); return; }
            }
            // 子网扫描：手机当前 WiFi 网段内找 8000 端口的电脑服务
            String ip = localIp();
            if (ip != null) {
                String prefix = ip.substring(0, ip.lastIndexOf('.') + 1);
                String hit = scanSubnet(prefix, ip);
                if (hit != null) { succeed(hit); return; }
            }
            // 首轮连不上：后台静默重试已知地址（10s 一轮，共 6 轮），不扫子网不扰用户
            for (int i = 0; i < 6; i++) {
                try { Thread.sleep(10000); } catch (Exception ignore) {}
                for (String base : bases) {
                    if (probe(base)) { succeed(base); return; }
                }
            }
        }).start();
    }

    private boolean probe(String base) {
        try {
            HttpURLConnection c = (HttpURLConnection) new URL(base + PAGE).openConnection();
            c.setConnectTimeout(2500);
            c.setReadTimeout(2500);
            c.setRequestProperty("Connection", "close");
            int code = c.getResponseCode();
            try { c.getInputStream().close(); } catch (Exception ignore) {}
            return code >= 200 && code < 500;
        } catch (Exception e) {
            return false;
        }
    }

    private void succeed(String base) {
        baseUrl = base;
        prefs.edit().putString("base_url", base).apply();
        runOnUiThread(() -> {
            // 已在正确地址的正常页面 → 不打扰；在离线壳或旧地址 → 自动切换
            String cur = String.valueOf(web.getUrl());
            if (offlineShown || !cur.startsWith(baseUrl)) {
                offlineShown = false;
                web.loadUrl(baseUrl + PAGE);
            }
        });
    }

    private void showOffline() {
        offlineShown = true;
        web.loadUrl("file:///android_asset/positions.html");
    }

    // ============ 子网扫描：找局域网里 8000 端口的电脑 ============

    private String localIp() {
        try {
            Enumeration<NetworkInterface> nis = NetworkInterface.getNetworkInterfaces();
            while (nis.hasMoreElements()) {
                for (java.net.InterfaceAddress ia : nis.nextElement().getInterfaceAddresses()) {
                    InetAddress a = ia.getAddress();
                    if (a.isSiteLocalAddress() && !a.isLoopbackAddress() && a.getAddress().length == 4) {
                        return a.getHostAddress();
                    }
                }
            }
        } catch (Exception ignore) {}
        return null;
    }

    private String scanSubnet(String prefix, String selfIp) {
        AtomicBoolean found = new AtomicBoolean(false);
        final String[] hitBase = {null};
        List<Thread> ts = new ArrayList<>();
        for (int i = 1; i <= 254; i++) {
            final String h = prefix + i;
            if (h.equals(selfIp)) continue;
            Thread t = new Thread(() -> {
                if (found.get()) return;
                String base = "http://" + h + ":" + PORT;
                if (probe(base) && found.compareAndSet(false, true)) {
                    hitBase[0] = base;
                }
            });
            ts.add(t);
        }
        for (Thread t : ts) t.start();
        long deadline = System.currentTimeMillis() + 8000;   // 扫描最多等 8 秒
        while (System.currentTimeMillis() < deadline) {
            if (found.get()) break;
            try { Thread.sleep(100); } catch (Exception ignore) {}
        }
        return hitBase[0];
    }

    // ============ JS 桥（未来扩展的接口层） ============

    public class Bridge {
        @android.webkit.JavascriptInterface
        public String serverBase() { return baseUrl; }

        @android.webkit.JavascriptInterface
        public String appVersion() { return "1.0"; }

        @android.webkit.JavascriptInterface
        public void retryLan() { runOnUiThread(() -> resolveInBackground()); }

        @android.webkit.JavascriptInterface
        public void toast(final String s) {
            runOnUiThread(() -> Toast.makeText(MainActivity.this, s, Toast.LENGTH_SHORT).show());
        }
    }

    // ============ 返回键 = 网页后退 ============

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && web.canGoBack()) {
            web.goBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    private int dp(int v) { return Math.round(v * getResources().getDisplayMetrics().density); }
}
