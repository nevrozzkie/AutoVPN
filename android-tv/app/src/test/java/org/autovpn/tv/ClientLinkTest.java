package org.autovpn.tv;
import org.junit.Test;
import static org.junit.Assert.*;

public class ClientLinkTest {
    private final String token = "fake-client-token-123456";
    @Test public void derivesEndpointsAndPreservesDeploymentPrefix() {
        ClientLink link = ClientLink.parse("https://vpn.example:8443/panel/client/" + token + "/");
        assertEquals("https://vpn.example:8443/panel/sub/" + token, link.subscriptionUrl());
        assertEquals("https://vpn.example:8443/panel/amnezia-key/" + token, link.amneziaKeyUrl());
        assertEquals("vpn.example", link.displayHost());
        assertFalse(link.displayHost().contains(token));
    }
    @Test public void acceptsExistingSubscriptionLink() {
        assertEquals("https://vpn.example/client/" + token,
                ClientLink.parse("https://vpn.example/sub/" + token).clientUrl());
    }
    @Test public void rejectsUnsafeOrAmbiguousLinksWithoutLeakingToken() {
        for (String value : new String[]{"http://vpn.example/client/" + token,
                "https://user:pass@vpn.example/client/" + token,
                "https://vpn.example/client/" + token + "?redirect=https://evil.example",
                "https://vpn.example/client/" + token + "#fragment",
                "https://vpn.example/../client/" + token,
                "https://vpn.example/client/abc%2Fdefg", "https://vpn.example/admin",
                "file:///client/" + token, "https://vpn.example:0/client/" + token}) {
            try { ClientLink.parse(value); fail("Should reject input"); }
            catch (IllegalArgumentException e) { assertFalse(e.getMessage().contains(token)); }
        }
    }
    @Test public void encodesWholeSubscriptionUrlAndName() {
        String result = ClientLink.parse("https://vpn.example/client/" + token).nekoImportUri("TV & new");
        assertEquals("sn://subscription?url=https%3A%2F%2Fvpn.example%2Fsub%2F" + token + "&name=TV+%26+new", result);
    }
}
