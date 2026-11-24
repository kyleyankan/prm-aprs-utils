APRS object feeds - Raspberry Pi install
========================================
Needs only Python 3.9+ (already on Raspberry Pi OS). No extra packages.

1. Copy and unpack
     scp aprs-pi.zip pi@raspberrypi.local:~
     ssh pi@raspberrypi.local
     unzip aprs-pi.zip && cd aprs-pi          (no unzip?  sudo apt install unzip)

2. Configure
     ./aprs_ctl.sh init
     nano aprs.env        callsign, Google key, PennDOT login, NWS contact, home lat,lon
                          (bucks, nj511 and web need no credentials)
                          SEND=0 prints only; SEND=1 transmits. Trim ENABLED to what you use.
                          ("send", the one program that talks to APRS-IS, always runs.)

3. Run
     ./aprs_ctl.sh start            start everything in the background
     ./aprs_ctl.sh status           what is running
     ./aprs_ctl.sh logs nws -f      follow one service's log
     ./aprs_ctl.sh stop [service]   stop everything, or one service
     Web map/editor: http://<pi address>:8080/   (no login - internal network only)

4. Start at boot (optional)
     sudo ./install-service.sh            (sudo ./install-service.sh remove  to undo)
   After that: sudo systemctl restart aprs-feeds   after editing aprs.env.

Sending to APRS-IS (one connection)
     Only aprs_send.py (service "send") logs in to APRS-IS. The feed scripts and the web editor
     hand it their packets every poll through data/outbox/. It sends an object when it is new,
     changed or killed, and re-sends an unchanged one after 26 minutes; everything else is dropped.
     ./aprs_ctl.sh logs send -f                        what was really sent, and why
     cd data && python3 ../aprs_send.py --callsign W3XYZ-9 --list
                                                       every object: last change, last send, next re-send
     The web map shows the same for each object: when it was last sent and when it is sent again.
     aprs.env: EXTRA_send="--resend-minutes 20" changes the 26 minutes.
     If "send" is stopped, nothing reaches APRS-IS; the other services say so in their logs.
     Upgrading: copy the new files, then ./aprs_ctl.sh restart. Everything is sent once at the
     first start, because the sender does not yet know what is on the air.

E-ink picture
     http://<pi address>:8080/eink.png      black-and-white map + object list, 272x792
     Options: ?w=&h=&miles=&lat=&lon=&rotate=90&list=0&invert=1&gray=1   (or /eink.bmp)

APRS symbols
     ./aprs_ctl.sh symbols      adds the symbol settings (SYM_FIRE, SYM_WX_TOR, ...) to aprs.env
     Edit them there, then ./aprs_ctl.sh restart. The web map and /map.png draw the real symbols.

Full-colour picture
     http://<pi address>:8080/map.png        800x480, same options as /eink.png, plus dark=1, zoom=2
     http://<pi address>:8080/map-all.png    the same with other people's objects included

Other people's APRS objects (receive only)
     http://<pi address>:8080/api/aprs            JSON; add ?stations=1 for stations
     http://<pi address>:8080/eink-all.png        the e-ink picture with them included
     Switch the layer on/off with the two checkboxes on the web map.
     aprs.env: APRS_LISTEN=0 turns the listener off, APRS_LISTEN_MILES=50 sets its radius.
     What was heard and all paths are saved in data/aprs_heard.json and restored after a restart.
     The latest APRS text messages are shown in a section of the web page and on /map.png
     (?messages=0 hides them, ?messages=8 shows more).
     Paths of moving objects/stations are drawn on the web map and pictures ("Paths" checkbox,
     ?paths=0 on the pictures); APRS_TRAIL_MINUTES=180 sets how much history is kept.

New Jersey (511NJ)
     aprs_nj511.py replaces aprs_njta.py (you can delete that file). In aprs.env:
       EXTRA_nj511="--county Camden,Burlington,Gloucester"     (blank = all of New Jersey)
     Types: --types incident,closure,delay (default); add roadwork, event, other if wanted.
     Only events whose written dates include today are sent.

Other tools
     python3 aprs_objects.py --callsign W3XYZ-9 --aprsfi-key KEY    find/kill leftover objects

Files: aprs.env = settings, data/ = object state and caches, logs/ = logs.
How it all fits together, and where each feed's data comes from: ARCHITECTURE.md
