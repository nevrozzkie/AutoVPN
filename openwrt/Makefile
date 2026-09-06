include $(TOPDIR)/rules.mk

PKG_NAME:=autovpn-controller
PKG_VERSION:=0.14.3
PKG_RELEASE:=1
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE
PKG_MAINTAINER:=AutoVPN contributors

include $(INCLUDE_DIR)/package.mk

define Package/autovpn-controller
  SECTION:=net
  CATEGORY:=Network
  SUBMENU:=VPN
  TITLE:=Native AutoVPN controller
  PKGARCH:=all
  DEPENDS:=+busybox +ca-bundle +curl +flock +coreutils-stat +coreutils-timeout +jsonfilter +procd +rpcd +rpcd-mod-ucode +ubus +uci +ucode +ucode-mod-fs +ucode-mod-uci +luci-base +sing-box-tiny +ip-full +firewall4 +kmod-nft-nat +kmod-nft-queue
endef

define Package/autovpn-controller/description
 A small ucode controller, procd service and native LuCI view for the
 versioned AutoVPN router snapshot protocol. Runtime VPN, policy routing,
 nftables and zapret integrations are deliberately supplied by an adapter.
endef

define Package/autovpn-controller/conffiles
/etc/config/autovpn
/etc/autovpn/credentials
/etc/autovpn/state/journal.json
/etc/autovpn/runtime/
/etc/autovpn/runtime-zapret/
/etc/autovpn/routing/ru.json
/etc/autovpn/networks/journal.json
/etc/autovpn/release.json
/etc/autovpn/release-signing.pem
/etc/autovpn/state/maintenance.lock
/etc/autovpn/state/update.lock
endef

Build/Compile=

define Package/autovpn-controller/install
	$(INSTALL_DIR) $(1)/etc/config
	$(INSTALL_CONF) ./files/etc/config/autovpn $(1)/etc/config/autovpn
	$(INSTALL_DIR) $(1)/etc/init.d
	$(INSTALL_BIN) ./files/etc/init.d/autovpn $(1)/etc/init.d/autovpn
	$(INSTALL_BIN) ./files/etc/init.d/autovpn-tunnel $(1)/etc/init.d/autovpn-tunnel
	$(INSTALL_BIN) ./files/etc/init.d/autovpn-tunnel-zapret $(1)/etc/init.d/autovpn-tunnel-zapret
	$(INSTALL_BIN) ./files/etc/init.d/autovpn-zapret $(1)/etc/init.d/autovpn-zapret
	$(INSTALL_BIN) ./files/etc/init.d/autovpn-direct-zapret $(1)/etc/init.d/autovpn-direct-zapret
	$(INSTALL_BIN) ./files/etc/init.d/autovpn-networks $(1)/etc/init.d/autovpn-networks
	$(INSTALL_DIR) $(1)/usr/libexec/autovpn
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/controller.uc $(1)/usr/libexec/autovpn/controller.uc
	$(INSTALL_DATA) ./files/usr/libexec/autovpn/credential.sh $(1)/usr/libexec/autovpn/credential.sh
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/loop $(1)/usr/libexec/autovpn/loop
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/ru-db-loop $(1)/usr/libexec/autovpn/ru-db-loop
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/ru-db-update $(1)/usr/libexec/autovpn/ru-db-update
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/ru-db-helper.uc $(1)/usr/libexec/autovpn/ru-db-helper.uc
	$(INSTALL_DIR) $(1)/etc/autovpn/routing
	$(INSTALL_CONF) ./files/etc/autovpn/routing/ru.json $(1)/etc/autovpn/routing/ru.json
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/http-adapter $(1)/usr/libexec/autovpn/http-adapter
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/http-helper.uc $(1)/usr/libexec/autovpn/http-helper.uc
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/runtime-adapter $(1)/usr/libexec/autovpn/runtime-adapter
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/runtime-lane $(1)/usr/libexec/autovpn/runtime-lane
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/runtime-helper.uc $(1)/usr/libexec/autovpn/runtime-helper.uc
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/probe-helper.uc $(1)/usr/libexec/autovpn/probe-helper.uc
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/zapret-helper.uc $(1)/usr/libexec/autovpn/zapret-helper.uc
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/direct-zapret-helper.uc $(1)/usr/libexec/autovpn/direct-zapret-helper.uc
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/zapret-install $(1)/usr/libexec/autovpn/zapret-install
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/awg-helper.uc $(1)/usr/libexec/autovpn/awg-helper.uc
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/network-helper.uc $(1)/usr/libexec/autovpn/network-helper.uc
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/setup-helper.uc $(1)/usr/libexec/autovpn/setup-helper.uc
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/wifi-bootstrap.uc $(1)/usr/libexec/autovpn/wifi-bootstrap.uc
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/install-wifi $(1)/usr/libexec/autovpn/install-wifi
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/maintenance-helper.uc $(1)/usr/libexec/autovpn/maintenance-helper.uc
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/update-helper $(1)/usr/libexec/autovpn/update-helper
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/network-watchdog $(1)/usr/libexec/autovpn/network-watchdog
	$(INSTALL_DIR) $(1)/usr/sbin
	$(INSTALL_BIN) ./files/usr/sbin/autovpnctl $(1)/usr/sbin/autovpnctl
	$(INSTALL_DIR) $(1)/usr/share/ucode/autovpn
	$(INSTALL_DATA) ./files/usr/share/ucode/autovpn/state.uc $(1)/usr/share/ucode/autovpn/state.uc
	$(INSTALL_DATA) ./files/usr/share/ucode/autovpn/lanes.uc $(1)/usr/share/ucode/autovpn/lanes.uc
	$(INSTALL_DATA) ./files/usr/share/ucode/autovpn/journal.uc $(1)/usr/share/ucode/autovpn/journal.uc
	$(INSTALL_DATA) ./files/usr/share/ucode/autovpn/health.uc $(1)/usr/share/ucode/autovpn/health.uc
	$(INSTALL_DATA) ./files/usr/share/ucode/autovpn/probes.uc $(1)/usr/share/ucode/autovpn/probes.uc
	$(INSTALL_DATA) ./files/usr/share/ucode/autovpn/orchestration.uc $(1)/usr/share/ucode/autovpn/orchestration.uc
	$(INSTALL_DATA) ./files/usr/share/ucode/autovpn/http.uc $(1)/usr/share/ucode/autovpn/http.uc
	$(INSTALL_DATA) ./files/usr/share/ucode/autovpn/runtime.uc $(1)/usr/share/ucode/autovpn/runtime.uc
	$(INSTALL_DATA) ./files/usr/share/ucode/autovpn/zapret.uc $(1)/usr/share/ucode/autovpn/zapret.uc
	$(INSTALL_DATA) ./files/usr/share/ucode/autovpn/direct_zapret.uc $(1)/usr/share/ucode/autovpn/direct_zapret.uc
	$(INSTALL_DATA) ./files/usr/share/ucode/autovpn/networks.uc $(1)/usr/share/ucode/autovpn/networks.uc
	$(INSTALL_DATA) ./files/usr/share/ucode/autovpn/network_transaction.uc $(1)/usr/share/ucode/autovpn/network_transaction.uc
	$(INSTALL_DATA) ./files/usr/share/ucode/autovpn/setup_policy.uc $(1)/usr/share/ucode/autovpn/setup_policy.uc
	$(INSTALL_DATA) ./files/usr/share/ucode/autovpn/process.uc $(1)/usr/share/ucode/autovpn/process.uc
	$(INSTALL_DIR) $(1)/usr/share/autovpn
	$(INSTALL_DATA) ./files/usr/share/autovpn/snapshot.schema.json $(1)/usr/share/autovpn/snapshot.schema.json
	$(INSTALL_DATA) ./files/usr/share/autovpn/guard-open.nft $(1)/usr/share/autovpn/guard-open.nft
	$(INSTALL_DATA) ./files/usr/share/autovpn/guard-closed.nft $(1)/usr/share/autovpn/guard-closed.nft
	$(INSTALL_DATA) ./files/usr/share/autovpn/guard-vpn-open.nft $(1)/usr/share/autovpn/guard-vpn-open.nft
	$(INSTALL_DATA) ./files/usr/share/autovpn/guard-vpn-closed.nft $(1)/usr/share/autovpn/guard-vpn-closed.nft
	$(INSTALL_DATA) ./files/usr/share/autovpn/guard-vpn_zapret-open.nft $(1)/usr/share/autovpn/guard-vpn_zapret-open.nft
	$(INSTALL_DATA) ./files/usr/share/autovpn/guard-vpn_zapret-closed.nft $(1)/usr/share/autovpn/guard-vpn_zapret-closed.nft
	$(INSTALL_DIR) $(1)/usr/share/nftables.d/chain-pre/forward
	$(INSTALL_DATA) ./files/usr/share/nftables.d/chain-pre/forward/30-autovpn.nft $(1)/usr/share/nftables.d/chain-pre/forward/30-autovpn.nft
	$(INSTALL_DIR) $(1)/usr/share/nftables.d/chain-pre/input
	$(INSTALL_DATA) ./files/usr/share/nftables.d/chain-pre/input/30-autovpn.nft $(1)/usr/share/nftables.d/chain-pre/input/30-autovpn.nft
	$(INSTALL_DIR) $(1)/usr/share/rpcd/ucode
	$(INSTALL_BIN) ./files/usr/share/rpcd/ucode/luci.autovpn $(1)/usr/share/rpcd/ucode/luci.autovpn
	$(INSTALL_DIR) $(1)/usr/share/rpcd/acl.d
	$(INSTALL_DATA) ./files/usr/share/rpcd/acl.d/luci-app-autovpn.json $(1)/usr/share/rpcd/acl.d/luci-app-autovpn.json
	$(INSTALL_DIR) $(1)/usr/share/luci/menu.d
	$(INSTALL_DATA) ./files/usr/share/luci/menu.d/luci-app-autovpn.json $(1)/usr/share/luci/menu.d/luci-app-autovpn.json
	$(INSTALL_DIR) $(1)/www/luci-static/resources/view/autovpn
	$(INSTALL_DATA) ./files/www/luci-static/resources/view/autovpn/overview.js $(1)/www/luci-static/resources/view/autovpn/overview.js
	$(INSTALL_DATA) ./files/www/luci-static/resources/view/autovpn/settings.js $(1)/www/luci-static/resources/view/autovpn/settings.js
	$(INSTALL_DATA) ./files/www/luci-static/resources/view/autovpn/networks.js $(1)/www/luci-static/resources/view/autovpn/networks.js
	$(INSTALL_DATA) ./files/www/luci-static/resources/view/autovpn/setup.js $(1)/www/luci-static/resources/view/autovpn/setup.js
	$(INSTALL_DATA) ./files/www/luci-static/resources/view/autovpn/maintenance.js $(1)/www/luci-static/resources/view/autovpn/maintenance.js
endef

define Package/autovpn-controller/postinst
#!/bin/sh
[ -n "$${IPKG_INSTROOT}" ] || {
	umask 077
	mkdir -p /etc/autovpn/state
	chmod 0700 /etc/autovpn /etc/autovpn/state
	/etc/init.d/autovpn-networks enable >/dev/null 2>&1 || true
	/etc/init.d/autovpn enable >/dev/null 2>&1 || true
	/etc/init.d/autovpn start >/dev/null 2>&1 || true
	/etc/init.d/rpcd reload >/dev/null 2>&1 || true
}
exit 0
endef

$(eval $(call BuildPackage,autovpn-controller))
