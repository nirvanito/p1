"""Sample-export generators, one module per connector or importer.

Each module defines

    def write_sample(dest: Path, anchor: date, combo: str) -> Path

rendering the persona (`lifecrm.demo.persona`) as that app's real export
format for `combo` ('android' = Windows laptop + Samsung phone, 'iphone' =
MacBook + iPhone), writing it under `dest`, and returning the path to hand
to the importer (a zip, a folder, a database file). A generator for a source
that one combo does not have raises ValueError for that combo. Generators are
deterministic for a given (anchor, combo) and never touch the real data dir.
"""
