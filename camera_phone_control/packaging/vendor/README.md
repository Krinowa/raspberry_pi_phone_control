# Vetted Windows tools

Place an approved, self-contained Windows x64 `ffmpeg.exe` in this directory
before every production build. It is used to enumerate DirectShow camera names
and alternative monikers. The build validates the PE architecture, execution,
and `dshow` support, then installs it as
`PhoneController\_internal\tools\ffmpeg.exe`. The frozen application does not
download FFmpeg and does not fall back to PATH if the bundled file is absent.

Use a static/self-contained build. The current package does not collect or
validate companion FFmpeg DLLs, so dynamically linked builds are unsupported.
Record the supplier URL, version, license, configuration and SHA-256
digest in the release record. Retain required LGPL/GPL notices and satisfy the
corresponding-source/source-offer terms for the selected build. Do not add an
unreviewed binary to the repository.

Optional local Android tooling can also be supplied here as `adb.exe`,
`AdbWinApi.dll`, and `AdbWinUsbApi.dll`; the Raspberry Pi control path does not
need those files.
