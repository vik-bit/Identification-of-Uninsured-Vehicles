# Free tunnel setup (no Tailscale)

Expose your warehouse ingest API (`localhost:8080`) to the internet so your teammate can push.

**Keep both windows open while testing:**
1. `ingest_api.py`
2. the tunnel command

---

## Option A — Cloudflare quick tunnel (free, no account)

Already downloaded for you at:

`tools\cloudflared.exe`

### You run

```powershell
cd "C:\Users\vikra\Downloads\Identification-of-Uninsured-Vehicles\Identification-of-Uninsured-Vehicles"

# terminal 1
..\venv\Scripts\python.exe ingest_api.py

# terminal 2
.\tools\cloudflared.exe tunnel --url http://127.0.0.1:8080
```

Wait for a line like:

```text
https://random-words.trycloudflare.com
```

### Tell teammate

```text
WAREHOUSE_API=https://random-words.trycloudflare.com
API_KEY=iia-ingest-key
```

(Use **https**, no `:8080`.)

If cloudflared times out on your network, use Option B.

---

## Option B — ngrok (free account, ~2 minutes)

Binary is at `tools\ngrok.exe`.

1. Sign up free: https://dashboard.ngrok.com/signup  
2. Copy your authtoken: https://dashboard.ngrok.com/get-started/your-authtoken  
3. On your PC:

```powershell
cd "C:\Users\vikra\Downloads\Identification-of-Uninsured-Vehicles\Identification-of-Uninsured-Vehicles"
.\tools\ngrok.exe config add-authtoken YOUR_TOKEN_HERE
.\tools\ngrok.exe http 8080
```

You’ll get a URL like `https://abc123.ngrok-free.app`.

### Tell teammate

```text
WAREHOUSE_API=https://abc123.ngrok-free.app
API_KEY=iia-ingest-key
```

He may need to open that URL once in a browser (ngrok free interstitial), then run `push_to_warehouse.py`.

---

## Teammate script change

```python
WAREHOUSE_API = "https://YOUR-TUNNEL-URL"   # no port
API_KEY = "iia-ingest-key"
```

```powershell
python push_to_warehouse.py
```

---

## Checklist

- [ ] `ingest_api.py` running  
- [ ] tunnel running and showing a public `https://...` URL  
- [ ] teammate health check: browser or `curl https://YOUR-URL/health` → `{"ok":true...}`  
- [ ] teammate push succeeds  

Tunnel URL changes every time you restart a **quick** tunnel — send him the new URL each session.
