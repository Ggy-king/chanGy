package com.changy.app;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
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
    private final Handler ui = new Handler(Looper.getMainLooper());
    private Runnable bootDog;        // 开屏看门狗：主框架 3 秒没加载出来 → 切离线壳

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        // 屏幕常亮（用户定稿 2026-10-12，同花顺式）：App 打开期间越过系统息屏计时，
        // 切后台/锁屏离开自动恢复系统默认。FLAG 只对本窗口生效，不申请任何权限。
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        prefs = getSharedPreferences("chan", MODE_PRIVATE);

        root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#131722"));

        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#131722"));   // 消灭加载期白屏（WebView 默认白底）
        WebSettings ws = web.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setAllowFileAccess(true);
        ws.setAllowUniversalAccessFromFileURLs(true);   // 离线壳(file://)直连 Gitee API
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageStarted(WebView v, String url, android.graphics.Bitmap favicon) {
                // 开屏看门狗：电脑连不上时 WebView 自己的超时要 10~30 秒才报错，
                // 期间全是白屏。主框架 3 秒没加载出来 → 直接切离线壳秒开；
                // 后台解析链找到电脑后 succeed() 会自动切回在线页。
                if (!offlineShown && url.startsWith("http")) {
                    if (bootDog != null) ui.removeCallbacks(bootDog);
                    bootDog = () -> { if (!offlineShown) showOffline(); };
                    ui.postDelayed(bootDog, 3000);
                }
            }
            @Override
            public void onPageFinished(WebView v, String url) {
                if (bootDog != null) { ui.removeCallbacks(bootDog); bootDog = null; }
            }
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
        // 系统通知权限（Android 13+ 运行时请求；原生 AlertService 刷抖音弹通知的前提）
        if (android.os.Build.VERSION.SDK_INT >= 33 &&
                checkSelfPermission("android.permission.POST_NOTIFICATIONS") !=
                        android.content.pm.PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 7);
        }
        // 秒开（用户定稿 2026-10-10）：启动永远直接进本地离线壳持仓页（file:// 零等待，
        // localStorage 先渲染 + Gitee 后台取数，拿不到就空态/重试）。
        // 连电脑是后台自己的事：解析链在后台慢慢找，找到后 succeed() 自动切在线页。
        offlineShown = true;
        web.loadUrl("file:///android_asset/positions.html");
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
        // 原生预警通知服务（2026-11-04）：连上电脑即拉起前台服务轮询触发预警。
        // 页面内 WS 在 App 切后台会被系统冻结断线（"已连接"变"未连接"），
        // 通知弹窗改由原生层兜底——刷抖音/回微信也能收到预警通知。
        try {
            startForegroundService(new Intent(this, AlertService.class));
        } catch (Exception ignore) {}
        runOnUiThread(() -> {
            // 已在正确地址的正常页面 → 不打扰；在离线壳或旧地址 → 自动切在线页
            String cur = String.valueOf(web.getUrl());
            if (offlineShown || !cur.startsWith(baseUrl)) {
                offlineShown = false;
                web.stopLoading();
                web.loadUrl(baseUrl + PAGE);
            }
        });
    }

    private void showOffline() {
        offlineShown = true;
        if (bootDog != null) { ui.removeCallbacks(bootDog); bootDog = null; }
        web.stopLoading();   // 掐掉还在黑洞里等的 http 加载，防止它稍后报错/加载完顶掉离线壳
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

        // ============ Gitee 令牌跨源共享（2026-11-04 修"每次打开都要输密钥"）============
        // 离线壳(file://)与在线页(http://)是两个浏览器源，localStorage 各自独立——
        // 用户在在线页输的令牌离线壳永远看不到，而启动必经离线壳 → 每次都弹输入框。
        // 令牌改存 SharedPreferences（原生层），两个页面读写都走桥；纯浏览器打开回退 localStorage。

        @android.webkit.JavascriptInterface
        public String getGhToken() {
            return prefs.getString("gh_token", "");
        }

        @android.webkit.JavascriptInterface
        public void setGhToken(final String t) {
            prefs.edit().putString("gh_token", t == null ? "" : t).apply();
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
