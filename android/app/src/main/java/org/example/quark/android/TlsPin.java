package org.example.quark.android;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.cert.CertificateException;
import java.security.cert.X509Certificate;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLSession;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

final class TlsPin {
    private TlsPin() { }

    static String fingerprint(X509Certificate certificate) throws Exception {
        byte[] hash = MessageDigest.getInstance("SHA-256").digest(certificate.getEncoded());
        StringBuilder text = new StringBuilder();
        for (byte value : hash) {
            if (text.length() != 0) text.append(':');
            text.append(String.format(java.util.Locale.ROOT, "%02X", value & 0xff));
        }
        return text.toString();
    }

    static SSLContext pinnedContext(String approved) throws Exception {
        SSLContext context = SSLContext.getInstance("TLS");
        context.init(null, new TrustManager[]{new X509TrustManager() {
            @Override public void checkClientTrusted(X509Certificate[] chain, String auth)
                    throws CertificateException {
                throw new CertificateException("Client certificate not supported");
            }

            @Override public void checkServerTrusted(X509Certificate[] chain, String auth)
                    throws CertificateException {
                if (chain == null || chain.length == 0) throw new CertificateException("No certificate");
                chain[0].checkValidity();
                try {
                    if (!approved.equals(fingerprint(chain[0]))) {
                        throw new CertificateException("Server certificate has changed");
                    }
                } catch (CertificateException error) {
                    throw error;
                } catch (Exception error) {
                    throw new CertificateException(error);
                }
            }

            @Override public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
        }}, null);
        return context;
    }

    static boolean matches(String approved, SSLSession session) {
        try {
            return approved.equals(fingerprint((X509Certificate) session.getPeerCertificates()[0]));
        } catch (Exception error) {
            return false;
        }
    }

    // A separate unauthenticated XMPP STARTTLS connection: never sends a password.
    static String inspect(String host, int port, String domain) throws Exception {
        try (Socket socket = new Socket()) {
            socket.connect(new InetSocketAddress(host, port), 10_000);
            socket.setSoTimeout(10_000);
            InputStream in = socket.getInputStream();
            OutputStream out = socket.getOutputStream();
            String safeDomain = domain.replace("&", "&amp;").replace("'", "&apos;")
                    .replace("<", "&lt;").replace(">", "&gt;");
            String header = "<stream:stream to='" + safeDomain + "' xmlns='jabber:client' "
                    + "xmlns:stream='http://etherx.jabber.org/streams' version='1.0'>";
            out.write(header.getBytes(StandardCharsets.UTF_8));
            out.flush();
            String features = readUntil(in, "</stream:features>");
            if (!features.contains("urn:ietf:params:xml:ns:xmpp-tls") || !features.contains("starttls")) {
                throw new IllegalStateException("Openfire does not offer STARTTLS");
            }
            out.write("<starttls xmlns='urn:ietf:params:xml:ns:xmpp-tls'/>".getBytes(StandardCharsets.UTF_8));
            out.flush();
            if (!readUntil(in, "/>").contains("proceed")) {
                throw new IllegalStateException("Openfire rejected STARTTLS");
            }
            SSLContext inspection = SSLContext.getInstance("TLS");
            inspection.init(null, new TrustManager[]{new X509TrustManager() {
                @Override public void checkClientTrusted(X509Certificate[] chain, String auth) { }
                @Override public void checkServerTrusted(X509Certificate[] chain, String auth) { }
                @Override public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
            }}, null);
            try (SSLSocket tls = (SSLSocket) inspection.getSocketFactory()
                    .createSocket(socket, host, port, false)) {
                tls.startHandshake();
                return fingerprint((X509Certificate) tls.getSession().getPeerCertificates()[0]);
            }
        }
    }

    private static String readUntil(InputStream input, String terminator) throws Exception {
        ByteArrayOutputStream data = new ByteArrayOutputStream();
        while (data.size() < 65_536) {
            int next = input.read();
            if (next == -1) break;
            data.write(next);
            String value = data.toString("UTF-8");
            if (value.contains(terminator)) return value;
        }
        throw new IllegalStateException("Invalid XMPP STARTTLS reply");
    }
}
