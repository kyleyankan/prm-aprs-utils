# How the data is processed

Between a source's report and an object on your map, four things happen: the report is turned
into an APRS object, it is rated for importance, duplicates are held back, and it is sent only as
often as it needs to be.

## From a report to an object

1. **Filter.** Only the wanted kinds of report are kept: fire and traffic calls, road events that
   apply today, weather alerts that are severe enough and close enough.
2. **Place.** Each report gets a latitude and longitude. Some sources supply one; others give a
   street location that has to be looked up. A report that cannot be placed is left out.
3. **Name.** Each object gets a name of up to 9 characters that stays the same for as long as the
   incident lasts. The first letters say what it is:

| Name starts with | Meaning |
|---|---|
| `FIRE` | A fire dispatch |
| `TRAF` | A crash or other traffic incident |
| `WORK` | Roadwork |
| `EVNT` | A planned event affecting traffic |
| `TOR`, `SVR`, `FFW`, `WND` and similar | A weather alert, followed by the forecast office |
| `RIV` | A river or tide gauge |

4. **Describe.** The text is the type of call and the location, shortened to fit what APRS
   radios can show. A text ending in `?` means the position is approximate.
5. **Remove.** When an incident ends, the object is not just dropped. A "kill" is sent, which tells
   every APRS map to take it off.

## Importance levels

Every object is rated critical, high, medium or low. The level depends only on what the object is
and how its text describes it, so it stays the same for the life of the object.

| Level | Examples |
|---|---|
| Critical | Tornado warning; entrapment, explosion, building collapse, hazardous materials, fatal crash; major flooding at a gauge |
| High | Building fires, rescues, crashes with serious injury or a closed road; severe thunderstorm, flash flood, marine, snow squall and tropical warnings; moderate flooding |
| Medium | Other fires and crashes, road closures, other weather warnings, watches for the severe kinds; minor flooding |
| Low | Alarms, investigations, controlled burns, hazards, disabled vehicles, delays, roadwork, events, advisories and statements; a gauge near flood stage or at low water |

The rating reads the wording the source used. It is only as good as that wording, so now and then
something is rated higher or lower than you would rate it yourself.

On the map you can hide levels you do not want to see: click a level at the top of the side panel.
Your choice is kept in your own browser and changes nothing for anyone else.

## Duplicates

A crash on a state road is often reported twice, once by the county's dispatchers and once by the
road agency. When two reports describe the same crash, the more important one is sent and the
other is held back.

## How often things are sent

One program sends everything, through a single connection to the APRS network. An object is sent:

- when it is new;
- when its position or text changes;
- when it ends (the kill);
- and again about every 26 minutes while it is unchanged, so stations that have just come on the
  air still receive it.

When an object rated critical is active, a general bulletin with its text is sent as well. That
reaches radios that show messages but have no map.

The operator can set a minimum level for sending. Objects below it are held back and do not
appear on this map.

## Where to see the objects

- **This site's map** shows what is being sent right now.
- **Any APRS map**, such as aprs.fi, shows the same objects alongside everything else on APRS.
- **An APRS radio or program** connected to the internet side of APRS receives them directly.
- **Over the air**, they are heard only where a volunteer station transmits them. See
  [Deploy it with your radio](/docs/deploy-it-with-your-radio).
