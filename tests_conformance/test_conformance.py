"""ADR-004 §4 conformance opt-in for the claude-code adapter.

Inherits the SDK-owned conformance battery. pytest collects every ``test_*``
method ``AdapterConformance`` defines against this template's own ``Adapter``,
proving the claude-code adapter honours the socket: identity + lifecycle present,
the MCP seam renders -> reads -> present-probes in lockstep on its OWN native
config (byte-stable + idempotent + additive), enumerate returns the load-bearing
tri-state including the required management tool (spawn stubbed), and an unmapped
runtime fails closed.
"""

from molecule_plugin.adapter_conformance import AdapterConformance

from adapter import Adapter


class TestClaudeCodeAdapterConformance(AdapterConformance):
    adapter_class = Adapter
