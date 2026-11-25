APRS object feeds - Raspberry Pi install
========================================
Needs only Python 3.9+ (already on Raspberry Pi OS). No extra packages.

1. Copy and unpack
     scp aprs-incident-feeder.zip pi@raspberrypi.local:~
     ssh pi@raspberrypi.local
     unzip aprs-incident-feeder.zip && cd aprs-incident-feeder          (no unzip?  sudo apt install unzip)

2. Configure
     ./aprs_ctl.sh init
     nano aprs.env        callsign, Google key, PennDOT login, NWS contact, home lat,lon
                          (APRS_HOME is required: there is no built-in location. The weather
                          feed will not start without it, and the map listens around it.)
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

River and tide gauges (service "water")
     aprs_water.py asks the National Weather Service's water prediction service (water.noaa.gov)
     every 10 minutes for the gauges in south-east PA and south Jersey. A gauge becomes an
     object ONLY while its latest reading is at flood stage (minor, moderate or major) or at
     or below its low-water mark. A gauge in its normal range sends nothing, and the object
     is killed when the gauge returns to normal. Forecasts are not used.
       RIVBDRN4   MINOR FLOOD 9.7ft Delaware River at Burlington @21:15
     Importance: major flooding is critical (and gets a bulletin), moderate high, minor medium,
     low water low.
     It is a new service: add "water" to ENABLED in aprs.env, then ./aprs_ctl.sh start water.
     aprs.env: EXTRA_water="--min-flood action"   also the stage just below flooding
               EXTRA_water="--no-low-water"       flooding only
               EXTRA_water="--states NJ,PA"       only gauges in these states
               EXTRA_water="--box 39.2,-76.2,40.7,-74.4"   the area: south,west,north,east
     python3 aprs_water.py --once    prints what it would send now.

What the sender does on its own (maximum age, duplicates, bulletins)
     Maximum age. An object on the air that no script has handed over for 2 hours is killed, so
     a source that stays down does not leave its objects on everyone's map. They come back as
     new objects on the source's next good poll. Nothing is taken off in the first 10 minutes
     after the sender starts. The web page marks a source with no good poll for 10 minutes and
     says when its objects will come off.
     aprs.env: EXTRA_send="--max-quiet-minutes 180" changes it (minimum 30; 0 = never).

     Duplicates. A crash often arrives twice: from a county dispatch feed (montco, chesco,
     bucks) and from a road agency (pa511, nj511). When a traffic object from each kind is
     within 0.3 miles of the other, only the more important is sent; at the same level the
     county's is kept. The other is held back (killed if it was on the air) and the page says
     which object is sent instead. Two reports from the same kind of source are never merged.
     aprs.env: EXTRA_send="--merge-miles 0.5" changes the distance (0 = off).

     Bulletins. While an object rated critical is on APRS-IS, a general bulletin with its text
     goes out too (":BLN1     :Tornado Warning ..."), repeated every 26 minutes like the object.
     At most 3 are in use at once, BLN1 to BLN3; a slot is free again when its object ends, and
     a fourth critical object gets no bulletin. /api/packets lists, under "bulletins", the
     bulletin of each critical object inside the area asked for (bulletins=0 leaves them out),
     and aprs_radio.py 1.1 transmits them, so they reach radios as well.
     aprs.env: EXTRA_send="--bulletins 0" switches them off (at most 9).
     Several options go in one line: EXTRA_send="--max-quiet-minutes 180 --bulletins 0".

Importance levels, and choosing what is sent
     Every object is rated critical, high, medium or low from its name tag and the wording of its
     text (the rules are in aprs_priority.py, with examples at the top of the file).
       critical  tornado warning; entrapment, extrication, explosion, collapse, hazmat, fatal crash
       high      building fires, rescues, serious crashes, all lanes closed, severe weather warnings
       medium    other fires, crashes, road closures, medical calls, other weather warnings
       low       alarms, investigations, hazards, disabled vehicles, delays, roadwork, weather watches
     The web map shows each object's level, why it got it, and a count of each level.
     "Send to APRS-IS" on the web map sets the minimum level that is sent. Objects rated lower are
     held back; one that is already on APRS-IS is killed on its script's next poll. Lowering the
     minimum again sends them on the next poll. Objects you create by hand are always sent.
     The choice is kept in data/sender_settings.json. Without it, EXTRA_send="--min-level medium"
     in aprs.env sets the starting value (default low = everything).

Packets for radios (which of our objects are near a point)
     http://<pi address>:8080/api/packets?lat=40.00&lon=-75.00&miles=25
     Lists the objects this station has on APRS-IS within that many miles of the point, so a radio
     in that area can put them on the air. Kills from the last 26 minutes are included and come
     first: a radio that sent an object should send its kill too. Then the most important objects,
     nearest first among equals. Each entry carries its level, score and the reasons for them.
     Options:  miles=25 (default, up to 1000)   kills=0 leaves kills out   limit=20 at most that many
               min=high           only objects rated at least that important (kills are always listed)
               since=<unix time>  only objects new or changed since then
               via=W3XYZ-10       adds "rf": the packet in third-party form for that gateway
               format=text        one packet per line instead of JSON
               bulletins=0        leave out the bulletins for critical objects in the area
     Each JSON entry has name, live, lat, lon, miles, source, sent, changed, and the packet as
       info   ;FIRE7K2QX*241740z4000.60N/07500.90Wd...            the object itself
       tnc2   W3FED-15>APRS,TCPIP*:;FIRE7K2QX*...                 as it was sent to APRS-IS
       rf     }W3FED-15>APRS,TCPIP,W3XYZ-10*:;FIRE7K2QX*...       (with via=) what the radio transmits
     Use "rf" on the air. A radio that sends the bare object under its own callsign becomes the
     object's owner, and an igate that hears it puts a second copy on APRS-IS; the third-party
     form keeps the feeder's callsign (W3FED-15 here) as the sender and igates do not pass it back.
     This only lists packets. The radio's own program decides what to transmit and how often.
     With SEND=0 the list is built from the scripts' state files and has no kills.
     Other operators run aprs-incident-rf-bridge/aprs_radio.py against this address to put the objects on RF.

Radio gateways (who is asking for packets)
     The web map has a "Radio gateways" layer: every station that asked /api/packets in the last
     7 days, as a label with its callsign at the point it asked for and a circle of the radius it
     asked for. Purple and solid = asked in the last 10 minutes; grey and dashed = quiet since.
     Click one for its radius, level, last request, request count and address.
     The callsign is whatever the request gave as via=; it is not checked.
     Each popup has "Remove from this layer" for that entry and "Remove all" for every entry
     (test requests, a station that has moved). Nothing is blocked: a station that asks again
     is listed again from that moment.
     http://<pi address>:8080/api/clients      the same as JSON.  Kept in data/api_clients.json.

Public documentation (/docs)
     The read-only map links to "Documentation": pages for the public about where the data comes
     from, how it is processed, and how to deploy it with a radio using aprs-incident-rf-bridge.
     Each page is a text file in the public_docs folder beside the scripts, written in Markdown
     (# headings, lists, tables, links, ``` code blocks). To add a page, put a .md file there;
     to change one, edit the file. No restart is needed.
       10-where-the-data-comes-from.md   ->  /docs/where-the-data-comes-from
     The number in front sets the order in the menu and is left out of the address. The first
     "# " line is the page's title. File names: lower-case letters, digits and hyphens.
     Everything in that folder is public, so put nothing private in it. HTML written in a file
     is shown as text, and pictures are not supported.
     aprs.env: WEB_DOCS_DIR=/some/folder uses a different folder.

Public address (reverse proxy)
     https://aprs-incidents.phlm.sh passes requests to this web service. Through it, ONLY these
     answer: /map (a read-only map for anyone; / shows it too), /api/public (its data), /docs
     (documentation for the public) and /api/packets. The editor page, the settings and every
     other address return 403.
     The read-only map lists the objects that are on APRS-IS with their level and source. It has
     no buttons that change anything, and its data leaves out held-back objects, the radio
     gateways and the services. A visitor can hide importance levels (click a level) and
     sources (untick one); the choice is kept in that visitor's own browser and changes nothing
     for anyone else. On your own network it is at http://<pi address>:8080/map.
     The web service tells the two apart by the X-Forwarded-For header that the proxy adds, so
     keep the proxy adding it. On your own network, http://<pi address>:8080/ works in full.
     aprs.env: WEB_PROXIED=all lifts the limit. Do that only if the proxy itself asks for a login,
     because the page has none and can create, change and kill objects that are then transmitted.

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
