"""Moadian infrastructure adapters (spec §12.3, US-404).

Only the deterministic fixture simulator lives here in this phase.
There is deliberately no production Moadian client: production
connectivity is not built until a later phase, and no module in this
package may import an HTTP client.
"""
