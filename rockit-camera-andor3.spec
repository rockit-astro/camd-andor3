Name:      rockit-camera-andor3
Version:   %{_version}
Release:   1
Summary:   Control code for Andor CMOS cameras
Url:       https://github.com/rockit-astro/camd-andor3
License:   GPL-3.0
BuildArch: noarch

%description


%build
mkdir -p %{buildroot}%{_bindir}
mkdir -p %{buildroot}%{_unitdir}
mkdir -p %{buildroot}%{_sysconfdir}/camd
mkdir -p %{buildroot}%{_udevrulesdir}

%{__install} %{_sourcedir}/andor3_camd %{buildroot}%{_bindir}
%{__install} %{_sourcedir}/andor3_camd@.service %{buildroot}%{_unitdir}
%{__install} %{_sourcedir}/ngts_m06.json %{buildroot}%{_sysconfdir}/camd
%package server
Summary:  Andor CMOS camera server
Group:    Unspecified
Requires: python3-rockit-camera-andor3 libusb
%description server

%files server
%defattr(0755,root,root,-)
%{_bindir}/andor3_camd
%defattr(0644,root,root,-)
%{_unitdir}/andor3_camd@.service

%package data-ngts-m06
Summary: Andor camera data for NGTS M06
Group:   Unspecified
%description data-ngts-m06

%files data-ngts-m06
%defattr(0644,root,root,-)
%{_sysconfdir}/camd/ngts_m06.json

%changelog
