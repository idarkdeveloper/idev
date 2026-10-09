# Running live trading from a fixed IP

SEBI's 2026 rules for API trading mean Groww accepts orders only from an IP address you
have registered with them. GitHub Actions changes IP on every run, so the scheduled routine
can keep recommending and paper-trading there, but **live orders need a machine with a fixed
public IP**. Pick one of the three routes below.

Nothing here turns live trading on. Real orders still need both `AUTO_TRADE=true` and
`GROWW_LIVE_ORDERS=true`, exactly as before.

## Trading NSE from outside India (e.g. Germany)

- **Account first.** If you are a non-resident Indian under FEMA, trading from a regular
  resident account isn't allowed; NRIs use NRE/NRO accounts with PIS permission through a
  broker that supports them. Confirm with Groww which account you hold. (Not legal advice.)
- **Prefer a server in India (route B, Mumbai or Bangalore region).** Groww then sees a
  fixed Indian IP; home connections abroad usually change IP, and Groww may not accept a
  foreign IP for API orders at all — check in Groww's API settings before using route A.
- **Market hours in Germany:** 09:15–15:30 IST is 05:45–12:00 in German summer time and
  04:45–11:00 in winter time (from the last Sunday of October). The agent keeps India time.
- **Taxes:** India taxes the gains; Germany may too if you are tax-resident there. The
  India–Germany tax treaty decides; ask a tax adviser.

## Step 1 – find out whether your IP is fixed

On the machine you want to use:

```
python -m trading_agent groww-check --ip
```

It prints the public IP that Groww would see. Run it on a few different days and after
restarting your router. If it never changes, that address can be registered. If it changes,
your connection has a dynamic IP: ask your internet provider for a static IP (usually a paid
add-on or a business plan), or use route B or C.

## Route A – your laptop (Windows)

Works when your home connection has a static IP.

1. Register the IP from step 1 in Groww's API settings, then put it in `.env`:
   `GROWW_ALLOWED_IP=<your IP>`
2. Install the auto-start task (once):
   `.\deploy\windows\install-autostart.ps1`
   Watch mode then starts at every login, runs on NSE trading days only, and keeps the
   laptop awake during market hours (it sleeps normally the rest of the time).
3. Keep the laptop on, plugged in and online from 09:15 to 15:30 IST on trading days.
   Closing the lid usually sleeps it regardless; set "When I close the lid: Do nothing"
   for the plugged-in case in Windows power settings if you want to close it.

To remove the task: `.\deploy\windows\install-autostart.ps1 -Remove`.

## Route B – a small rented server (recommended if your IP isn't fixed, or you live abroad)

Any provider that gives a static/reserved public IP works; choose an **Indian region**
(AWS Lightsail or EC2 in Mumbai, DigitalOcean in Bangalore) so Groww sees an Indian IP.
Rough prices (October 2026): about ₹400–600 a month; 1 GB of memory is enough.

1. Create an Ubuntu server with a reserved/static IP. Note the IP.
2. Register that IP in Groww's API settings.
3. On the server:
   ```
   git clone <your repo> trading-agent && cd trading-agent
   cp .env.example .env    # fill in your keys, and GROWW_ALLOWED_IP=<the server's IP>
   docker compose -f deploy/docker-compose.yml up -d --build
   ```
   (Without Docker: use the two files in `deploy/systemd/`; each explains how.)
4. Open the dashboard through an SSH tunnel from your laptop, never the open internet:
   ```
   ssh -L 8787:127.0.0.1:8787 <user>@<server IP>
   ```
   then browse to http://127.0.0.1:8787/.

## Route C – keep GitHub Actions, add a fixed-IP proxy

Keep the scheduled routine on GitHub and send only its Groww calls through a proxy that has
a fixed IP (for example a tiny server from route B running a proxy, or a paid static-IP
proxy). Add two repository secrets:

- `GROWW_PROXY_URL` = `http://user:password@<proxy IP>:<port>`
- `GROWW_ALLOWED_IP` = the proxy's IP (registered with Groww)

Every Groww request, including the IP check below, then goes through the proxy.

## The IP check before every live order

When `GROWW_ALLOWED_IP` is set, every call that could place, change or cancel an order first
looks up the machine's public IP (through the same proxy as the Groww calls, cached for ten
minutes). If it doesn't match, or can't be found, the order is not sent and you get a clear
message instead of a rejection from Groww. With `GROWW_ALLOWED_IP` unset, nothing changes.
