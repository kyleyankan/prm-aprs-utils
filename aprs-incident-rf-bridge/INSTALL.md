# Installing aprs-incident-rf-bridge as a service

This sets `aprs_radio.py` up as a systemd service on Linux, so it starts at boot, restarts after a
network drop, and keeps its settings in one file. If you only want to try the script, the
[README](README.md) shows how to run it by hand; do that first if you have never used it.

It has been tried on Debian 12. It should work on any Linux that uses systemd, including
Raspberry Pi OS, Ubuntu, Fedora and Arch.

## What you need

- **A licence that covers what you transmit.** The service transmits under your callsign, so you are
  its control operator, including while you are not at the keyboard.
- **A Linux machine with systemd and Python 3.8 or later.** Nothing else is installed; the script
  uses only what comes with Python.
- **A KISS TNC** that machine can reach: Direwolf or another software TNC with its KISS TCP port on
  (Direwolf's default is 8001), or a hardware TNC in KISS mode on a serial port.
- **The feeder's address.** K3YLE's feeder is at `https://aprs-incidents.phlm.sh`. Another feeder's
  operator will give you theirs.
- **Your position** as a latitude and longitude, and a radius in miles.

What the feeder's operator sees: each request carries your callsign, the position and radius you ask
for, and your internet address. The feeder keeps those for 7 days and shows them on its operator's map.

## Install

Get the `aprs-incident-rf-bridge` folder onto the machine (clone the repository or copy the folder), then from
inside it:

```sh
sudo sh systemd/install.sh
```

The installer:

| Puts | Where |
|---|---|
| The script, this guide and the README | `/opt/aprs-incident-rf-bridge/` |
| Your settings (created from the example the first time) | `/etc/aprs-incident-rf-bridge/aprs-incident-rf-bridge.conf` |
| The service | `/etc/systemd/system/aprs-incident-rf-bridge.service` |

It does not start the service, and it never overwrites a settings file that is already there.

## Configure

Open the settings file:

```sh
sudo nano /etc/aprs-incident-rf-bridge/aprs-incident-rf-bridge.conf
```

Each line is `name = value`. Lines starting with `#` are ignored. Set these first:

| Setting | What to put |
|---|---|
| `server` | The feeder's address |
| `callsign` | Your callsign and SSID, e.g. `W3XYZ-10`. The service refuses to start while it is `N0CALL`. |
| `lat`, `lon` | The centre of your area in decimal degrees. West and south are negative. |
| `miles` | The radius around that point |
| `transmit` | Leave it at `no` for now |

Then tell it where your TNC is. Use one of these:

```ini
# Direwolf or another software TNC (this is Direwolf on the same machine)
kiss-tcp = 127.0.0.1:8001
```

```ini
# a hardware TNC in KISS mode on a serial port
kiss-serial = /dev/ttyUSB0
baud = 9600
```

On a TNC with more than one radio, `kiss-port` picks which one (for Direwolf, the `CHANNEL` number).

Every option of `aprs_radio.py` can be set in this file under the same name without the dashes:
`min`, `max-objects`, `max-per-poll`, `path`, `auto`, `burst`, `no-bulletins`, `poll`, `refresh`,
`gap`. The example file lists them with their defaults, and the [README](README.md) explains what
each one does. Options that are on or off take `yes` or `no`.

## Start it without transmitting

With `transmit = no` the service only writes to its log what it would send. Start it and watch:

```sh
sudo systemctl start aprs-incident-rf-bridge
```

```sh
journalctl -u aprs-incident-rf-bridge -f
```

You should see something like this:

```
# aprs_radio 1.3 for W3XYZ-10 (print-only: nothing is transmitted)
# asking https://aprs-incidents.phlm.sh/api/packets?lat=40.12000&lon=-75.34000&miles=10&min=medium&via=W3XYZ-10
# every 120s; at most 10 objects, 6 packets per poll, 8s apart; refresh every 30 min
(not transmitted) new: W3XYZ-10>APZIFD:}K3YLE-15>APRS,TCPIP,W3XYZ-10*:;FIRE7K2QX*241740z4007.80N/07519.80WdBUILDING FIRE MAIN ST/OAK ST
# 1 listed, 1 carried by this station, 1 would be sent
```

Leave it for a few polls. If the list is longer than you want on the air, lower `miles`, raise `min`
or lower `max-objects`, then restart the service.

## Turn transmitting on

When the list looks right, set this in the settings file:

```ini
transmit = yes
```

```sh
sudo systemctl restart aprs-incident-rf-bridge
```

The log lines now start with the reason (`new`, `changed`, `refresh`, `kill`) with no "(not
transmitted)" in front, and your radio keys up for each one.

## Start it at every boot

```sh
sudo systemctl enable aprs-incident-rf-bridge
```

## Day to day

| To | Run |
|---|---|
| See whether it is running | `systemctl status aprs-incident-rf-bridge` |
| Follow the log | `journalctl -u aprs-incident-rf-bridge -f` |
| See today's log | `journalctl -u aprs-incident-rf-bridge --since today` |
| Apply a settings change | `sudo systemctl restart aprs-incident-rf-bridge` |
| Stop it | `sudo systemctl stop aprs-incident-rf-bridge` |
| Stop it starting at boot | `sudo systemctl disable aprs-incident-rf-bridge` |

What the service has transmitted is remembered in `/var/lib/aprs-incident-rf-bridge/state.json`, so a restart or
reboot does not send everything again.

## Taking your objects off the air

Stopping the service does not kill the objects it put on the air; they fade from maps as clients
time them out. To take them off at once, stop the service and run the script one more time with a
limit of zero objects. It transmits a kill for each object it still carries and then exits:

```sh
sudo systemctl stop aprs-incident-rf-bridge
```

```sh
sudo python3 /opt/aprs-incident-rf-bridge/aprs_radio.py --config /etc/aprs-incident-rf-bridge/aprs-incident-rf-bridge.conf --state-file /var/lib/aprs-incident-rf-bridge/state.json --max-objects 0 --once --transmit
```

This command transmits. It uses your settings file for the callsign and TNC.

## How the service behaves

- **The feeder cannot be reached:** nothing is transmitted and nothing is killed. It tries again at
  the next poll.
- **The TNC cannot be reached:** it says so in the log and tries again at the next poll.
- **The script crashes or is killed:** systemd starts it again after 30 seconds.
- **The settings file has a mistake:** the service stops and stays stopped, because restarting cannot
  fix a wrong setting. The log names the file, the line and the problem. Fix it and start the service.
- **Direwolf on the same machine:** if Direwolf runs as a service called `direwolf`, this service
  starts after it.
- **Who it runs as:** an unprivileged user that systemd creates for it. It can read its settings,
  write its own state folder and use the network, and nothing else on the machine.

## Updating

Get the new version of the folder, then run the installer again:

```sh
sudo sh systemd/install.sh
```

Your settings are kept, and a running service is restarted on the new version.

## Removing it

```sh
sudo sh /opt/aprs-incident-rf-bridge/uninstall.sh
```

This stops and removes the service and `/opt/aprs-incident-rf-bridge`, and keeps your settings and the record of
what was sent. Add `--purge` to remove those as well. Take your objects off the air first (above) if
any are still showing.

## Troubleshooting

**The service fails straight away.** Read the reason:

```sh
journalctl -u aprs-incident-rf-bridge -n 20
```

- `use your own callsign, not N0CALL`: the settings file still has the example callsign.
- `there is no setting called ...`: a misspelt name; the line number is in the message.
- `... must be yes or no`: an on/off setting has some other value.

**"could not send to the KISS TNC".** The TNC is not answering at the address in the settings.
Check that Direwolf is running and its KISS port matches `kiss-tcp`. If the TNC is on another
machine, check it accepts connections from this one.

**A serial TNC gives "Permission denied".** The service needs to be in the group that owns the
port. The installer adds it to `dialout` (Debian, Ubuntu, Raspberry Pi OS, Fedora) or `uucp` (Arch)
when one of those exists. Check the port's group and what the service was given:

```sh
ls -l /dev/ttyUSB0
```

```sh
systemctl show aprs-incident-rf-bridge -p SupplementaryGroups
```

If the port belongs to a different group, create
`/etc/systemd/system/aprs-incident-rf-bridge.service.d/serial.conf` containing these two lines, with your group's
name, then run `sudo systemctl daemon-reload` and restart the service:

```ini
[Service]
SupplementaryGroups=yourgroup
```

**"could not get the packet list".** The machine cannot reach the feeder. Check its internet
connection and the `server` address. Nothing is transmitted or killed while this lasts.

**Nothing is listed.** There may be nothing in your radius at that importance level. Try a larger
`miles`, or `min = low` for a while, with `transmit = no`.

**Python is not at `/usr/bin/python3`.** The installer stops and says where it found it. Create a
drop-in that overrides `ExecStart` with the right path, or link it into `/usr/bin`.
