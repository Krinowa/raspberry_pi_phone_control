# Vetted Windows tools

Place an approved Windows x64 `ffmpeg.exe` in this directory before a production
build. It is used only to enumerate DirectShow camera names. The application
does not download FFmpeg, and the source/runtime fallback to an integer camera
index remains available when it is absent.

Record the supplier, version, license and SHA-256 digest in the release record.
Do not add an unreviewed binary to the repository.

Optional local Android tooling can also be supplied here as `adb.exe`,
`AdbWinApi.dll`, and `AdbWinUsbApi.dll`; the Raspberry Pi control path does not
need those files.
