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

The phone reaches it over [Tailscale](https://tailscale.com), privately. No
router ports are opened and there's no public URL.

## 1. The machine

- Install **Ubuntu Server LTS** (no desktop needed), with OpenSSH enabled.
- In the BIOS (F2 at boot):
  - **Power Management → AC Recovery → Power On.** It comes back by itself
    after a power cut.
  - **C-States Control → on.** This is most of the difference between ~10W
    and ~20W at idle.
- Install Docker Engine and the compose plugin
  ([docs.docker.com/engine/install/ubuntu](https://docs.docker.com/engine/install/ubuntu/)).
- Install Tailscale (`curl -fsSL https://tailscale.com/install.sh | sh`), then
  `sudo tailscale up`.

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

## 3. Install

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
sudo nano /opt/health/.env              # IMAGE_TAG, APP_PASSCODE, DEEPSEEK_API_KEY, GARMIN_*
cd /opt/health && sudo docker compose up -d
curl -s localhost:8000/readyz           # {"status":"ok","storage":true}
```

## 4. Reach it from the phone

The session cookie is `Secure`, so the app **must** be served over HTTPS. Over
plain `http://<tailscale-ip>:8000` the login appears to work, then every page
sends you back to it, because the browser never returns the cookie over http.

In the Tailscale admin console, under **DNS**, turn on MagicDNS and HTTPS
certificates. Then, on the PC:

```bash
sudo tailscale serve --bg http://127.0.0.1:8000
tailscale serve status                  # prints https://<pc-name>.<tailnet>.ts.net
```

The setting persists across reboots. Install Tailscale on the phone, sign in,
open that URL, and add it to the home screen.

## 5. Move the log off Azure (once)

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

and check the history in the app matches what Azure showed.

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
- **Logged in, but every page goes back to the login:** you're on http, not
  the `https://….ts.net` URL. See step 4.
- **The insight or Garmin timer fails immediately:** a secret is missing from
  `.env`. `journalctl -u health-insight` names it.
