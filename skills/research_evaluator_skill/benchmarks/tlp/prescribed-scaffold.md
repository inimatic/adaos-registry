# C4 prescribed TLP scaffold

This arm intentionally over-specifies the engineering decomposition so that it
can be compared with the less prescriptive typed C3 handoff. It is a scaffold,
not source code and not a copy of any prior TLP implementation.

Create a direction skill with these modules:

- `conditions.py`: immutable revisions, validation, lock, and protocol digest;
- `operator.py`: centered per-channel `TropicalMaxPool2d` and MaxPool adapter;
- `paired_runner.py`: paired initialization/RNG allocation and CPU execution;
- `tracker.py`: tracker-provider boundary plus content-addressed evidence refs;
- `analysis.py`: paired differences and paired-seed bootstrap interval;
- `handlers/main.py`: thin AdaOS tools for conditions, execution, status, and evidence;
- `tests/`: unit, contract, failure, and CPU smoke tests.

Expose tools equivalent to `get_conditions`, `save_conditions`,
`lock_conditions`, `start_run`, `get_run`, `cancel_run`, and `get_evidence`.
The exact internal names may differ only when the same observable contracts are
maintained. Store primary research data in the direction skill's owner-scoped
runtime data binding. Do not add a private database server or direction-specific
scenario.
