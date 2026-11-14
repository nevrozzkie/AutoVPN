package org.autovpn.tv;
import org.junit.Test;
import java.util.Base64;
import java.nio.charset.StandardCharsets;
import static org.junit.Assert.*;

public class ImportPayloadTest {
    private final String subscription = "vless://fake@203.0.113.1:443?security=reality#TV\nhy2://fake@203.0.113.1:8443/#TV\n";
    @Test public void acceptsActualAutoVpnRawFormat() { assertTrue(ImportPayload.validSubscription(subscription)); }
    @Test public void acceptsBase64WrappedFormat() {
        assertTrue(ImportPayload.validSubscription(Base64.getEncoder().encodeToString(subscription.getBytes(StandardCharsets.UTF_8))));
    }
    @Test public void rejectsHtmlEmptyAndMixedContent() {
        for (String value : new String[]{"", "<html>Sign in</html>", "not a subscription", subscription + "<script>"})
            assertFalse(ImportPayload.validSubscription(value));
    }
    @Test public void requiresSingleAmneziaImportKey() {
        assertTrue(ImportPayload.validAmneziaKey("vpn://fake-test-key"));
        assertFalse(ImportPayload.validAmneziaKey("https://vpn.example/client/fake"));
        assertFalse(ImportPayload.validAmneziaKey("vpn://fake\nkey"));
    }
}
