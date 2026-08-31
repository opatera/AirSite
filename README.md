# AirSite

AirOwen's Website for Learning.

## Run locally

Start the combined static site and AirServer upload service:

```bash
python3 server.py --host 0.0.0.0 --port 8080
```

Then open the server in a browser and use the `AirServer` tab to browse and upload into:

`/media/airowen/Storage/share`

## Deploy with Nginx

This repo includes deployment files in `deploy/`:

- `deploy/airsite.service` runs the Python app with `systemd` on `127.0.0.1:8080`
- `deploy/airsite.nginx.conf` reverse-proxies Nginx on port `80` to the app

Use the repo-root deploy command to publish updates:

```bash
./airdeploy
```

That command:

- fast-forwards from `origin` first when the repo is clean
- skips `git pull` when you have local edits and deploys the current working tree as-is
- installs or updates the `systemd` unit
- installs or updates the Nginx site config
- ensures the AirSite Nginx site symlink exists
- restarts the Python app
- validates and reloads Nginx

## HTTPS

Generate a self-signed certificate for private-network use:

```bash
sudo ./deploy/generate-cert.sh AirServer 192.168.1.50
```

Replace `192.168.1.50` with your server's LAN IP if you access the site by IP. You can add more hostnames or IPs as extra arguments.

Then deploy normally:

```bash
./airdeploy
```

Notes:

- HTTP on port `80` now redirects to HTTPS on port `443`
- browsers will warn about a self-signed cert unless you trust it on your devices
