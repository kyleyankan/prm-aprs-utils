# Where the data comes from

Everything on the map starts as a public report from a county, a state agency or the National
Weather Service. A program reads each source every couple of minutes, turns each report into an
APRS object, and sends it to the APRS network under one callsign.

Nothing here is entered by hand as a matter of routine, and nothing comes from private or
restricted systems.

> This is a convenience display for radio amateurs. It can be minutes late, a position can be
> approximate, and a source can stop answering. Do not use it in place of official alerts or 911.

## The sources

| Source | What it reports | Who publishes it |
|---|---|---|
| Montgomery County, PA | Fire and traffic dispatches | The county's live incident feed |
| Chester County, PA | Fire and traffic dispatches | The Department of Emergency Services' live incident page |
| Bucks County, PA | Fire and traffic dispatches | The county's public 911 Incident Report |
| Pennsylvania roads | Crashes, closures, roadwork | PennDOT, the data behind 511PA |
| New Jersey roads | Crashes, closures, roadwork, events | 511NJ |
| Weather | Warnings, watches and tracked storms | The National Weather Service and the National Hurricane Center |
| Rivers and tides | Gauges at flood stage or low water | The National Weather Service's water prediction service |

The map's side panel lists each source with a count, and marks one that has stopped answering.

## What each one gives us

### County dispatch

The three counties publish what their dispatch centres have sent units to: the type of call, the
cross streets or block, the municipality and the time. By default only fire and traffic calls are
carried; medical calls are left out.

Montgomery and Chester counties give a street location with no coordinates, so the location is
looked up to place it on the map. When only the street or the municipality can be found, the
position is approximate and the object's text ends in `?`. Bucks County supplies a map position.

An incident is taken off when it leaves the county's list, or 6 hours after it was dispatched.

### Road agencies

PennDOT and 511NJ report crashes, closures, roadwork and planned events, each with a position.
A Pennsylvania event is taken off as soon as its road reopens. New Jersey events that are
scheduled for other days are left out until the day they apply.

Traffic and roadway information for Pennsylvania is provided by PennDOT.

### Weather

National Weather Service alerts within about 200 miles are carried when they are rated severe or
worse, along with tropical storms from the National Hurricane Center.

- A warning that tracks a storm (severe thunderstorm, tornado, marine, snow squall) sits on the
  storm and is moved along its track until the next update.
- A warning with an outline carries that outline, so APRS maps that support it draw the warned area.
- A watch or a county-wide product is placed once, near the middle of the affected area.

An alert is taken off when it ends or is cancelled.

### Rivers and tides

About 140 river and tide gauges in the area are checked every 10 minutes. A gauge appears only
while its latest reading is at flood stage or at its low-water mark, and disappears when it
returns to normal or stops reporting.

## How we treat the sources

- Each source is asked no more often than it updates, and never faster than once a minute.
- Where a site says automated visitors are not welcome, the program does not visit it.
- If a source cannot be reached or its answer is not understood, nothing changes: no objects are
  added and none are removed until it answers again. After two hours of silence a source's
  objects are taken off.
