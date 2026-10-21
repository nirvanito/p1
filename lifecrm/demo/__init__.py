"""Demo instance: a synthetic life, rendered as the real exports each phone and
computer produces, and run through the real pipeline into a throwaway root.

`persona.py` is the single story every generator renders, so the texts, the
calendar, the photos, the health export and the journal all agree with each
other. `sources/<connector>.py` turns that story into one app's export format
(a Health Connect zip, an SMS Backup & Restore XML, a Mac chat.db, ...).
Nothing here ever touches the real data directory.
"""
