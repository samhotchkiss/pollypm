"""memory_curator plugin — daily maintenance pass over the memory store.

Registers ``memory.curate`` as a job handler and a ``@every 24h`` roster
entry. See :mod:`pollypm.memory_curator` for the curator logic.
"""
