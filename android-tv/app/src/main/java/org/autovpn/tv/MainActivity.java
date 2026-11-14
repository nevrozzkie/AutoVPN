package org.autovpn.tv;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipDescription;
import android.content.ComponentName;
import android.content.Intent;
import android.content.res.Configuration;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Bundle;
import android.text.InputType;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.ScrollView;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class MainActivity extends Activity {
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private SecretStore secrets;
    private ClientLink link;
    private TextView server, status;
    private Button amnezia, neko, bind, forget;
    private boolean busy;

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        secrets = new SecretStore(this);
        buildUi();
        try { String value = secrets.read(); if (value != null) link = ClientLink.parse(value); }
        catch (Exception e) { status.setText("Не удалось прочитать привязку. Подключите AutoVPN заново."); }
        refresh();
    }
    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
    private TextView text(String value, int size, int color) {
        TextView view = new TextView(this); view.setText(value); view.setTextSize(size); view.setTextColor(color); return view;
    }
    private Button button(String title, LinearLayout parent) {
        Button b = new Button(this); b.setText(title); b.setAllCaps(false); b.setTextSize(18);
        b.setTextColor(Color.WHITE); b.setPadding(dp(20), dp(8), dp(20), dp(8));
        b.setOnFocusChangeListener((v, focused) -> paint(b, focused)); paint(b, false);
        b.setMinHeight(dp(64));
        var params = new LinearLayout.LayoutParams(-1, -2); params.bottomMargin = dp(12);
        parent.addView(b, params); return b;
    }
    private void paint(Button button, boolean focused) {
        GradientDrawable bg = new GradientDrawable(); bg.setColor(Color.rgb(focused ? 30 : 21, focused ? 102 : 38, focused ? 97 : 54));
        bg.setCornerRadius(dp(12)); bg.setStroke(dp(focused ? 3 : 1), Color.parseColor(focused ? "#7DE3D0" : "#304356"));
        button.setBackground(bg);
    }
    private void buildUi() {
        getWindow().setStatusBarColor(Color.BLACK); getWindow().setNavigationBarColor(Color.BLACK);
        boolean television = (getResources().getConfiguration().uiMode & Configuration.UI_MODE_TYPE_MASK)
                == Configuration.UI_MODE_TYPE_TELEVISION;
        boolean compact = getResources().getConfiguration().screenWidthDp < 600;
        if (television) getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_FULLSCREEN);
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.setFitsSystemWindows(true);
        scroll.setBackgroundColor(Color.rgb(10, 17, 26));
        LinearLayout root = new LinearLayout(this); root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(television ? 48 : 24), dp(28), dp(television ? 48 : 24), dp(24)); root.setBackgroundColor(Color.rgb(10, 17, 26));
        TextView title = text("AutoVPN", 30, Color.WHITE); title.setTypeface(null, Typeface.BOLD); root.addView(title);
        server = text("", 15, Color.rgb(155, 177, 195)); root.addView(server);
        TextView note = text("Актуальная конфигурация из AutoVPN · импорт нового профиля", 15, Color.rgb(155, 177, 195));
        note.setPadding(0, dp(8), 0, dp(18)); root.addView(note);
        amnezia = button("Добавить в Amnezia", root); amnezia.setOnClickListener(v -> importConfig(true));
        neko = button("Добавить в NekoBox", root); neko.setOnClickListener(v -> importConfig(false));
        LinearLayout controls = new LinearLayout(this); controls.setOrientation(compact ? LinearLayout.VERTICAL : LinearLayout.HORIZONTAL);
        bind = new Button(this); bind.setAllCaps(false); bind.setText("Привязать AutoVPN");
        bind.setOnClickListener(v -> showBinding()); controls.addView(bind, new LinearLayout.LayoutParams(compact ? -1 : 0, -2, compact ? 0 : 1));
        forget = new Button(this); forget.setAllCaps(false); forget.setText("Отвязать");
        forget.setOnClickListener(v -> new AlertDialog.Builder(this).setTitle("Отвязать AutoVPN?")
                .setMessage("Ссылка будет удалена с устройства. Профили в VPN-клиентах сохранятся.")
                .setNegativeButton("Отмена", null).setPositiveButton("Отвязать", (d, w) -> {
                    try { secrets.clear(); link = null; refresh(); status.setText("Привязка удалена."); }
                    catch (Exception e) { status.setText("Не удалось удалить привязку. Попробуйте ещё раз."); }
                }).show()); controls.addView(forget, new LinearLayout.LayoutParams(compact ? -1 : 0, -2, compact ? 0 : 1)); root.addView(controls);
        status = text("Выберите клиент. Подтверждение импорта выполняется в его приложении.", 14, Color.rgb(155, 177, 195));
        status.setPadding(0, dp(12), 0, 0); root.addView(status);
        scroll.addView(root, new ScrollView.LayoutParams(-1, -2));
        setContentView(scroll);
    }
    private void refresh() {
        server.setText(link == null ? "AutoVPN ещё не привязан" : "Подключено: " + link.displayHost());
        amnezia.setEnabled(link != null && !busy); neko.setEnabled(link != null && !busy);
        bind.setEnabled(!busy); forget.setEnabled(link != null && !busy);
        bind.setText(link == null ? "Привязать AutoVPN" : "Изменить привязку");
        if (link == null) bind.requestFocus();
    }
    private void showBinding() {
        // Keep the token out of IME suggestions, screenshots, accessibility text and logs.
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        EditText input = new EditText(this); input.setSingleLine(true);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD
                | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        input.setHint("https://ваш-сервер/client/…");
        input.setPadding(dp(20), dp(14), dp(20), dp(14));
        AlertDialog dialog = new AlertDialog.Builder(this).setTitle("Привязка AutoVPN")
                .setMessage("Вставьте личную ссылку клиента или подписки AutoVPN. Нужен HTTPS. Для устройства лучше создать отдельного клиента.")
                .setView(input).setNegativeButton("Отмена", null).setPositiveButton("Сохранить", null).create();
        dialog.setOnShowListener(d -> {
            dialog.getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
                try {
                    ClientLink parsed = ClientLink.parse(input.getText().toString());
                    secrets.save(parsed.clientUrl()); link = parsed; input.setText(""); dialog.dismiss();
                    status.setText("Привязка сохранена. Выберите приложение для импорта."); refresh(); amnezia.requestFocus();
                } catch (IllegalArgumentException e) { input.setError(e.getMessage()); }
                catch (Exception e) { input.setError("Не удалось безопасно сохранить привязку."); }
            });
        });
        dialog.setOnDismissListener(d -> getWindow().clearFlags(WindowManager.LayoutParams.FLAG_SECURE));
        dialog.show();
    }
    private void importConfig(boolean useAmnezia) {
        if (busy || link == null) return;
        final ClientLink selected = link;
        final ComponentName target = useAmnezia
                ? new ComponentName("org.amnezia.vpn", "org.amnezia.vpn.ImportConfigActivity")
                : new ComponentName("moe.nb4a", "io.nekohasekai.sagernet.ui.MainActivity");
        try { getPackageManager().getActivityInfo(target, 0); }
        catch (Exception e) { status.setText("Сначала установите " + (useAmnezia ? "AmneziaVPN." : "NekoBox.")); return; }
        busy = true; refresh(); status.setText("Получаю актуальную конфигурацию…");
        worker.execute(() -> {
            try {
                String body = ConfigFetch.get(useAmnezia ? selected.amneziaKeyUrl() : selected.subscriptionUrl());
                if (useAmnezia && !ImportPayload.validAmneziaKey(body))
                    throw new ConfigFetch.SafeError("Сервер не выдал корректный ключ Amnezia.");
                if (!useAmnezia && !ImportPayload.validSubscription(body))
                    throw new ConfigFetch.SafeError("Сервер не выдал поддерживаемую подписку NekoBox.");
                runOnUiThread(() -> {
                    if (isFinishing() || isDestroyed()) return;
                    busy = false; refresh();
                    Intent intent;
                    if (useAmnezia) {
                        intent = new Intent(Intent.ACTION_SEND).setType(ClipDescription.MIMETYPE_TEXT_PLAIN)
                                .putExtra(Intent.EXTRA_TEXT, body).setComponent(target);
                    } else {
                        String name = "AutoVPN · " + new SimpleDateFormat("dd.MM HH:mm:ss", Locale.ROOT).format(new Date());
                        intent = new Intent(Intent.ACTION_VIEW, Uri.parse(selected.nekoImportUri(name))).setComponent(target);
                    }
                    try {
                        startActivity(intent);
                        status.setText("Передано в " + (useAmnezia ? "Amnezia" : "NekoBox")
                                + ". Подтвердите добавление. Старые профили сохранены.");
                    } catch (Exception e) { status.setText("Не удалось открыть импорт. Проверьте версию VPN-клиента."); }
                });
            } catch (Exception e) {
                String message = e instanceof ConfigFetch.SafeError ? e.getMessage()
                        : "Не удалось получить конфигурацию. Проверьте Интернет, HTTPS и время устройства.";
                runOnUiThread(() -> {
                    if (isFinishing() || isDestroyed()) return;
                    busy = false; refresh(); status.setText(message);
                });
            }
        });
    }
    @Override protected void onDestroy() { worker.shutdownNow(); super.onDestroy(); }
}
