# AirRetro

AirRetro is a local-first music player and Game Boy Advance library. It opens a localhost web app where each person chooses the folders containing their music and `.gba` ROMs. Nothing needs to be uploaded to a service.

## Run from source

```bash
python3 server.py --desktop
```

AirRetro opens at `http://airretro.localhost:8080`. If that port is already in use, it automatically uses the next available port and prints the exact URL. The reserved `.localhost` domain always points back to your own computer, so it does not need DNS or hosts-file setup. On the Home page, enter the absolute paths to your music directory and GBA ROM directory. The choices are saved locally in `~/.airretro/settings.json`.

## Build a Windows app

Install [Inno Setup 6](https://jrsoftware.org/isinfo.php) once, then run `build-windows.bat` on Windows. It installs PyInstaller if necessary and produces both `dist/AirRetro.exe` and `dist/AirRetro-Setup-0.1.0.exe`.

The setup executable installs AirRetro under `Program Files`, creates a Start Menu shortcut, offers an optional Desktop shortcut, and includes an uninstaller. User library settings remain in the user's profile when the app is uninstalled.

## Test

```bash
python3 -m unittest -v test_server.py
```

## Publish a GitHub release

Pushing a semantic version tag triggers `.github/workflows/release.yml`. GitHub Actions tests the app, builds the native Linux binary and Windows installer, generates `SHA256SUMS.txt`, and attaches them to a GitHub Release.

```bash
git tag -a v0.1.0 -m "AirRetro first release"
git push origin v0.1.0
```

The workflow requires GitHub Actions to have read/write workflow permissions, which is configured through its `contents: write` permission.
