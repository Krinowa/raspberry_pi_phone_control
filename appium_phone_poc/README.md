# Appium phone connectivity proof of concept

Run Python and Appium on the same macOS machine or Ubuntu/Linux mini PC, with a
physical Android phone attached by USB. The current `main.py` launches Maybank MAE
(`com.maybank2u.life`), optionally acknowledges the specific warning below once,
prints UI node counts without screen text, and closes the session.

The requested acknowledgement applies only to this message:

> Our anti-malware tool has identified app(s) that may put your MAE app at risk

If that wording and the unique, visible, enabled, clickable **Proceed Anyway**
element are exposed by MAE, the script clicks once and waits two seconds. It matches
resource ID `app_shield_error_undefined`, class `android.view.ViewGroup`, package
`com.maybank2u.life`, and accessibility description `Proceed\u00a0Anyway` (a
non-breaking space, normalized to an ordinary space). It never uses the temporary
`ELEMENT 40` index. This explicitly
continues despite the malware-risk warning; it does not remove the reported risk.
It performs no subsequent navigation, Back, login, PIN, OTP, biometric, transfer,
or payment action. The acknowledgement was checked with simulated UI responses;
it has not been tested against this screen on the phone.

## macOS setup on this Mac

Environment inspected and setup verified on 2026-09-14:

| Tool | Before setup | Result |
| --- | --- | --- |
| macOS / architecture | 15.6 (24G84), arm64 | Apple Silicon |
| Homebrew | 6.0.18 at `/opt/homebrew` | Existing installation used |
| Python | 3.13.13 | Preserved; project `venv` created |
| Node.js / npm | 24.19.0 / 11.17.0, through NVM | Preserved |
| Java | No runtime found | Homebrew OpenJDK 17.0.20.1 installed |
| ADB | Missing | Homebrew platform-tools 37.0.1, ADB 1.0.41 installed |
| Appium | Missing | 2.19.0 installed in the existing NVM Node environment |
| UiAutomator2 | Missing | 4.2.9 installed |
| Android SDK | Missing | Command-line tools, build-tools 35.0.0, emulator executable installed |
| Python Appium client | No project virtual environment | 5.3.1 installed in `venv` |

Homebrew Python 3.14 was also already present; the existing active Python 3.13
was used without reinstalling Python or changing the default. No Node version or
NVM configuration was changed.

The missing tools were installed with:

```zsh
brew install openjdk@17 android-platform-tools android-commandlinetools
npm install -g appium@2.19.0
appium driver install uiautomator2@4.2.9
```

### Java and SDK environment

**`~/.zshrc` was not modified.** Add exactly these lines to it for persistence,
or run them in each terminal used for this project:

```zsh
export JAVA_HOME="/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home"
export ANDROID_HOME="/opt/homebrew/share/android-commandlinetools"
export PATH="$JAVA_HOME/bin:$ANDROID_HOME/platform-tools:$ANDROID_HOME/cmdline-tools/latest/bin:$PATH"
```

After adding the lines yourself, run `source ~/.zshrc` in both terminals.
The explicit JAVA_HOME uses the [Homebrew JDK](https://formulae.brew.sh/formula/openjdk@17)
without registering a system-wide JDK symlink. `/usr/libexec/java_home -v 17`
may still report no registered JDK; the Java commands above work with this JAVA_HOME.
The SDK path follows [Homebrew's command-line tools installation](https://formulae.brew.sh/cask/android-commandlinetools).

To reuse the installed ADB rather than download another copy, setup created:

```text
/opt/homebrew/share/android-commandlinetools/platform-tools
  -> /opt/homebrew/Caskroom/android-platform-tools/37.0.1/platform-tools
```

If Homebrew later upgrades platform-tools and removes that versioned directory,
update this SDK symlink to the directory containing the new Homebrew ADB binary.
The Android SDK license was accepted and these SDK packages were installed:

```zsh
sdkmanager --sdk_root="$ANDROID_HOME" 'build-tools;35.0.0' emulator
```

Doctor 4.2.9 checks for the emulator executable even for physical-device testing.
Only its tools were installed: no emulator image or virtual device was created or
run. The current SDK prints a `sdkmanager` deprecation notice; installation succeeded.

### Verification results and next run

```zsh
java -version
javac -version
adb version
appium --version
appium driver list --installed
appium driver doctor uiautomator2
```

Verified: Appium **2.19.0**, UiAutomator2 **4.2.9**, Java **17.0.20.1**.
Doctor reported **0 required fixes** and **3 optional fixes**:

- `bundletool.jar`: only needed for Android App Bundle handling.
- `ffmpeg`: only needed for screen recording.
- GStreamer executables: only needed for screen streaming.

These optional tools were left uninstalled. Appium successfully loaded UiAutomator2
and returned HTTP 200 with `value.ready: true` from `http://127.0.0.1:4723/status`.
The verification server was then stopped. Python dependency checks passed.

`adb devices -l` showed **no connected phone**. Running the prototype reported
`No authorized physical USB Android device found` and exited with status 1.
No app was launched or clicked; real-phone session creation/navigation remains untested.

After applying the environment lines, start Appium in terminal 1:

```zsh
appium --address 127.0.0.1 --port 4723 --log-level info
```

Connect a USB phone and manually enable/authorize USB debugging as described in
section 4 below. In terminal 2, also with the environment configured:

```zsh
cd /Users/weizhe/Documents/phone_controller/appium_phone_poc
source venv/bin/activate
adb devices -l
python main.py
```

The original setup verification used Android Settings. The current script targets
MAE as described above; section 7 documents its configuration. Skip the Ubuntu
installation steps on this Mac. The Settings commands in section 5 are examples
for identifying Android package/activity names.

## 1. Linux prerequisites

These commands use Bash on Ubuntu 22.04/24.04, Python 3.10+, and an x86_64 mini PC.
Install Java, Python, ADB, and USB permission rules:

```bash
sudo apt update
sudo apt install -y curl unzip openjdk-17-jdk python3 python3-venv python3-pip adb android-sdk-platform-tools-common
sudo usermod -aG plugdev "$USER"
```

Log out and back in after changing group membership, then reconnect the USB cable.

Check `node --version` and `npm --version`. If needed, install Node 22 using
[nvm's versioned installer](https://github.com/nvm-sh/nvm/tree/v0.40.3):

```bash
curl -fsSLo /tmp/nvm-install.sh https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh
bash /tmp/nvm-install.sh
export NVM_DIR="$HOME/.nvm"
source "$NVM_DIR/nvm.sh"
nvm install 22
nvm use 22
nvm alias default 22
node --version
npm --version
```

## 2. Android SDK and Java

ADB alone is insufficient for UiAutomator2. Download the **Linux command line tools**
ZIP from the [official Android downloads page](https://developer.android.com/studio#command-tools),
accept its license, and save it as `~/Downloads/android-commandlinetools.zip`.
For a fresh SDK directory:

```bash
mkdir -p "$HOME/Android/Sdk/cmdline-tools"
unzip "$HOME/Downloads/android-commandlinetools.zip" -d "$HOME/Android/Sdk/cmdline-tools"
mv "$HOME/Android/Sdk/cmdline-tools/cmdline-tools" "$HOME/Android/Sdk/cmdline-tools/latest"
```

Add these lines to `~/.bashrc` and run them in both the server and Python terminals.
The JAVA_HOME expression finds the selected Java installation; ensure `java -version`
reports Java 17 or newer.

```bash
export JAVA_HOME="$(dirname "$(dirname "$(readlink -f "$(command -v java)")")")"
export ANDROID_HOME="$HOME/Android/Sdk"
export PATH="$ANDROID_HOME/platform-tools:$ANDROID_HOME/cmdline-tools/latest/bin:$PATH"
```

Install SDK packages and review/accept the SDK licenses interactively:

```bash
sdkmanager --sdk_root="$ANDROID_HOME" "platform-tools" "platforms;android-35" "build-tools;35.0.0"
sdkmanager --licenses
java -version
adb version
command -v adb
```

No emulator/system image is needed. If you already have an SDK, use its path as
ANDROID_HOME instead of creating another. See the official
[sdkmanager instructions](https://developer.android.com/tools/sdkmanager).

## 3. Install Appium 2 and UiAutomator2

Use these versioned commands for this project:

```bash
npm install -g appium@2.19.0
appium driver install uiautomator2@4.2.9
appium --version
appium driver list --installed
appium driver doctor uiautomator2
```

Expect Appium `2.19.0` and UiAutomator2 `4.2.9`. Resolve required doctor failures
before running; optional video/streaming tools are unnecessary. Do not use `sudo`
for npm with nvm. If you already installed a different UiAutomator2 version, use
`appium driver uninstall uiautomator2` before installing the version above.

The generic commands are `npm install -g appium` and
`appium driver install uiautomator2`, but they select current releases. This project
pins versions because [UiAutomator2 5+ requires Appium 3](https://github.com/appium/appium-uiautomator2-driver#requirements).

## 4. Connect and authorize the phone

1. On the phone, open Settings → About phone and tap **Build number** seven times
   to enable Developer Options (the location varies by manufacturer).
2. In Developer Options, enable **USB debugging**.
3. Connect a USB data cable to the mini PC. Unlock the phone manually and accept
   the **Allow USB debugging** prompt for this computer.
4. Keep the phone awake and manually unlocked while testing. Leave its security
   mechanisms enabled.

```bash
adb devices
adb devices -l
```

The serial must show state `device`, not `unauthorized` or `offline`. The detailed
listing must include `usb:`. The script rejects emulators and network transports.
With multiple phones, set `export ANDROID_UDID='YOUR_ADB_SERIAL'`.
See Android's [device connection and ADB documentation](https://developer.android.com/tools/adb).

## 5. Determine package and activity

Open the installed app manually to its harmless starting screen. List packages:

```bash
adb shell pm list packages
adb shell pm list packages | grep -i settings
adb shell pm path com.android.settings
```

Find the foreground activity (the field name varies across Android versions):

```bash
adb shell dumpsys activity activities | grep -E 'mResumedActivity|topResumedActivity'
adb shell dumpsys window windows | grep -E 'mCurrentFocus|mFocusedApp'
```

For example, `com.android.settings/.Settings` gives package
`com.android.settings` and activity `.Settings`. A current internal activity may
not be exported/launchable. Resolve the package's launcher activity when needed:

```bash
adb shell cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER com.android.settings
```

Use that exported launcher component in config. The foreground activity printed
after launch may differ if the app redirects. Do not attempt to start protected
activities. With multiple devices, add `-s YOUR_ADB_SERIAL` immediately after `adb`
to every diagnostic command above.

## 6. Start Appium

In a separate terminal with the SDK/Java/Node environment configured:

```bash
appium --address 127.0.0.1 --port 4723 --log-level info
```

The basic startup command is `appium`; the command above binds it to localhost
and limits verbose logs. Leave it running. Check from another terminal:

```bash
curl -fsS http://127.0.0.1:4723/status
```

Expect `value.ready: true`. Appium 2's default base path is `/`, so config uses
`http://127.0.0.1:4723`, without `/wd/hub`. Use a matching URL if you intentionally
change the server port/base path. The server must use the same PC/ADB device as Python.

## 7. Configure and run

```bash
cd appium_phone_poc
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

The current script uses constants in `main.py`: `APPIUM_SERVER_URL`, `APP_PACKAGE`,
`MAE_WARNING_TEXT`, `PROCEED_BUTTON_TEXT`, `PROCEED_BUTTON_ID`, and
`PROCEED_BUTTON_CLASS`. It resolves the installed app's
launcher activity through ADB. `config.py` contains the original Settings POC
configuration and is **not imported by the current script**. Disconnect other
phones before running; device selection requires a single physical phone.

```bash
python main.py
```

A successful run logs the device, session, launch, current package/activity,
warning acknowledgement or absence, UI node counts, and session closure.
Exit status is `0` for a completed flow, `1` on failure, and `130` on interruption.
Cleanup failures are logged separately. Closing the session preserves the
installed target app and its data.

## Safety and failure behavior

The only configured click is the requested acknowledgement of the exact MAE notice
above. This is an explicit exception to the original stop-on-security-warning
behavior. No credentials or financial operations are used. The script does not
alter FLAG_SECURE, screenshot protection, biometrics, PINs, or anti-automation controls.

After launch, the handler checks exposed MAE text/content descriptions for the
supplied warning (allowing line-break/spacing differences). If present, it prints
the existing diagnostic element listings and waits up to ten seconds for the
identified **Proceed Anyway** button. It rechecks the warning, foreground package,
resource ID, class, accessibility description, visibility, clickable state, and
enabled state before clicking.
A button with that label on a different page is not clicked. If no matching notice
appears, it logs that no click was performed and proceeds to inspection only.
If the warning appears but its button is unavailable/ambiguous, the run fails.
An unavailable or malformed hierarchy also stops the run. There is no screenshot,
coordinate, permission-changing, or repeated-click fallback.

These checks do not diagnose malware or prove that continuing is safe. If the app
denies automation, the script reports the error and stops.

The full hierarchy stays in memory. The warning-screen diagnostic listings print
element text and accessibility descriptions; keep those logs private. Appium itself
can also log command data, so avoid debug-level logging.
UiAutomator2 installs its standard helper APKs on the phone; it does not install,
clear, or replace the target app. No automatic permission grants or unlock are used.

| Failure | Next step |
| --- | --- |
| ADB missing / no USB phone | Install/check platform-tools, cable, USB rules, `adb devices -l` |
| Unauthorized / offline | Accept the phone's USB prompt manually / reconnect cable |
| Server unavailable | Start Appium, check `/status` and configured URL |
| Driver missing | Install the pinned UiAutomator2 version and restart Appium |
| Package/activity incorrect | Check `pm path`, foreground activity, and launcher resolution |
| Launch fails / automation denied | Review local server logs; stop if security blocks automation |
| Hierarchy unavailable / automation blocked | Stop; do not bypass protections |
| Specified warning absent | No click; inspection only |
| Warning button missing / ambiguous | Stop and review the screen manually |
| Connection lost | Inspect phone/server; the click is never retried automatically |

Cleanup is attempted in `finally`. If a connection disappears during session creation
or deletion, cleanup cannot be guaranteed; check the server and stop it if necessary.
Do not automatically rerun after an uncertain click. This project contains no
dashboard, payment queue, transfer workflow, OCR, or remote capture.
