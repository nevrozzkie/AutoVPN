package org.autovpn.tv;

import java.nio.charset.StandardCharsets;
import java.util.Base64;

final class ImportPayload {
    static boolean validAmneziaKey(String body) {
        return body.startsWith("vpn://") && body.length() >= 12 && !body.matches("(?s).*\\s.*");
    }
    static boolean validSubscription(String body) {
        // AutoVPN /sub returns raw URI lines. Some deployments also wrap them in Base64.
        if (validLines(body)) return true;
        try {
            String decoded = new String(Base64.getDecoder().decode(body.replaceAll("\\s", "")), StandardCharsets.UTF_8);
            return validLines(decoded);
        } catch (IllegalArgumentException e) { return false; }
    }
    private static boolean validLines(String value) {
        boolean found = false;
        for (String line : value.split("\\r?\\n")) {
            if (line.isEmpty()) continue;
            if (!(line.startsWith("vless://") || line.startsWith("hysteria2://") || line.startsWith("hy2://"))) return false;
            found = true;
        }
        return found;
    }
}
