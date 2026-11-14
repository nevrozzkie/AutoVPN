package org.autovpn.tv;

import java.net.URI;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;

/** Pure parsing: never resolves or connects to user-supplied hosts. */
public final class ClientLink {
    private final String base;
    private final String token;
    private final String host;

    private ClientLink(String base, String token, String host) {
        this.base = base; this.token = token; this.host = host;
    }

    public static ClientLink parse(String value) {
        try {
            if (value == null || value.length() > 2048) throw new IllegalArgumentException();
            URI uri = new URI(value.trim());
            if (!"https".equalsIgnoreCase(uri.getScheme()) || uri.getHost() == null
                    || uri.getRawUserInfo() != null || uri.getRawQuery() != null
                    || uri.getRawFragment() != null || uri.getPort() == 0 || uri.getPort() > 65535) {
                throw new IllegalArgumentException();
            }
            String path = uri.getRawPath();
            java.util.regex.Matcher match = java.util.regex.Pattern.compile(
                    "^((?:/[A-Za-z0-9_-]+)*)/(?:client|sub)/([A-Za-z0-9_-]{8,256})/?$").matcher(path);
            if (!match.matches()) throw new IllegalArgumentException();
            return new ClientLink("https://" + uri.getRawAuthority() + match.group(1), match.group(2), uri.getHost());
        } catch (Exception e) {
            // Never propagate URI parser errors: their message can contain the secret token.
            throw new IllegalArgumentException("Нужна HTTPS-ссылка AutoVPN вида /client/… или /sub/…");
        }
    }

    public String displayHost() { return host; }
    public String clientUrl() { return base + "/client/" + token; }
    public String subscriptionUrl() { return base + "/sub/" + token; }
    public String amneziaKeyUrl() { return base + "/amnezia-key/" + token; }
    public String nekoImportUri(String name) {
        return "sn://subscription?url=" + encode(subscriptionUrl()) + "&name=" + encode(name);
    }
    private static String encode(String value) {
        try { return URLEncoder.encode(value, "UTF-8"); }
        catch (java.io.UnsupportedEncodingException impossible) { throw new AssertionError(impossible); }
    }
}
