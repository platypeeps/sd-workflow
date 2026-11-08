# Home

Home is at `/home` (sd:2117): the design's critical Home Assistant tiles and
its wall display (`?kiosk=1`). `GET /api/home` (`home_screen.document`) lists
the tiles one text names, one `headline|<entity_id>|<name>` or
`tile|<group>|<entity_id>|<name>` per line. An optional last field, `alarm`,
`lock`, `toggle` or `sensor`, sets how the tile reads its state when its domain
would not; its commands follow the domain. Entity ids describe a house, so the checkout ships only
`home-tiles.conf.example`. A line the page cannot read is named and skipped;
with no list the page says "No tile list" and draws no grid.

The text is the workflow database's setting row `dashboard.home_tiles`
(sd:3987). The row wins: once it exists, `<config>/project-dashboard/home-tiles.conf`
is not read; with no row the file answers. A failed row read says "tile list
not read" with its reason, reads no file in its place and calls Home
Assistant for nothing. Edit tiles opens a text editor on the row, else the
file, else the example. Save posts `POST /api/home/tiles {text, before}`:
it parses first and refuses a text with problems (400, each with its line
number), and refuses a `before` that is not the digest of the text the page
showed (409, nothing written). A save toasts with Undo, which posts the
previous text. The first Save is the import; the lead deletes the file after
one nightly cycle. Each read makes one `GET /api/states` call to Home Assistant (sd:3427),
bounded by `home_screen.TIMEOUT`, with the URL and token local-ha-mcp uses:
`HA_MCP_URL` and `HA_TOKEN` from the environment, else `<config>/ha-mcp/.env`.
The LaunchAgent does not carry `HA_TOKEN`, so the server reads that file. Each
tile shows its state in the design's grammar and its age from `last_changed`.
A failed read (not configured, down, token refused, timed out, redirected, an
answer it cannot parse) names its error on every tile, and an entity Home
Assistant does not list names its own; the page still draws. The token never
reaches the page, and no redirect is followed. Under launchd the server's
interpreter may need macOS Local Network permission to reach a LAN host, as
local-ha-mcp's Node does.

The first tile, Home Assistant's Web UI (sd:6163), links to `/home/assistant`
on the dashboard in a new tab. With a session, that path answers 302 to the
REST base above, the scheme and host of `HA_MCP_URL`; without the settings it
answers 503 with the reason, and the tile shows "Cannot open" with it. The
document carries `web` (`{path, available, reason}`), never the host.

A tile's Act section and row menu offer the Home Assistant services its domain
allows (sd:4966), from `home_screen.SERVICES`, which the document carries as
`writes.services`: light, switch, fan and input_boolean turn on, off or toggle
at once with Undo; scene and script turn on and button presses, once, with no
Undo; lock (lock, unlock), alarm_control_panel (arm home, away or night,
disarm) and cover (open, close) change physical security, so each asks for the
entity id typed out and has no Undo. Each posts `POST /api/home/service
{entity_id, service, before[, confirm]}`, which refuses (400, nothing sent) an
entity the tile list does not name, a service the allowlist does not name for
that domain, and a security service whose `confirm` is not the entity id; a
`before` that is not the tile list's digest is a 409. It then calls
`POST /api/services/<domain>/<service>` with the read's URL and token, bounded
by `home_screen.SERVICE_TIMEOUT`, writes one record to the command journal
(Activity's "Command journal", on the Home page's item), and reads the tile's
state again, which the tile shows at once. Home Assistant's own reason for a
refusal (a token without service rights is a 401 or 403) is the toast; no
answer is an unknown outcome, and the page reads the tiles again. Writes are
off, with the read's reason, until Home Assistant was read. Any other domain
gets Read state, copy only: its `curl` line names `$HA_TOKEN` and `$HA_URL`
and never holds a value. The page reads `/api/home` through the
shell's reader (`read.js`) when it opens, on Read again, and each minute while
it is visible (sd:3522); the minute read draws no loading state, so the tiles
keep their place, and a failed read draws no tile and says why.

A binary sensor reads by its `device_class` (sd:4959): on is ok for
connectivity, power, plug, running, battery charging and presence, and off is
neutral except connectivity, which reads caution; on is a warning
for problem, smoke, gas, carbon monoxide, moisture, safety, tamper, heat and
cold; and a neutral fact, with no colour, for doors, windows, openings, motion
and occupancy. A sensor with no class is neutral on/off. Details opens with one
line on what the state means, from the same device class and domain tables, and
names the entity, its last change and its source. The repo holds no text per
entity.

A script or automation tile reads as its last run (sd:5727): Home Assistant's
`last_triggered`, in local time with its zone, or "last run not read" when the
answer holds none. Good night is such a tile; its entity id is in the tile list.

Views (`places.js`, `places.py`): Overview is the page above; Tasks, Briefs and
Reports each read their own document, `GET /api/home/tasks`, `/briefs` and
`/reports`, only when shown, and the address keeps the view as `#view=<id>`.
Tasks are the open items with no repo: personal and home items have none; and any item tagged home with `sd task edit N
--area home` (pack sd:8010), whatever its repo, which leaves Personal and its realm tab. Tasks under Realm Home holds
the same rows. A report is in no Tasks realm. A brief is home's when its series, lower-cased with spaces as hyphens,
names a job of the `home` family in `dashboard.report_families`; a report
is home's when its job is in that family. A missing checkout, family or file
makes the view unknown with the reason and the line to add, never an empty list.
The views are `window.PAGE_VIEWS`, set from the markup before `shell.js`; the shell's view switch draws
them in `#viewswitch`, and until that shell lands `places.js` draws a segmented control there (`viewSwitch`).

Open | All (sd:5752): Tasks, Briefs and Reports show their open rows; All
adds the week's done tasks (`places.task_rows`, the Tasks page's bound), the
seen briefs or the acknowledged reports, and the address keeps it as
`?tasks=all`, `?briefs=all` or `?reports=all`. Each row's act is its
acknowledge: a task's Done (or Done → next occurrence for a repeating one), a
brief's Seen, a report's Acknowledge, each with Undo. Seen is Today's mark,
the snooze `today:brief:<stem>` held until the brief's text changes, so a
brief seen here or on Today is seen in both; Not seen clears it.

Map (pack sd:8053, `home_map.py`, `home-map.js`) is the lot's assets over Arapahoe County's 2024 aerial, one
SVG as HOA's map. The address is the setting row `dashboard.home_map` (Settings), one line; the pack holds none.
The county's address locator places it in EPSG:2232 (US feet), and the map is the 400 ft square around it. The
aerial's ImageServer answers one `exportImage` for that square at full resolution (0.217 ft per pixel), cached
with the geocode under `<state>/system/home-map/`, so the county is asked once per address;
`GET /api/home/map/aerial` serves it. A failed read names its reason, and the assets still draw on a plain ground.

Assets are rows of the workflow database's `home_asset` table (migration 49, `sd_db.assets`): a kind, a name, a
point or a line in the same feet, a fields object and a photo address. Add: press Add, pick a kind, click the map
(a line kind takes a click per point, then Finish line), fill the short form (a plant or tree offers species,
planted, sun, water, zone and notes). Drag a point to move it. A row or a mark opens its form: Save, Delete, and
Add care task, which files a recurring task (area home) naming the asset in `fields.asset`; the next occurrence
keeps it. A plant or tree with an open care task due before today carries the warning ring.

Koi (pack sd:8054, `home_koi.py`, `home-koi.js`) shares the Map's assets: the pond is the `pond` asset, placed on
the Map (`fields.volume_gal` sizes a change in gallons), and each fish a `koi` asset with no place: name, variety,
photo, acquired, health notes and a log of size and weight. Water tests (ammonia, nitrite, nitrate, pH, KH,
temperature) are logged on the pond through `POST /api/home/koi/test`, which appends the reading to the
pond's current row. A pond saved from a form keeps the `water_log` and `tests` the server wrote. Feeding, filter cleaning, water tests and treatments are recurring care
tasks on the pond, filed as the Map files them. Water changes come from Home Assistant: the setting row
`dashboard.koi_water` names each script that changes the water, one `<entity_id>|<percent>` line, and each run
in Home Assistant's history (it keeps ten days) is one change of that percent. When the history holds a change
the pond's log lacks, the view posts `POST /api/home/koi/water`, which writes it once. The view shows days since
the last change, the percent replaced in the last 7 days and the weekly average over 28.
