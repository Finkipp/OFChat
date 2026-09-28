%global debug_package %{nil}

Name:           ofchat
Version:        0.1.1
Release:        1%{?dist}
Summary:        GTK XMPP client for Openfire
License:        MIT
Source0:        %{name}-%{version}.tar.gz
BuildArch:      x86_64

Requires:       python3 >= 3.11
Requires:       python3-gobject-base
Requires:       gtk3

%description
OFChat is a desktop XMPP client for Openfire with local message history,
notifications and XEP-0224 attention requests.

%prep
%autosetup

%build
# Python dependencies are installed into a private directory during packaging.

%install
install -d %{buildroot}%{_datadir}/ofchat %{buildroot}%{_bindir} %{buildroot}%{_datadir}/applications
cp -a chat main.py %{buildroot}%{_datadir}/ofchat/
install -m 0755 packaging/ofchat %{buildroot}%{_bindir}/ofchat
install -m 0644 OFChat.desktop %{buildroot}%{_datadir}/applications/org.example.OFChat.desktop
# Fetch only Python 3.11 wheels compatible with glibc 2.28 (Red OS 8).
python3.11 -m pip install --disable-pip-version-check --no-compile \
    --only-binary=:all: --platform manylinux_2_28_x86_64 \
    --platform manylinux_2_17_x86_64 \
    --implementation cp --python-version 3.11 --abi cp311 \
    --target %{buildroot}%{_datadir}/ofchat/vendor -r requirements.txt
rm -rf %{buildroot}%{_datadir}/ofchat/vendor/bin
find %{buildroot}%{_datadir}/ofchat -type d -name __pycache__ -exec rm -rf '{}' +

%files
%license LICENSE
%{_bindir}/ofchat
%{_datadir}/applications/org.example.OFChat.desktop
%{_datadir}/ofchat/

%changelog
* Mon Sep 28 2026 OFChat contributors - 0.1.1-1
- Remove build-machine paths from RPM dependencies

* Mon Sep 28 2026 OFChat contributors - 0.1.0-1
- Initial RPM release
