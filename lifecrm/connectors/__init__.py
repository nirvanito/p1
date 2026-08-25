"""Connectors for the observed tier: pure readers, one per data source.

See lifecrm/connectors/base.py for the contract and docs/observed-tier.md for
the design. A connector never writes to the database — lifecrm/domain/
observations.py owns every write.
"""
