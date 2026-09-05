include $(TOPDIR)/rules.mk

PKG_NAME:=amneziawg-tools
PKG_VERSION:=1.0.20250903
PKG_RELEASE:=1

PKG_SOURCE:=v$(PKG_VERSION).tar.gz
PKG_SOURCE_URL:=https://github.com/amnezia-vpn/amneziawg-tools/archive/refs/tags/
PKG_HASH:=d729a6f54aafcd55b2cbb7324f09ca8f0d2536772970652bf822a271d0c907d7
PKG_BUILD_DIR:=$(BUILD_DIR)/$(PKG_NAME)-$(PKG_VERSION)

PKG_MAINTAINER:=AutoVPN contributors
PKG_LICENSE:=GPL-2.0
PKG_LICENSE_FILES:=COPYING

include $(INCLUDE_DIR)/package.mk

MAKE_PATH:=src
MAKE_VARS += PLATFORM=linux

define Package/amneziawg-tools
  SECTION:=net
  CATEGORY:=Network
  SUBMENU:=VPN
  TITLE:=AmneziaWG userspace control program
  URL:=https://github.com/amnezia-vpn/amneziawg-tools
  DEPENDS:=+kmod-amneziawg
endef

define Package/amneziawg-tools/description
 Minimal AmneziaWG userspace package for AutoVPN. It installs only the awg
 control binary and the fixed engine contract receipt.
endef

define Package/amneziawg-tools/install
	$(INSTALL_DIR) $(1)/usr/bin
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/src/wg $(1)/usr/bin/awg
	$(INSTALL_DIR) $(1)/usr/share/autovpn
	$(INSTALL_DATA) ./files/usr/share/autovpn/awg-engine.json $(1)/usr/share/autovpn/awg-engine.json
endef

$(eval $(call BuildPackage,amneziawg-tools))
