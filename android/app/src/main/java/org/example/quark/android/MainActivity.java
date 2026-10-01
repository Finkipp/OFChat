package org.example.quark.android;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.ServiceConnection;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.os.IBinder;
import android.view.View;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ListView;
import android.widget.ScrollView;
import android.widget.TextView;
import java.util.ArrayList;
import java.util.List;
import org.jxmpp.jid.impl.JidCreate;

public final class MainActivity extends Activity implements XmppService.Listener {
    private XmppService service;
    private boolean bound, visible, online;
    private String selectedPeer;
    private LinearLayout loginPanel, chatPanel;
    private EditText jidField, passwordField, hostField, portField, compose;
    private TextView state, chatTitle, messages;
    private Button connectButton;
    private ScrollView messageScroll;
    private ArrayAdapter<XmppService.Contact> contactAdapter;
    private final List<XmppService.Contact> contacts = new ArrayList<>();

    private final ServiceConnection binding = new ServiceConnection() {
        @Override public void onServiceConnected(ComponentName name, IBinder binder) {
            service = ((XmppService.LocalBinder) binder).getService();
            service.setListener(MainActivity.this);
            connectButton.setEnabled(true);
            String peer = getIntent().getStringExtra("peer");
            if (peer != null) selectPeer(peer);
        }

        @Override public void onServiceDisconnected(ComponentName name) {
            service = null;
            connectButton.setEnabled(false);
        }
    };

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        buildUi();
        android.content.SharedPreferences saved = getSharedPreferences("quark_login", MODE_PRIVATE);
        jidField.setText(saved.getString("jid", ""));
        hostField.setText(saved.getString("host", ""));
        portField.setText(saved.getString("port", "5222"));
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 10);
        }
    }

    @Override public void onStart() {
        super.onStart();
        visible = true;
        bound = bindService(new Intent(this, XmppService.class), binding, Context.BIND_AUTO_CREATE);
    }

    @Override public void onStop() {
        visible = false;
        if (service != null) service.setListener(null);
        if (bound) unbindService(binding);
        bound = false;
        service = null;
        super.onStop();
    }

    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        String peer = intent.getStringExtra("peer");
        if (peer != null && service != null) selectPeer(peer);
    }

    private int dp(int value) {
        return (int) (getResources().getDisplayMetrics().density * value + .5f);
    }

    private TextView label(String text, int size) {
        TextView view = new TextView(this);
        view.setText(text);
        view.setTextSize(size);
        view.setPadding(dp(4), dp(6), dp(4), dp(6));
        return view;
    }

    private EditText input(String hint) {
        EditText field = new EditText(this);
        field.setSingleLine(true);
        field.setHint(hint);
        return field;
    }

    private LinearLayout vertical() {
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(dp(14), dp(10), dp(14), dp(10));
        return layout;
    }

    private void buildUi() {
        LinearLayout root = vertical();
        setContentView(root);
        TextView heading = label("Quark", 23);
        root.addView(heading);
        state = label("Не подключено", 14);
        root.addView(state);

        loginPanel = vertical();
        jidField = input("JID: имя@сервер");
        passwordField = input("Пароль");
        passwordField.setInputType(129);
        hostField = input("Адрес Openfire (необязательно)");
        portField = input("Порт (5222)");
        portField.setInputType(2);
        loginPanel.addView(jidField);
        loginPanel.addView(passwordField);
        loginPanel.addView(hostField);
        loginPanel.addView(portField);
        connectButton = new Button(this);
        connectButton.setText("Подключиться");
        connectButton.setEnabled(false);
        connectButton.setOnClickListener(view -> connect());
        loginPanel.addView(connectButton);
        root.addView(loginPanel);

        chatPanel = vertical();
        chatPanel.setVisibility(View.GONE);
        LinearLayout commands = new LinearLayout(this);
        Button add = new Button(this);
        add.setText("+ JID");
        add.setOnClickListener(view -> addPeer());
        Button logout = new Button(this);
        logout.setText("Отключиться");
        logout.setOnClickListener(view -> {
            if (service != null) service.disconnect();
            onState("Отключено", false);
        });
        commands.addView(add, new LinearLayout.LayoutParams(0, dp(48), 1));
        commands.addView(logout, new LinearLayout.LayoutParams(0, dp(48), 1));
        chatPanel.addView(commands);

        contactAdapter = new ArrayAdapter<>(this, android.R.layout.simple_list_item_1, contacts);
        ListView list = new ListView(this);
        list.setAdapter(contactAdapter);
        list.setOnItemClickListener((parent, view, position, id) -> selectPeer(contacts.get(position).jid));
        chatPanel.addView(list, new LinearLayout.LayoutParams(-1, dp(135)));
        chatTitle = label("Выберите контакт", 17);
        chatPanel.addView(chatTitle);

        messageScroll = new ScrollView(this);
        messages = label("", 15);
        messages.setTextIsSelectable(true);
        messageScroll.addView(messages);
        chatPanel.addView(messageScroll, new LinearLayout.LayoutParams(-1, 0, 1));

        compose = new EditText(this);
        compose.setHint("Сообщение…");
        compose.setSingleLine(false);
        compose.setMinLines(2);
        chatPanel.addView(compose);
        LinearLayout sendButtons = new LinearLayout(this);
        Button send = new Button(this);
        send.setText("Отправить");
        send.setOnClickListener(view -> {
            String body = compose.getText().toString().trim();
            if (service != null && selectedPeer != null && !body.isEmpty()) {
                service.send(selectedPeer, body);
                compose.setText("");
            }
        });
        Button attention = new Button(this);
        attention.setText("Внимание");
        attention.setOnClickListener(view -> {
            if (service != null && selectedPeer != null) service.requestAttention(selectedPeer);
        });
        sendButtons.addView(send, new LinearLayout.LayoutParams(0, dp(48), 1));
        sendButtons.addView(attention, new LinearLayout.LayoutParams(0, dp(48), 1));
        chatPanel.addView(sendButtons);
        root.addView(chatPanel, new LinearLayout.LayoutParams(-1, 0, 1));
    }

    private void connect() {
        if (service == null) return;
        String jid = jidField.getText().toString().trim();
        String password = passwordField.getText().toString();
        String host = hostField.getText().toString().trim();
        int port;
        try {
            JidCreate.entityBareFrom(jid);
            port = Integer.parseInt(portField.getText().toString().trim());
            if (port < 1 || port > 65535 || password.isEmpty()) throw new IllegalArgumentException();
        } catch (Exception invalid) {
            state.setText("Введите JID, пароль и порт от 1 до 65535");
            return;
        }
        getSharedPreferences("quark_login", MODE_PRIVATE).edit()
                .putString("jid", jid).putString("host", host).putString("port", String.valueOf(port)).apply();
        startForegroundService(new Intent(this, XmppService.class));
        service.connect(jid, password, host, port);
        state.setText("Подключение…");
    }

    private void addPeer() {
        EditText input = new EditText(this);
        input.setHint("имя@сервер");
        new AlertDialog.Builder(this).setTitle("Открыть чат по JID").setView(input)
                .setNegativeButton("Отмена", null)
                .setPositiveButton("Открыть", (dialog, which) -> {
                    try {
                        selectPeer(JidCreate.entityBareFrom(input.getText().toString().trim()).toString());
                    } catch (Exception error) {
                        state.setText("Неверный JID");
                    }
                }).show();
    }

    private void selectPeer(String peer) {
        selectedPeer = peer;
        boolean known = false;
        for (XmppService.Contact contact : contacts) {
            if (peer.equals(contact.jid)) {
                chatTitle.setText(contact.name + " · " + peer);
                known = true;
                break;
            }
        }
        if (!known) {
            chatTitle.setText(peer);
            contacts.add(new XmppService.Contact(peer, peer));
            contactAdapter.notifyDataSetChanged();
        }
        showHistory();
    }

    private void showHistory() {
        if (selectedPeer == null || service == null || service.account() == null) return;
        List<String> history = service.store().recent(service.account(), selectedPeer, 200);
        messages.setText(android.text.TextUtils.join("\n\n", history));
        messageScroll.post(() -> messageScroll.fullScroll(View.FOCUS_DOWN));
    }

    @Override public void onState(String text, boolean connected) {
        state.setText(text);
        online = connected;
        loginPanel.setVisibility(connected ? View.GONE : View.VISIBLE);
        chatPanel.setVisibility(connected ? View.VISIBLE : View.GONE);
        if (connected) showHistory();
    }

    @Override public void onContacts(List<XmppService.Contact> updated) {
        contacts.clear();
        contacts.addAll(updated);
        contactAdapter.notifyDataSetChanged();
        if (selectedPeer == null && !contacts.isEmpty()) selectPeer(contacts.get(0).jid);
        else if (selectedPeer != null) selectPeer(selectedPeer);
    }

    @Override public void onMessage(String peer) {
        if (peer.equals(selectedPeer)) showHistory();
    }

    @Override public boolean isViewing(String peer) {
        return visible && online && peer.equals(selectedPeer);
    }

    @Override public void onCertificate(String fingerprint, String reason) {
        new AlertDialog.Builder(this).setTitle("Сертификат Openfire")
                .setMessage("Сервер предъявил неподтверждённый сертификат. Сверьте SHA-256 с Openfire:\n\n"
                        + fingerprint + "\n\n" + reason)
                .setNegativeButton("Отклонить", null)
                .setPositiveButton("Доверять сертификату", (dialog, which) -> {
                    if (service != null) service.approveCertificate(fingerprint);
                }).show();
    }
}
