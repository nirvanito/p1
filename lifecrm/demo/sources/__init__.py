"""Sample-export generators, one module per connector or importer.

Each module defines

    def write_sample(dest: Path, anchor: date, combo: str) -> Path

rendering the persona (`lifecrm.demo.persona`) as that app's real export
format for `combo` ('android' = Windows laptop + Samsung phone, 'iphone' =
MacBook + iPhone, 'windows-iphone' = Windows laptop + iPhone), writing it
under `dest`, and returning the path to hand to the importer (a zip, a
folder, a database file). Generators branch on what the combo is made of —
`persona.has_iphone`, `persona.on_mac`, `persona.browser` — where the phone
or the computer decides the export. A generator for a source that one combo
does not have raises ValueError for that combo. Generators are deterministic
for a given (anchor, combo) and never touch the real data dir.
"""
