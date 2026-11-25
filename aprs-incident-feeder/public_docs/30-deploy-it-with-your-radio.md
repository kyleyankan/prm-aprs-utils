# Deploy it with your radio

The objects on this map go to the internet side of APRS. They reach radios in the field only
where a licensed operator transmits them. If you have a radio and a TNC, you can be that station
for your area with a small program called `aprs-incident-rf-bridge`.

## What it does

`aprs-incident-rf-bridge` asks this feeder for the objects within a radius of your position, keeps the few
that matter most, and transmits them through your TNC under your callsign. It sends an object
when it is new or changed, repeats it every 30 minutes, and sends the kill when it ends.

The defaults are deliberately small: medium importance and up, ten objects at most, six packets
per poll, eight seconds apart. A shared 1200 baud channel fills quickly.

## What you need

- **An amateur radio licence that covers what you transmit.** Your station sends these packets
  under your callsign, so you are its control operator.
- **A radio on the APRS frequency** for your region (144.390 MHz in North America).
- **A KISS TNC:** a software TNC such as Direwolf, or a hardware TNC in KISS mode.
- **A computer with Python 3.8 or later** beside the radio. A Raspberry Pi is plenty. Nothing else
  needs installing.

## Get the program

It is one file, in the `aprs-incident-rf-bridge` folder of the project's repository:

[github.com/kyleyankan/prm-aprs-utils](https://github.com/kyleyankan/prm-aprs-utils/tree/main/aprs-incident-rf-bridge)

The folder's README explains every option, and its INSTALL guide shows how to run it as a service
that starts when the computer boots.

## Try it without transmitting

Run it once with your own callsign and position. Without `--transmit` it only prints what it
would send:

```
python3 aprs_radio.py --callsign W3XYZ-10 --lat 40.12 --lon -75.34 --once
```

Each line shows why a packet would be sent (`new`, `changed`, `refresh` or `kill`) and the packet.
If the list is longer than you want on the air, use a smaller `--miles` or a higher `--min`.

## Transmit

When the list looks right, add `--transmit`:

```
python3 aprs_radio.py --callsign W3XYZ-10 --lat 40.12 --lon -75.34 --transmit
```

That uses Direwolf on the same computer. For a hardware TNC on a serial port, add
`--kiss-serial /dev/ttyUSB0 --baud 9600`.

## Good practice on a shared channel

- **Start small.** Keep the default limits until you have listened to how busy your channel is.
- **Coordinate.** If another station near you already carries these objects, two of you sending
  the same thing only adds load. Ask on your local net.
- **Leave the path empty** unless you have a reason. These objects matter locally, and a wide
  path spreads them over digipeaters far from the incident.
- **Take your objects off when you stop for good.** The README shows the one command that sends a
  kill for everything your station still carries.

## What the feeder's operator sees

Each request your station makes carries your callsign, the position and radius you ask for, and
your internet address. The feeder keeps those for 7 days, and its operator can see which stations
are carrying its objects and for what area. They are not shown on the public map.

## How the packets look on the air

They go out in APRS third-party form, from your callsign:

```
W3XYZ-10>APZIFD:}K3YLE-15>APRS,TCPIP,W3XYZ-10*:;FIRE7K2QX*241740z4007.80N/07519.80WdBUILDING FIRE MAIN ST/OAK ST
```

Your callsign is the sender, so the transmission is identified as yours. The feeder stays the
owner of the object, which keeps internet gateways that hear you from sending a second copy back
to the APRS network.
