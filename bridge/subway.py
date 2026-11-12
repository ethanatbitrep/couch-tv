"""NYC subway arrivals for the TV home: the next trains at the stations in config.json "subway".

    "subway": {
      "stations": [
        {"name": "W 4 St", "stops": ["A32", "D20"]},
        {"name": "Christopher St", "stops": ["133"]}
      ],
      "count": 3
    }

`stops` are GTFS stop ids without the direction letter (stops.txt in the MTA's static GTFS):
A32 is W 4 St-Wash Sq on the 8 Av line (A C E), D20 the same station on the 6 Av line (B D F M),
133 Christopher St on the 7 Av line (1). Optional `lines` on a station keeps only those lines.
`count` is how many trains each direction shows (1 to 6, default 3).

The MTA's realtime feeds (GTFS-realtime protobuf) are public and need no key. Each feed carries a
group of lines; the bridge reads the ones the stations need, at most every 20 seconds, whoever
asks. Uptown is the N platform, downtown the S one. The protobuf is read here with a small
decoder for the few fields used (trip route, stop id, arrival time), so nothing new to install.
"""
import asyncio
import logging
import time

import config

log = logging.getLogger("subway")

FEED = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2F{}"
# which feed carries a line (the MTA groups them by division)
LINE_FEED = {**{l: "gtfs" for l in "1234567"}, "GS": "gtfs", "6X": "gtfs", "7X": "gtfs",
             **{l: "gtfs-ace" for l in ("A", "C", "E", "H", "FS")},
             **{l: "gtfs-bdfm" for l in ("B", "D", "F", "M", "FX")},
             **{l: "gtfs-nqrw" for l in "NQRW"}, "G": "gtfs-g", "J": "gtfs-jz", "Z": "gtfs-jz",
             "L": "gtfs-l", "SI": "gtfs-si"}
# which feeds stop at a stop id, by its line letter (A.. on 8 Av, D.. on 6 Av, 1.. on 7 Av ...)
STOP_FEEDS = {"1": ["gtfs"], "2": ["gtfs"], "3": ["gtfs"], "4": ["gtfs"], "5": ["gtfs"], "6": ["gtfs"],
              "7": ["gtfs"], "9": ["gtfs"], "A": ["gtfs-ace"], "H": ["gtfs-ace"], "S": ["gtfs-ace", "gtfs-si"],
              "B": ["gtfs-bdfm"], "D": ["gtfs-bdfm"], "F": ["gtfs-bdfm"], "M": ["gtfs-bdfm"],
              "N": ["gtfs-nqrw"], "Q": ["gtfs-nqrw"], "R": ["gtfs-nqrw"], "G": ["gtfs-g"],
              "J": ["gtfs-jz"], "L": ["gtfs-l"]}
SHOW = {"6X": "6", "7X": "7", "FX": "F", "GS": "S", "FS": "S", "H": "S"}   # express variants keep their line's bullet
EVERY = 20      # seconds between reads of a feed
GONE = 0        # a train is dropped once its departure time has passed

_cfg = config.CONFIG.get("subway") or {}
STATIONS = [s for s in (_cfg.get("stations") or []) if isinstance(s, dict) and s.get("stops")]
COUNT = max(1, min(6, int(_cfg.get("count") or 3)))
ENABLED = bool(STATIONS)

_cache = {}   # feed -> (read at, [(route, stop_id, arrives, leaves), ...])
_lock = asyncio.Lock()


# ---------- a small protobuf reader (wire format only, the fields named below) ----------
def _varint(b, i):
    n = shift = 0
    while True:
        c = b[i]
        i += 1
        n |= (c & 0x7F) << shift
        if c < 0x80:
            return n, i
        shift += 7


def _fields(b):
    """Yields (field number, wire type, value): ints for varints, bytes for length-delimited."""
    i, end = 0, len(b)
    while i < end:
        key, i = _varint(b, i)
        num, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 2:
            ln, i = _varint(b, i)
            v, i = b[i:i + ln], i + ln
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError(f"protobuf wire type {wt}")
        yield num, wt, v


def _time_of(event):   # StopTimeEvent: time = 2
    for n, wt, v in _fields(event):
        if n == 2 and wt == 0:
            return v
    return None


def parse_feed(data):
    """FeedMessage bytes -> [(route_id, stop_id, arrives, leaves)] for every stop of every trip
    (unix times; either one stands in for the other when the feed gives only one).
    FeedMessage.entity = 2; FeedEntity.trip_update = 3; TripUpdate.trip = 1, stop_time_update = 2;
    TripDescriptor.route_id = 5; StopTimeUpdate.arrival = 2, departure = 3, stop_id = 4."""
    out = []
    for n, wt, ent in _fields(data):
        if n != 2 or wt != 2:
            continue
        for n2, wt2, tu in _fields(ent):
            if n2 != 3 or wt2 != 2:
                continue
            route, stops = None, []
            for n3, wt3, v in _fields(tu):
                if n3 == 1 and wt3 == 2:
                    for n4, wt4, v4 in _fields(v):
                        if n4 == 5 and wt4 == 2:
                            route = v4.decode(errors="replace")
                elif n3 == 2 and wt3 == 2:
                    stop, arr, dep = None, None, None
                    for n4, wt4, v4 in _fields(v):
                        if n4 == 4 and wt4 == 2:
                            stop = v4.decode(errors="replace")
                        elif n4 == 2 and wt4 == 2:
                            arr = _time_of(v4)
                        elif n4 == 3 and wt4 == 2:
                            dep = _time_of(v4)
                    if stop and (arr or dep):
                        stops.append((stop, arr or dep, dep or arr))
            if route:
                out.extend((route, s, a, d) for s, a, d in stops)
    return out


# ---------- reading the feeds ----------
def _feeds_for(station):
    lines = station.get("lines")
    if lines:
        return sorted({LINE_FEED[l] for l in lines if l in LINE_FEED})
    return sorted({f for s in station["stops"] for f in STOP_FEEDS.get(str(s)[:1].upper(), [])})


async def _read(feed, session):
    got = _cache.get(feed)
    if got and time.time() - got[0] < EVERY:
        return got[1]
    try:
        async with session.get(FEED.format(feed)) as r:
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
            rows = parse_feed(await r.read())
        _cache[feed] = (time.time(), rows)
        return rows
    except Exception as e:   # keep the last good read for a few minutes rather than show nothing
        log.warning("subway feed %s: %s", feed, e)
        if got and time.time() - got[0] < 300:
            return got[1]
        return None


async def arrivals():
    """{"stations": [{"name", "uptown": [{"line", "min"}...], "downtown": [...]}], "at": unix}
    or None when no feed could be read."""
    import aiohttp
    feeds = sorted({f for s in STATIONS for f in _feeds_for(s)})
    async with _lock:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            rows = dict(zip(feeds, await asyncio.gather(*(_read(f, session) for f in feeds))))
    if not any(v is not None for v in rows.values()):
        return None
    now = time.time()
    out = []
    for st in STATIONS:
        stops = {str(s).upper() for s in st["stops"]}
        keep = {str(l).upper() for l in st.get("lines") or []}
        dirs = {"N": [], "S": []}
        for f in _feeds_for(st):
            for route, stop, t, leaves in rows.get(f) or []:
                base, d = stop[:-1].upper(), stop[-1:].upper()
                if base not in stops or d not in dirs or leaves < now - GONE:
                    continue
                line = SHOW.get(route, route)
                if keep and line not in keep and route not in keep:
                    continue
                dirs[d].append((t, line))

        def nexts(items):
            items.sort()
            return [{"line": line, "min": max(0, int((t - now) // 60))} for t, line in items[:COUNT]]
        out.append({"name": str(st.get("name") or "")[:40], "uptown": nexts(dirs["N"]), "downtown": nexts(dirs["S"])})
    return {"stations": out, "at": int(now)}
