"""
_supervisor_state_ref.py — Lightweight bridge for shutdown signal.

V69/S1: The headless_executor runs Gemini subprocesses but has no
reference to the supervisor's shared state object.  main.py sets
`stop_requested = True` on this module when safe stop is triggered,
and the executor checks it before spawning Plan Mode subprocesses.

This avoids threading events or passing state objects through the
entire executor call chain.
"""

# Set by main.py when state.stop_requested becomes True.
# Checked by headless_executor before Plan Mode Step 1.
stop_requested: bool = False
