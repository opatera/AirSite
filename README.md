# AirRetro

AirRetro is a local-first music player and Game Boy Advance library. It opens a localhost web app where each person chooses the folders containing their music and `.gba` ROMs. Nothing needs to be uploaded to a service.

## Run from source

```bash
python3 server.py --desktop
```

AirRetro opens at `http://airretro.localhost:8080`. If that port is already in use, it automatically uses the next available port and prints the exact URL. The reserved `.localhost` domain always points back to your own computer, so it does not need DNS or hosts-file setup. On the Home page, enter the absolute paths to your music directory and GBA ROM directory. The choices are saved locally in `~/.airretro/settings.json`.

## Build a Windows app

On Windows, run `build-windows.bat`. It installs PyInstaller if necessary and produces `dist/AirRetro.exe`. Double-clicking the executable launches the local server and opens the app in the default browser.

## Test

```bash
python3 -m unittest -v test_server.py
```

## Publish a GitHub release

Pushing a semantic version tag triggers `.github/workflows/release.yml`. GitHub Actions tests the app, builds native Linux and Windows binaries, generates `SHA256SUMS.txt`, and attaches all three files to a GitHub Release.

```bash
git tag -a v0.1.0 -m "AirRetro first release"
git push origin v0.1.0
```

The workflow requires GitHub Actions to have read/write workflow permissions, which is configured through its `contents: write` permission.
