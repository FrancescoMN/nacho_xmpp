# Tor XMPP Desktop Client (Prosody Onion)

Desktop client written in Python for a Prosody onion deployment.

Features:
- Launches an internal Tor client process at app startup
- Generates and uses a dedicated `torsocks` config bound to that Tor SOCKS port
- Registers users with XMPP in-band registration (XEP-0077)
- 1:1 text messaging
- File upload using XMPP HTTP Upload (XEP-0363), then sends upload URL in chat

## Security model

- Network operations are executed by a worker process launched via `torsocks`.
- DNS resolution for onion/service hosts is routed via Tor because the worker always runs under `torsocks` with app-managed SOCKS endpoint.
- Current default uses insecure TLS verification for self-signed onion cert deployments. Replace with certificate pinning before production use.

## Requirements

- `python3`
- `tkinter` (`python3-tk` package on Debian/Ubuntu)
- `tor`
- `torsocks`
- Python packages from `requirements.txt`

## Install dependencies

```bash
sudo apt-get update
sudo apt-get install -y python3-tk tor torsocks

cd /home/francesco/prosody-onion-secure/torchat_client
python3 -m pip install -r requirements.txt
```

## Run

```bash
cd /home/francesco/prosody-onion-secure/torchat_client
python3 -m app.main
```

## Usage flow

1. Start app (it will start Tor sidecar).
2. Register a new user: provide `domain`, `username`, `password`, click `Register`.
3. Connect with the same credentials.
4. Set `Peer JID` and send messages.
5. Click `Send File` to upload file over XMPP HTTP Upload and send link.
