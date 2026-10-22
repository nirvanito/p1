"""The local web UI: `lifecrm serve`.

A read-only front end over a snapshot of the database — ask with citations,
the day timeline, people, open loops, sources and health — for a laptop
browser or a phone on the same Wi-Fi. `server.py` owns HTTP and the snapshot,
`api.py` the reads (plain dicts, one per page), `index.html` the single page,
`icons.py` the home-screen icons. Capture stays where it is: voice notes and
/log. Nothing here writes the database except, optionally, the question log
that `lifecrm ask` also keeps.
"""
