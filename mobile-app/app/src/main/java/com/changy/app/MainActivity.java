package com.changy.app;

import android.app.Activity;
import android.app.Dialog;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.drawable.ColorDrawable;
import android.graphics.drawable.GradientDrawable;
import android.os.Bundle;
import android.view.Gravity;
import android.view.KeyEvent;
import android.widget.LinearLayout;
import android.view.View;
import android.view.Window;
import android.view.ViewGroup;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.TextView;
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
    private TextView status;
    private TextView gear;
    private SharedPreferences prefs;
    private String baseUrl;          // 例如 http://192.168.1.5:8000
    private boolean errorState;

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
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView v, String url) {
                if (!errorState) status.setVisibility(View.GONE);
            }
        });
        web.setScrollBarStyle(View.SCROLLBARS_INSIDE_OVERLAY);
        web.addJavascriptInterface(new Bridge(), "Android");
        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        // 状态层：连接提示 / 错误重试
        status = new TextView(this);
        status.setTextColor(Color.parseColor("#d1d4dc"));
        status.setTextSize(15);
        status.setLineSpacing(6, 1);
        status.setGravity(Gravity.CENTER);
        status.setBackgroundColor(Color.parseColor("#131722"));
        status.setPadding(60, 60, 60, 60);
        status.setOnClickListener(v -> { if (errorState) resolveAndLoad(); });
        root.addView(status, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        // ⚙ 悬浮设置按钮（右上角，低调半透明）
        gear = new TextView(this);
        gear.setText("⚙");
        gear.setTextSize(15);
        gear.setTextColor(Color.parseColor("#787b86"));
        gear.setGravity(Gravity.CENTER);
        gear.setOnClickListener(v -> showSettings());
        FrameLayout.LayoutParams gp = new FrameLayout.LayoutParams(dp(38), dp(38));
        gp.gravity = Gravity.TOP | Gravity.END;
        gp.setMargins(0, dp(34), dp(6), 0);
        root.addView(gear, gp);

        setContentView(root);
        resolveAndLoad();
    }

    // ============ 地址解析链：上次成功 → mDNS 默认 → 子网扫描 → 手动设置 ============

    private void resolveAndLoad() {
        errorState = false;
        showStatus("正在连接电脑服务…");
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
            runOnUiThread(() -> showError(
                    "无法连接电脑服务\n\n请确认：\n· 电脑上的项目已启动\n· 手机与电脑在同一网络（WiFi 或热点）\n\n点击本屏幕重试\n或点右上角 ⚙ 手动设置地址"));
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
            errorState = false;
            showStatus("加载页面中…");
            web.loadUrl(baseUrl + PAGE);
        });
    }

    private void showStatus(String t) {
        errorState = false;
        status.setVisibility(View.VISIBLE);
        status.setText(t);
    }

    private void showError(String t) {
        errorState = true;
        status.setVisibility(View.VISIBLE);
        status.setText(t);
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

    // ============ 设置对话框（深色圆角，与 APP 主题一致） ============

    private void showSettings() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(22);
        box.setPadding(pad, dp(20), pad, dp(16));
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(Color.parseColor("#1e222d"));
        bg.setCornerRadius(dp(14));
        box.setBackground(bg);

        TextView title = new TextView(this);
        title.setText("服务器地址");
        title.setTextColor(Color.WHITE);
        title.setTextSize(17);
        title.setTypeface(null, android.graphics.Typeface.BOLD);
        box.addView(title);

        TextView hint = new TextView(this);
        hint.setText("电脑启动服务时屏幕会打印地址\n例如 http://192.168.1.5:8000\n留空恢复默认：" + DEFAULT_HOST);
        hint.setTextColor(Color.parseColor("#787b86"));
        hint.setTextSize(12);
        hint.setLineSpacing(dp(3), 1);
        hint.setPadding(0, dp(10), 0, dp(14));
        box.addView(hint);

        final EditText input = new EditText(this);
        input.setText(baseUrl != null ? baseUrl : prefs.getString("base_url", ""));
        input.setHint("http://192.168.1.5:8000");
        input.setHintTextColor(Color.parseColor("#4a4e5a"));
        input.setTextSize(14);
        input.setTextColor(Color.WHITE);
        input.setSingleLine();
        GradientDrawable ibg = new GradientDrawable();
        ibg.setColor(Color.parseColor("#131722"));
        ibg.setCornerRadius(dp(9));
        ibg.setStroke(dp(1), Color.parseColor("#2a2e39"));
        input.setBackground(ibg);
        input.setPadding(dp(12), dp(11), dp(12), dp(11));
        box.addView(input, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.END);
        LinearLayout.LayoutParams rp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        rp.topMargin = dp(16);
        row.setLayoutParams(rp);

        LinearLayout.LayoutParams bp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        bp.setMargins(dp(8), 0, 0, 0);

        row.addView(textBtn("恢复默认", "#787b86", "#2a2e39", v -> {
            prefs.edit().putString("base_url", "http://" + DEFAULT_HOST + ":" + PORT).apply();
            dlg.dismiss();
            resolveAndLoad();
        }), bp);
        row.addView(textBtn("取消", "#d1d4dc", "#2a2e39", v -> dlg.dismiss()), bp);

        GradientDrawable sbg = new GradientDrawable();
        sbg.setColor(Color.parseColor("#f0b90b"));
        sbg.setCornerRadius(dp(9));
        TextView save = new TextView(this);
        save.setText("保存并连接");
        save.setTextColor(Color.parseColor("#131722"));
        save.setTextSize(14);
        save.setTypeface(null, android.graphics.Typeface.BOLD);
        save.setBackground(sbg);
        save.setPadding(dp(16), dp(9), dp(16), dp(9));
        save.setOnClickListener(v -> {
            String v2 = input.getText().toString().trim();
            if (v2.isEmpty()) v2 = "http://" + DEFAULT_HOST + ":" + PORT;
            v2 = v2.replaceAll("/+$", "");
            prefs.edit().putString("base_url", v2).apply();
            dlg.dismiss();
            resolveAndLoad();
        });
        row.addView(save, bp);
        box.addView(row);

        dlg = new Dialog(this);
        dlg.requestWindowFeature(Window.FEATURE_NO_TITLE);
        dlg.getWindow().setBackgroundDrawable(new ColorDrawable(Color.TRANSPARENT));
        dlg.setContentView(box);
        android.view.WindowManager.LayoutParams lp = dlg.getWindow().getAttributes();
        lp.width = (int) (getResources().getDisplayMetrics().widthPixels * 0.86);
        dlg.getWindow().setAttributes(lp);
        dlg.show();
    }

    private Dialog dlg;

    private TextView textBtn(String txt, String fg, String border, View.OnClickListener oc) {
        TextView t = new TextView(this);
        t.setText(txt);
        t.setTextColor(Color.parseColor(fg));
        t.setTextSize(14);
        GradientDrawable g = new GradientDrawable();
        g.setColor(Color.parseColor("#131722"));
        g.setCornerRadius(dp(9));
        g.setStroke(dp(1), Color.parseColor(border));
        t.setBackground(g);
        t.setPadding(dp(14), dp(9), dp(14), dp(9));
        t.setGravity(Gravity.CENTER);
        t.setOnClickListener(oc);
        return t;
    }

    // ============ JS 桥（未来扩展的接口层） ============

    public class Bridge {
        @android.webkit.JavascriptInterface
        public String serverBase() { return baseUrl; }

        @android.webkit.JavascriptInterface
        public String appVersion() { return "1.0"; }

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
