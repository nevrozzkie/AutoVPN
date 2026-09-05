include $(TOPDIR)/rules.mk

PKG_NAME:=autovpn-controller
PKG_VERSION:=0.7.0
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
  DEPENDS:=+busybox +ca-bundle +curl +procd +rpcd +rpcd-mod-ucode +ubus +uci +ucode +ucode-mod-fs +ucode-mod-uci +luci-base +sing-box-tiny +ip-full +firewall4 +kmod-nft-nat
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
	$(INSTALL_BIN) ./files/etc/init.d/autovpn-networks $(1)/etc/init.d/autovpn-networks
	$(INSTALL_DIR) $(1)/usr/libexec/autovpn
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/controller.uc $(1)/usr/libexec/autovpn/controller.uc
	$(INSTALL_DATA) ./files/usr/libexec/autovpn/credential.sh $(1)/usr/libexec/autovpn/credential.sh
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/loop $(1)/usr/libexec/autovpn/loop
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/http-adapter $(1)/usr/libexec/autovpn/http-adapter
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/http-helper.uc $(1)/usr/libexec/autovpn/http-helper.uc
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/runtime-adapter $(1)/usr/libexec/autovpn/runtime-adapter
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/runtime-helper.uc $(1)/usr/libexec/autovpn/runtime-helper.uc
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/awg-helper.uc $(1)/usr/libexec/autovpn/awg-helper.uc
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/network-helper.uc $(1)/usr/libexec/autovpn/network-helper.uc
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/setup-helper.uc $(1)/usr/libexec/autovpn/setup-helper.uc
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/maintenance-helper.uc $(1)/usr/libexec/autovpn/maintenance-helper.uc
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/update-helper $(1)/usr/libexec/autovpn/update-helper
	$(INSTALL_BIN) ./files/usr/libexec/autovpn/network-watchdog $(1)/usr/libexec/autovpn/network-watchdog
	$(INSTALL_DIR) $(1)/usr/sbin
	$(INSTALL_BIN) ./files/usr/sbin/autovpnctl $(1)/usr/sbin/autovpnctl
	$(INSTALL_DIR) $(1)/usr/share/ucode/autovpn
	$(INSTALL_DATA) ./files/usr/share/ucode/autovpn/state.uc $(1)/usr/share/ucode/autovpn/state.uc
	$(INSTALL_DATA) ./files/usr/share/ucode/autovpn/journal.uc $(1)/usr/share/ucode/autovpn/journal.uc
	$(INSTALL_DATA) ./files/usr/share/ucode/autovpn/health.uc $(1)/usr/share/ucode/autovpn/health.uc
	$(INSTALL_DATA) ./files/usr/share/ucode/autovpn/orchestration.uc $(1)/usr/share/ucode/autovpn/orchestration.uc
	$(INSTALL_DATA) ./files/usr/share/ucode/autovpn/http.uc $(1)/usr/share/ucode/autovpn/http.uc
	$(INSTALL_DATA) ./files/usr/share/ucode/autovpn/runtime.uc $(1)/usr/share/ucode/autovpn/runtime.uc
	$(INSTALL_DATA) ./files/usr/share/ucode/autovpn/networks.uc $(1)/usr/share/ucode/autovpn/networks.uc
	$(INSTALL_DATA) ./files/usr/share/ucode/autovpn/network-transaction.uc $(1)/usr/share/ucode/autovpn/network-transaction.uc
	$(INSTALL_DATA) ./files/usr/share/ucode/autovpn/setup-policy.uc $(1)/usr/share/ucode/autovpn/setup-policy.uc
	$(INSTALL_DATA) ./files/usr/share/ucode/autovpn/process.uc $(1)/usr/share/ucode/autovpn/process.uc
	$(INSTALL_DIR) $(1)/usr/share/autovpn
	$(INSTALL_DATA) ./files/usr/share/autovpn/snapshot.schema.json $(1)/usr/share/autovpn/snapshot.schema.json
	$(INSTALL_DATA) ./files/usr/share/autovpn/guard-open.nft $(1)/usr/share/autovpn/guard-open.nft
	$(INSTALL_DATA) ./files/usr/share/autovpn/guard-closed.nft $(1)/usr/share/autovpn/guard-closed.nft
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
	/etc/init.d/rpcd reload >/dev/null 2>&1 || true
}
exit 0
endef

$(eval $(call BuildPackage,autovpn-controller))
