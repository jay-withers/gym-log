# Running Health on the home PC

Everything on one machine: the app, the weekly insight, the daily Garmin sync,
and the log itself on local disk. The log is snapshotted hourly to an external
drive. No Azure. Written for a Dell OptiPlex 5060, but nothing here is specific
to it beyond being x86-64, which is what the published image is built for.

```
/opt/health/            compose.yaml, backup.sh, .env (secrets, 0600)
/srv/health/data/       gymlog.json, gymlog.chat.json, gymlog.garmin-session.json
/mnt/backup/health/     the external drive: one directory per snapshot
```

It's reachable from anywhere at `https://health.jaywithers.uk` through a
[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/):
an outbound connection from the PC to Cloudflare, so no router ports are
opened and your home IP isn't exposed. **Cloudflare Access** sits in front, so
nobody reaches even the login page without first proving they're you by
email code.

## 1. The machine

- Install **Ubuntu Server LTS** (no desktop needed), with OpenSSH enabled.
- In the BIOS (F2 at boot):
  - **Power Management → AC Recovery → Power On.** It comes back by itself
    after a power cut.
  - **C-States Control → on.** This is most of the difference between ~10W
    and ~20W at idle.
- Install Docker Engine and the compose plugin
  ([docs.docker.com/engine/install/ubuntu](https://docs.docker.com/engine/install/ubuntu/)).

## 2. The backup drive

Mount it by UUID, with `nofail` so the PC still boots when it's unplugged:

```bash
sudo blkid                              # find the drive's UUID and type
sudo mkdir -p /mnt/backup
echo 'UUID=<uuid> /mnt/backup <ext4|exfat|ntfs3> defaults,nofail 0 2' | sudo tee -a /etc/fstab
sudo mount -a && mountpoint /mnt/backup
```

ext4 is best if the drive is only for this. exFAT or NTFS work too, if it also
needs to plug into a Windows or Mac machine.

## 3. Create the tunnel

`jaywithers.uk` must use Cloudflare's nameservers. If it doesn't yet: add the
site on Cloudflare's free plan, check the imported DNS records match the
current ones (including the Azure CNAME and `asuid` TXT for `health`, so
nothing breaks before the cut-over), then change the nameservers at your
registrar.

Then, in the Cloudflare dashboard, go to **Zero Trust → Networks → Tunnels →
Create a tunnel**. Choose the **Cloudflared** type and name it `health`. From
the install command it shows, copy only the token (the long value after
`--token`); compose runs `cloudflared` itself. Skip the public hostname for
now; that's step 5.

## 4. Install

From a checkout, on your own machine (`HOME_HOST` is an ssh destination: an
alias in `~/.ssh/config` or `user@host`):

```bash
make install-home HOME_HOST=optiplex
```

This copies `deploy/home` across and runs `install.sh` there as root. That
puts the files in place, creates `/srv/health/data` owned by uid 10001 (the
image's user), and enables the three timers. It's safe to re-run after
changing any file in this directory. The first time, it also creates
`/opt/health/.env` from `.env.example`.

Fill in the secrets, then start it:

```bash
ssh optiplex
sudo nano /opt/health/.env              # IMAGE_TAG, TUNNEL_TOKEN, APP_PASSCODE, DEEPSEEK_API_KEY, GARMIN_*
cd /opt/health && sudo docker compose up -d
curl -s localhost:8000/readyz           # {"status":"ok","storage":true}
sudo docker compose logs cloudflared    # "Registered tunnel connection" x4
```

The tunnel should now show as **Healthy** in the dashboard.

## 5. Put Access in front, then add a hostname

Do Access **first**, so there's never a moment where the app is public with
only the passcode in front of it. The login has no rate limiting.

**Access:** Zero Trust → Access → Applications → **Add an application →
Self-hosted**.
- Domain: `health-home.jaywithers.uk` and `health.jaywithers.uk` (add both).
- Session duration: 1 month, so the email code isn't asked for between sets.
- Policy: **Allow**, include **Emails** → your address.

Under Settings → Authentication, the default "One-time PIN" login method is
enough.

**Hostname, for testing:** Networks → Tunnels → `health` → **Public Hostname
→ Add**.
- Subdomain `health-home`, domain `jaywithers.uk`.
- Service type **HTTP**, URL **`gymlog:8000`**. That's the compose service by
  name, not `localhost`: `localhost` inside the tunnel container is the tunnel
  container itself.

Open `https://health-home.jaywithers.uk` on the phone, on mobile data. You
should get Cloudflare's email-code page, then the app's passcode page, then
the app. Log in, then reload: you should stay logged in.

`health.jaywithers.uk` itself is switched over in step 6, once the log is
moved, because adding it is the moment the phone stops talking to Azure.

## 6. Move the log off Azure and switch the domain (once)

Stop logging on the Azure app first, so nothing is written there after the
copy. Then, from a checkout with `az login` done and `terraform` initialised:

```bash
make migrate-home HOME_HOST=optiplex
```

This downloads `gymlog.json`, plus `chat.json` and `garmin-session.json` if they
exist, renames them to the names the app uses locally, stops the app on the
PC, installs them owned by uid 10001, and starts it again. Then:

```bash
ssh optiplex 'sudo systemctl start health-backup && ls /mnt/backup/health'
```

and check the history at `https://health-home.jaywithers.uk` matches what
Azure showed.

Then switch the real domain. In DNS, delete the `health` CNAME (pointing at
`*.azurecontainerapps.io`) and the `asuid.health` TXT record. Then add a
second public hostname on the tunnel: subdomain `health`, service
`http://gymlog:8000`. Cloudflare creates the new DNS record itself. Once it
works, remove the `health-home` hostname and its DNS record.

The Azure app keeps running, unreachable at that name, until it's retired.
Its managed certificate will fail to renew, which is harmless until then.

## Updating

```bash
make deploy-home HOME_HOST=optiplex IMAGE_TAG=v0.19.0
```

This rewrites `IMAGE_TAG` in `/opt/health/.env`, pulls, and recreates the
container. The tag is required, same as `make deploy`.

## Scheduled work

| Timer | When | Does |
| --- | --- | --- |
| `health-insight.timer` | Sundays 20:00 UTC | `gymlog insight` |
| `health-garmin.timer` | daily 05:00 UTC | `gymlog garmin-sync` |
| `health-backup.timer` | hourly | `backup.sh` |

All are `Persistent=true`: a run missed while the PC was off happens at next
boot. `systemctl list-timers 'health-*'` shows when each runs next.
`journalctl -u health-garmin` shows what the last run did. `systemctl --failed`
is where a broken one shows up, including a backup that found the drive
unplugged.

## Backups

`backup.sh` copies every `gymlog*.json` into
`/mnt/backup/health/<UTC timestamp>/` along with a `SHA256SUMS`, but only when
something changed since the newest snapshot. So most hourly runs write
nothing. Snapshots older than 180 days are pruned, but the newest 10 are
always kept.

It **refuses to run** if `/mnt/backup` isn't mounted (otherwise it would fill
an empty directory on the internal disk and call that a backup), or if
`gymlog.json` isn't valid JSON.

The snapshots include the Garmin session file, which holds OAuth tokens, so
treat the drive as holding secrets.

**One drive beside the PC protects against the PC's disk dying, not against
fire, theft or a power surge that takes both.** For that, keep a second drive
somewhere else and swap them occasionally. The script doesn't care which one
is mounted.

### Restoring

```bash
cd /opt/health && sudo docker compose stop gymlog
ls /mnt/backup/health                   # pick a snapshot
sudo install -o 10001 -g 10001 -m 0600 /mnt/backup/health/<snapshot>/gymlog*.json /srv/health/data/
sudo docker compose start gymlog
```

Rehearse this once, before you need it.

## Troubleshooting

- **Saves fail with a 500, and the logs say permission denied:**
  `/srv/health/data` isn't owned by 10001. Re-run `make install-home`, or
  `sudo chown -R 10001:10001 /srv/health/data`.
- **Logged in, but every page goes back to the login:** the request isn't
  arriving over HTTPS. Use the `https://` URL, and check the hostname's
  service type is HTTP (Cloudflare does the HTTPS).
- **"Bad gateway" (502) from Cloudflare:** the hostname's URL isn't
  `gymlog:8000`, or the app container is down (`sudo docker compose ps`).
- **Home-screen icon opens with browser bars:** add it to the home screen
  *after* logging in through Access, so the manifest fetch is authenticated.
- **Admin from outside the house:** the tunnel only carries the app. For ssh
  from elsewhere, add the PC as a Cloudflare Access SSH application, or
  install Tailscale just for that.
- **The insight or Garmin timer fails immediately:** a secret is missing from
  `.env`. `journalctl -u health-insight` names it.
