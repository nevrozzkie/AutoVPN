package org.autovpn.tv;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import javax.net.ssl.HttpsURLConnection;

final class ConfigFetch {
    private static final int MAX_BYTES = 256 * 1024;
    static String get(String url) throws Exception {
        HttpsURLConnection connection = (HttpsURLConnection) new URL(url).openConnection();
        connection.setInstanceFollowRedirects(false);
        connection.setConnectTimeout(10_000);
        connection.setReadTimeout(15_000);
        connection.setRequestProperty("Accept", "text/plain");
        connection.setRequestProperty("User-Agent", "AutoVPN-TV/0.1");
        connection.setUseCaches(false);
        try {
            int status = connection.getResponseCode();
            if (status == 401 || status == 403 || status == 404)
                throw new SafeError("Клиент недоступен. Проверьте ссылку и доступ в AutoVPN.");
            if (status != 200) throw new SafeError("AutoVPN не выдал конфигурацию (HTTP " + status + ").");
            String type = connection.getContentType();
            if (type != null && type.toLowerCase(java.util.Locale.ROOT).contains("text/html"))
                throw new SafeError("Сервер вернул страницу вместо конфигурации.");
            long deadline = System.nanoTime() + 20_000_000_000L;
            try (InputStream in = connection.getInputStream(); ByteArrayOutputStream out = new ByteArrayOutputStream()) {
                byte[] buffer = new byte[4096];
                int count;
                while ((count = in.read(buffer)) != -1) {
                    if (out.size() + count > MAX_BYTES || System.nanoTime() > deadline)
                        throw new SafeError("Конфигурация слишком большая или сервер отвечает слишком долго.");
                    out.write(buffer, 0, count);
                }
                String body = out.toString(StandardCharsets.UTF_8.name()).trim();
                if (body.isEmpty()) throw new SafeError("AutoVPN вернул пустую конфигурацию.");
                return body;
            }
        } finally { connection.disconnect(); }
    }
    static final class SafeError extends Exception { SafeError(String message) { super(message); } }
}
