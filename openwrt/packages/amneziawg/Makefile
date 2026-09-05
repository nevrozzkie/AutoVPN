include $(TOPDIR)/rules.mk

include $(INCLUDE_DIR)/kernel.mk

PKG_NAME:=amneziawg-linux-kernel-module
PKG_VERSION:=1.0.20251004
PKG_RELEASE:=1

PKG_SOURCE:=v$(PKG_VERSION).tar.gz
PKG_SOURCE_URL:=https://github.com/amnezia-vpn/amneziawg-linux-kernel-module/archive/refs/tags/
PKG_HASH:=c60393ed591c87abebcdd6f7bd87e3faef747902a4f76a6b41bf4ff189220e75
PKG_BUILD_DIR:=$(KERNEL_BUILD_DIR)/$(PKG_NAME)-$(PKG_VERSION)

PKG_MAINTAINER:=AutoVPN contributors
PKG_LICENSE:=GPL-2.0
PKG_LICENSE_FILES:=COPYING

include $(INCLUDE_DIR)/package.mk

define KernelPackage/amneziawg
  SECTION:=kernel
  CATEGORY:=Kernel Modules
  SUBMENU:=Network Support
  TITLE:=AmneziaWG kernel module
  URL:=https://github.com/amnezia-vpn/amneziawg-linux-kernel-module
  FILES:=$(PKG_BUILD_DIR)/src/amneziawg.ko
  AUTOLOAD:=$(call AutoProbe,amneziawg)
  DEPENDS:=+kmod-udptunnel4 +kmod-udptunnel6 +kmod-crypto-lib-chacha20poly1305 +kmod-crypto-lib-curve25519
endef

define KernelPackage/amneziawg/description
 Standalone AmneziaWG kernel module pinned for the AutoVPN AWG1-on-UAPI-v2
 configuration contract.
endef

define Build/Compile
	+$(KERNEL_MAKE) $(KERNEL_MAKE_FLAGS) \
		M="$(PKG_BUILD_DIR)/src" \
		WIREGUARD_VERSION="$(PKG_VERSION)" \
		modules
endef

$(eval $(call KernelPackage,amneziawg))
