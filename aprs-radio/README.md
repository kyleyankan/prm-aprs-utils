# aprs-radio: put the Incident Feeder's objects on the air

`aprs_radio.py` is for a licensed operator with a radio and a KISS TNC. It asks an Incident Feeder
for the objects near you, picks the ones worth sending, and transmits them through your TNC under
your callsign. It needs only Python 3.8 or later; there is nothing to install.

## From the feed source to your radio

```mermaid
flowchart TD
    subgraph feeder[At the feeder, a Raspberry Pi]
        src[1. Public source<br>county dispatch, 511 traffic, weather service]
        feed[2. Feed script<br>builds the object: name, position, text]
        sender[3. Sender<br>sends it to APRS-IS and records what it sent]
        api["4. Web service: /api/packets<br>lists sent objects near any point, ranked"]
        src --> feed --> sender --> api
    end
    subgraph station[At your station]
        ask[5. aprs_radio.py asks the feeder<br>every 2 minutes: your position and radius]
        keep[6. Keeps the few that matter<br>medium importance and up, ten at most]
        decide{"7. Does it need sending now?<br>new, changed, 30 minutes old, or a kill"}
        wait[Nothing sent for this object]
        tnc[8. KISS TNC<br>Direwolf, or a hardware TNC on a serial port]
        radio([9. Your radio, on 144.39 MHz<br>third-party packets under your callsign])
        ask --> keep --> decide
        decide -->|yes| tnc --> radio
        decide -->|no| wait
    end
    api -->|"the list, as JSON"| ask
```

Steps 1 to 4 happen at the feeder whether or not anyone is listening. Steps 5 to 9 run at your
station, and only when you start the script. Without `--transmit`, step 8 prints the packet
instead of sending it.

## Before you start

- **A licence that covers what you transmit.** Your station sends these packets, so you are its
  control operator.
- **A KISS TNC.** A software TNC such as Direwolf with its KISS TCP port on (the default, 8001), or
  a hardware TNC in KISS mode on a serial port.
- **The feeder's address.** K3YLE's feeder is at `https://aprs-incidents.phlm.sh`. Another feeder's operator will give you
  theirs.
- **Your position** as a latitude and longitude, and a radius in miles.

What the feeder's operator sees: each request carries your callsign, the position and radius you
ask for, and your internet address. The feeder keeps those for 7 days and shows them on its
operator's map.

## Try it without transmitting

Without `--transmit` the script only prints what it would send. Start here:

```sh
python3 aprs_radio.py --server https://aprs-incidents.phlm.sh --callsign W3XYZ-10 --lat 40.12 --lon -75.34 --once
```

Each line shows the reason (`new`, `changed`, `refresh`, `kill` or `dropped`) and the packet.

## Transmit

Add `--transmit` once the list looks right.

```sh
# Direwolf or another software TNC on this machine
python3 aprs_radio.py --server https://aprs-incidents.phlm.sh --callsign W3XYZ-10 --lat 40.12 --lon -75.34 --transmit

# a hardware TNC in KISS mode (Linux or macOS)
python3 aprs_radio.py --server https://aprs-incidents.phlm.sh --callsign W3XYZ-10 --lat 40.12 --lon -75.34 \
    --kiss-serial /dev/ttyUSB0 --baud 9600 --transmit
```

On Windows, use a software TNC and `--kiss-tcp`; the serial option needs Linux or macOS.

## What it sends, and how often

| Situation | What happens |
|---|---|
| An object is new, or its position, symbol or text changed | Transmitted |
| An object is unchanged | Transmitted again every 30 minutes (`--refresh`) |
| The feeder kills an object you transmitted | The kill is transmitted |
| An object you transmitted leaves the list | Your station kills it, so it does not linger on maps |
| The feeder cannot be reached | Nothing is transmitted and nothing is killed |
| The TNC cannot be reached | Reported, and tried again on the next poll |

The defaults are deliberately small, because a shared 1200 baud channel fills quickly:

| Option | Default | Meaning |
|---|---|---|
| `--miles` | 10 | radius around your position |
| `--min` | medium | least important level to carry: `low`, `medium`, `high` or `critical` |
| `--max-objects` | 10 | most objects kept on the air at once, most important first |
| `--max-per-poll` | 6 | most packets transmitted per poll |
| `--gap` | 8 | seconds between two transmissions (never less than 2) |
| `--poll` | 120 | seconds between polls of the feeder (never less than 60) |
| `--refresh` | 30 | minutes before an unchanged object is sent again (never less than 10) |
| `--path` | none | digipeater path, such as `WIDE2-1` |
| `--kiss-port` | 0 | which radio to use on a TNC with more than one |
| `--no-bulletins` | off | do not transmit bulletins |
| `--auto` | off | carry everything in the radius, slowly, most important first (see below) |
| `--burst` | 2 | with `--auto`: packets per poll, 1 to 3 |

**Auto mode (`--auto`).** The defaults carry a short list: medium and above, ten objects. With
`--auto` the script asks for every object in your radius, whatever its level, and works through
them a little at a time: at most `--burst` packets per poll (default 2, never more than 3). The
order each poll is:

1. kills, so nothing this station put on the air is left behind;
2. bulletins for critical objects;
3. objects, the most important level first. Within a level a new or changed object goes before
   one that is only due for its `--refresh` repeat, and the one sent longest ago goes first.

So a new building fire jumps ahead of the roadwork still waiting, and low-importance objects go
out only when nothing more important is waiting. At 2 packets every 2 minutes that is 60 packets
an hour. Within 25 miles of Norristown one evening there were about 60 objects: the handful rated
medium were repeated every 30 minutes and each low one got a turn roughly once an hour. On a busy
day some low ones wait longer or are never sent; that is the intent. `--min` and `--max-objects`
still limit the list if you give them, and `--max-per-poll` is not used in this mode.

```sh
python3 aprs_radio.py --server https://aprs-incidents.phlm.sh --callsign W3XYZ-10 --lat 40.12 --lon -75.34 --miles 15 --auto
```

**Bulletins.** When an object rated critical (a tornado warning, an entrapment, an explosion) is
inside your radius, the feeder also lists a general bulletin with its text, and this script
transmits it: `W3XYZ-10>APZIFD:}K3YLE-15>APRS,TCPIP,W3XYZ-10*::BLN1     :Tornado Warning til 9:15PM`.
That reaches radios that show messages but no map. A bulletin goes out when it is new or its
text changed and again every `--refresh` minutes, after any kills and before the objects, and it
counts towards `--max-per-poll`. It is sent whatever `--min` says. There is nothing to kill when
it ends; it is simply no longer sent. The feeder uses at most three at a time (BLN1 to BLN3).

On Direwolf, a `KISSPORT` line with no channel number serves every channel, and `--kiss-port` is
then the Direwolf channel to transmit on (`--kiss-port 0` for `CHANNEL 0`). A `KISSPORT` line that
names a channel always uses that channel.

With `--min low` the list fills with roadwork. Leave it at `medium` or higher unless your area is
quiet.

## How the packets look on the air

They go out in APRS third-party form, from your callsign:

```
W3XYZ-10>APZIFD:}K3YLE-15>APRS,TCPIP,W3XYZ-10*:;FIRE7K2QX*241740z4007.80N/07519.80WdBUILDING FIRE MAIN ST/OAK ST
```

- **Your callsign is the sender**, so the transmission is identified as yours.
- **The feeder stays the owner of the object.** If you sent the bare object instead, your station
  would take the object over, and any igate that heard it would put a second copy on APRS-IS.
  Igates do not pass third-party packets back.
- **`APZIFD`** is an experimental destination; `--tocall` changes it.

## Running it all the time

What was transmitted is kept in `aprs_radio_state.json` (`--state-file`), so stopping and starting
does not send everything again.

**As a service on Linux.** `systemd/install.sh` installs the script as a systemd service that starts
at boot, restarts after a crash and reads its settings from `/etc/aprs-radio/aprs-radio.conf`.
[INSTALL.md](INSTALL.md) walks through installing and configuring it:

```sh
sudo sh systemd/install.sh
```

**Settings in a file.** `--config FILE` reads the options from a file instead of the command line,
one `name = value` per line, named like the options without the dashes (`max-objects = 5`). On/off
options take `yes` or `no`. Anything also given on the command line wins. The service uses this;
`systemd/aprs-radio.conf.example` is a commented starting point.

If you stop it for good while objects are on the air, they are not killed; they fade from maps as
clients time them out. To take them off at once, run it one more time with the same settings and
state file plus `--max-objects 0 --once --transmit`: it sends a kill for each object it still
carries and then exits.

## Tests

```sh
cd tests && python3 -m unittest test_radio
```

The tests use a fake feeder and a fake TNC on this machine and never key a radio.
