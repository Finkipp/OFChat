%global debug_package %{nil}

Name:           quark
Version:        0.3.0
Release:        1%{?dist}
Summary:        GTK XMPP client for Openfire
License:        MIT
Source0:        %{name}-%{version}.tar.gz
BuildArch:      x86_64

Requires:       python3 >= 3.11
Requires:       python3-gobject-base
Requires:       gtk3
Requires:       libappindicator-gtk3
Requires:       libsecret
Requires:       gtksourceview4
Requires:       hicolor-icon-theme
Obsoletes:      ofchat < 0.3.0
Provides:       ofchat = %{version}-%{release}

%description
Quark is a desktop XMPP client for Openfire with local message history,
notifications, file transfers, and chat state notifications.

%prep
%autosetup

%build
# Python dependencies are installed into a private directory during packaging.

%install
install -d %{buildroot}%{_datadir}/quark %{buildroot}%{_bindir} %{buildroot}%{_datadir}/applications
install -d %{buildroot}%{_datadir}/icons/hicolor/scalable/apps
cp -a chat main.py %{buildroot}%{_datadir}/quark/
install -m 0755 packaging/quark %{buildroot}%{_bindir}/quark
install -m 0644 Quark.desktop %{buildroot}%{_datadir}/applications/org.example.Quark.desktop
install -m 0644 icons/hicolor/scalable/apps/org.example.Quark.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/
# Fetch only Python 3.11 wheels compatible with glibc 2.28 (Red OS 8).
python3.11 -m pip install --disable-pip-version-check --no-compile \
    --only-binary=:all: --platform manylinux_2_28_x86_64 \
    --platform manylinux_2_17_x86_64 \
    --implementation cp --python-version 3.11 --abi cp311 \
    --target %{buildroot}%{_datadir}/quark/vendor -r requirements.txt
rm -rf %{buildroot}%{_datadir}/quark/vendor/bin
find %{buildroot}%{_datadir}/quark -type d -name __pycache__ -exec rm -rf '{}' +

%files
%license LICENSE
%{_bindir}/quark
%{_datadir}/applications/org.example.Quark.desktop
%{_datadir}/icons/hicolor/scalable/apps/org.example.Quark.svg
%{_datadir}/quark/

%changelog
* Tue Sep 29 2026 Quark contributors - 0.3.0-1
- Rename OFChat to Quark; rich messages, file transfer, receipts and groups

* Mon Sep 28 2026 OFChat contributors - 0.2.0-1
- Add tray, broadcast, paged searchable history and conversation export

* Mon Sep 28 2026 OFChat contributors - 0.1.1-1
- Remove build-machine paths from RPM dependencies

* Mon Sep 28 2026 OFChat contributors - 0.1.0-1
- Initial RPM release
