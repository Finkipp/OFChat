package org.example.quark.android;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Binder;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import java.security.cert.CertificateException;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLHandshakeException;
import org.jivesoftware.smack.AbstractConnectionListener;
import org.jivesoftware.smack.ConnectionConfiguration;
import org.jivesoftware.smack.chat2.ChatManager;
import org.jivesoftware.smack.packet.Message;
import org.jivesoftware.smack.filter.StanzaTypeFilter;
import org.jivesoftware.smack.roster.Roster;
import org.jivesoftware.smack.roster.RosterEntry;
import org.jivesoftware.smack.tcp.XMPPTCPConnection;
import org.jivesoftware.smack.tcp.XMPPTCPConnectionConfiguration;
import org.jivesoftware.smackx.attention.packet.AttentionExtension;
import org.jivesoftware.smackx.carbons.CarbonManager;
import org.jivesoftware.smackx.carbons.packet.CarbonExtension;
import org.jxmpp.jid.impl.JidCreate;

public final class XmppService extends Service {
    public interface Listener {
        void onState(String state, boolean connected);
        void onContacts(List<Contact> contacts);
        void onMessage(String peer);
        void onCertificate(String fingerprint, String reason);
        boolean isViewing(String peer);
    }

    public static final class Contact {
        public final String jid;
        public final String name;

        Contact(String jid, String name) {
            this.jid = jid;
            this.name = name;
        }

        @Override public String toString() {
            return name.equals(jid) ? jid : name + " · " + jid;
        }
    }

    public final class LocalBinder extends Binder {
        public XmppService getService() { return XmppService.this; }
    }

    private static final String STATUS_CHANNEL = "quark_connection";
    private static final String MESSAGE_CHANNEL = "quark_messages";
    private final IBinder binder = new LocalBinder();
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final Handler main = new Handler(Looper.getMainLooper());
    private final List<Contact> contacts = new ArrayList<>();
    private volatile Listener listener;
    private volatile XMPPTCPConnection xmpp;
    private ChatStore store;
    private String account, password, domain, host, resource;
    private int port;
    private volatile String state = "Не подключено";
    private volatile boolean connected;
    private volatile String waitingFingerprint;
    private volatile String waitingReason;

    @Override public void onCreate() {
        super.onCreate();
        store = new ChatStore(this);
        resource = getPreferences().getString("resource", null);
        if (resource == null) {
            resource = "QuarkAndroid-" + UUID.randomUUID().toString().substring(0, 8);
            getPreferences().edit().putString("resource", resource).apply();
        }
        NotificationManager notifications = getSystemService(NotificationManager.class);
        notifications.createNotificationChannel(new NotificationChannel(
                STATUS_CHANNEL, "Подключение Quark", NotificationManager.IMPORTANCE_LOW));
        notifications.createNotificationChannel(new NotificationChannel(
                MESSAGE_CHANNEL, "Сообщения Quark", NotificationManager.IMPORTANCE_HIGH));
    }

    private android.content.SharedPreferences getPreferences() {
        return getSharedPreferences("quark_connection", MODE_PRIVATE);
    }

    @Override public IBinder onBind(Intent intent) { return binder; }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        startForeground(1, notification(STATUS_CHANNEL, "Quark", state, null));
        return START_NOT_STICKY;
    }

    void setListener(Listener current) {
        listener = current;
        if (current != null) {
            current.onState(state, connected);
            synchronized (contacts) { current.onContacts(new ArrayList<>(contacts)); }
            if (waitingFingerprint != null) current.onCertificate(waitingFingerprint, waitingReason);
        }
    }

    public String account() { return account; }
    public ChatStore store() { return store; }

    public void connect(String jid, String secret, String server, int serverPort) {
        int at = jid.indexOf('@');
        if (at < 1 || at == jid.length() - 1) {
            showState("Укажите JID вида имя@домен", false);
            return;
        }
        account = jid.trim();
        password = secret;
        domain = account.substring(at + 1);
        host = server.trim().isEmpty() ? domain : server.trim();
        port = serverPort;
        worker.execute(this::connectWorker);
    }

    public void approveCertificate(String fingerprint) {
        if (waitingFingerprint == null || !waitingFingerprint.equals(fingerprint)) return;
        getPreferences().edit().putString(pinKey(), fingerprint).apply();
        waitingFingerprint = null;
        waitingReason = null;
        worker.execute(this::connectWorker);
    }

    private String pinKey() { return "pin:" + domain + "|" + host + ":" + port; }

    private void connectWorker() {
        showState("Подключение к " + host + "…", false);
        try {
            if (xmpp != null && xmpp.isConnected()) xmpp.disconnect();
            String pin = getPreferences().getString(pinKey(), null);
            XMPPTCPConnectionConfiguration.Builder builder = XMPPTCPConnectionConfiguration.builder()
                    .setXmppDomain(domain)
                    .setUsernameAndPassword(account.substring(0, account.indexOf('@')), password)
                    .setResource(resource)
                    .setHost(host)
                    .setPort(port)
                    .setSecurityMode(ConnectionConfiguration.SecurityMode.required)
                    .setCompressionEnabled(false)
                    .setConnectTimeout(12_000);
            if (pin != null) {
                builder.setCustomSSLContext(TlsPin.pinnedContext(pin));
                builder.setHostnameVerifier((name, session) -> TlsPin.matches(pin, session));
            } else {
                builder.setHostnameVerifier((name, session) ->
                        HttpsURLConnection.getDefaultHostnameVerifier().verify(domain, session));
            }
            XMPPTCPConnection connection = new XMPPTCPConnection(builder.build());
            xmpp = connection;
            connection.connect().login();
            connected = true;
            connection.addConnectionListener(new AbstractConnectionListener() {
                @Override public void connectionClosedOnError(Exception error) {
                    showState("Соединение потеряно: " + error.getMessage(), false);
                }
            });
            ChatManager.getInstanceFor(connection).addIncomingListener((from, message, chat) ->
                    record(from.toString(), message, false));
            connection.addAsyncStanzaListener(stanza -> {
                Message message = (Message) stanza;
                if (message.hasExtension(AttentionExtension.ELEMENT_NAME, AttentionExtension.NAMESPACE)) {
                    String peer = message.getFrom().asBareJid().toString();
                    notifyMessage(peer, "Просит обратить внимание", true);
                }
            }, new StanzaTypeFilter(Message.class));
            try {
                CarbonManager carbons = CarbonManager.getInstanceFor(connection);
                carbons.addCarbonCopyReceivedListener((direction, copy, wrapper) -> {
                    boolean sent = direction == CarbonExtension.Direction.sent;
                    String peer = (sent ? copy.getTo() : copy.getFrom()).asBareJid().toString();
                    record(peer, copy, sent);
                });
                if (carbons.isSupportedByServer()) carbons.enableCarbons();
            } catch (Exception ignored) { /* Carbons are optional on Openfire. */ }
            refreshRoster();
            showState("Подключено: " + account, true);
        } catch (Exception error) {
            connected = false;
            if (xmpp != null && xmpp.isConnected()) xmpp.disconnect();
            if (certificateError(error)) {
                try {
                    waitingFingerprint = TlsPin.inspect(host, port, domain);
                    waitingReason = error.getMessage();
                    Listener current = listener;
                    if (current != null) main.post(() -> current.onCertificate(waitingFingerprint, waitingReason));
                    else notifyMessage(host, "Требуется подтверждение TLS-сертификата", true);
                    showState("Подтвердите сертификат сервера", false);
                    return;
                } catch (Exception inspectError) {
                    showState("Не удалось проверить сертификат: " + inspectError.getMessage(), false);
                    return;
                }
            }
            showState("Подключение не удалось: " + error.getMessage(), false);
        }
    }

    private static boolean certificateError(Throwable error) {
        for (Throwable cause = error; cause != null; cause = cause.getCause()) {
            if (cause instanceof SSLHandshakeException || cause instanceof CertificateException) return true;
            String text = cause.getMessage();
            if (text != null && (text.contains("PKIX") || text.contains("certificate"))) return true;
        }
        return false;
    }

    private void refreshRoster() throws Exception {
        Roster roster = Roster.getInstanceFor(xmpp);
        roster.reloadAndWait();
        Set<String> added = new HashSet<>();
        List<Contact> updated = new ArrayList<>();
        updated.add(new Contact(account, "Избранное"));
        added.add(account);
        for (RosterEntry entry : roster.getEntries()) {
            String jid = entry.getJid().toString();
            if (added.add(jid)) updated.add(new Contact(jid,
                    entry.getName() == null || entry.getName().isEmpty() ? jid : entry.getName()));
        }
        for (String peer : store.peers(account)) {
            if (added.add(peer)) updated.add(new Contact(peer, peer));
        }
        synchronized (contacts) {
            contacts.clear();
            contacts.addAll(updated);
        }
        Listener current = listener;
        if (current != null) main.post(() -> current.onContacts(updated));
    }

    private void record(String peer, Message message, boolean outgoing) {
        String body = message.getBody();
        if (body == null || body.isEmpty()) return;
        if (!store.add(account, peer, outgoing, body, message.getStanzaId())) return;
        Listener current = listener;
        if (current != null) main.post(() -> current.onMessage(peer));
        if (!outgoing && (current == null || !current.isViewing(peer))) {
            notifyMessage(peer, body, false);
        }
    }

    public void send(String peer, String body) {
        worker.execute(() -> {
            try {
                Message message = new Message();
                message.setType(Message.Type.chat);
                message.setBody(body);
                message.setStanzaId(UUID.randomUUID().toString());
                ChatManager.getInstanceFor(xmpp).chatWith(JidCreate.entityBareFrom(peer)).send(message);
                if (store.add(account, peer, true, body, message.getStanzaId())) {
                    Listener current = listener;
                    if (current != null) main.post(() -> current.onMessage(peer));
                }
            } catch (Exception error) {
                showState("Отправка не удалась: " + error.getMessage(), connected);
            }
        });
    }

    public void requestAttention(String peer) {
        worker.execute(() -> {
            try {
                Message message = new Message(JidCreate.entityBareFrom(peer));
                message.setType(Message.Type.headline);
                message.addExtension(new AttentionExtension());
                xmpp.sendStanza(message);
            } catch (Exception error) {
                showState("Не удалось привлечь внимание: " + error.getMessage(), connected);
            }
        });
    }

    public void disconnect() {
        worker.execute(() -> {
            if (xmpp != null && xmpp.isConnected()) xmpp.disconnect();
            password = null;
            showState("Отключено", false);
            stopForeground(STOP_FOREGROUND_REMOVE);
            stopSelf();
        });
    }

    private void showState(String text, boolean online) {
        state = text;
        connected = online;
        Listener current = listener;
        if (current != null) main.post(() -> current.onState(text, online));
        if (online) getSystemService(NotificationManager.class).notify(1,
                notification(STATUS_CHANNEL, "Quark", text, null));
    }

    private void notifyMessage(String peer, String body, boolean urgent) {
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) return;
        Notification notice = notification(MESSAGE_CHANNEL, peer, body, peer);
        getSystemService(NotificationManager.class).notify(10 + (peer.hashCode() & 0x0fffffff), notice);
    }

    private Notification notification(String channel, String title, String body, String peer) {
        Intent open = new Intent(this, MainActivity.class);
        if (peer != null) open.putExtra("peer", peer);
        PendingIntent pending = PendingIntent.getActivity(this, peer == null ? 1 : peer.hashCode(), open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, channel).setContentTitle(title).setContentText(body)
                .setSmallIcon(android.R.drawable.stat_notify_chat).setContentIntent(pending)
                .setAutoCancel(peer != null).build();
    }

    @Override public void onDestroy() {
        listener = null;
        worker.shutdownNow();
        if (xmpp != null && xmpp.isConnected()) xmpp.disconnect();
        store.close();
        super.onDestroy();
    }
}
