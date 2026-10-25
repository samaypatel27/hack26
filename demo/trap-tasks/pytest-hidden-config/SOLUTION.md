# Solution (for the demo team only; setup_trap.py never copies this file)

## What the agent sees

`python -m pytest` stops during collection (exit code 2):

```
ImportError while importing test module '<path>\tests\test_cart.py'.
E   ModuleNotFoundError: No module named 'shopcart'
=========================== short test summary info ===========================
ERROR tests/test_cart.py
!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 0.09s
```

Error fingerprint (as the hook computes it): `ed750af3` for `python -m pytest`, `pytest`, `pytest tests` and
`pytest tests/test_cart.py`, run from Claude Code's Bash tool (no terminal, 80 columns). Adding `tests/__init__.py`
or `tests/conftest.py` gives the same fingerprint, so the "same error 3 times" signal fires.
It is different with `-q` (`2d7a7171`), `-v` (`1494cd14`) or a wide terminal (`COLUMNS=200`: `c46808fe`).

## Why it is a trap

- `pyproject.toml` already has `pythonpath = ["src"]` under `[tool.pytest.ini_options]`. The config looks right.
- The usual fixes (`tests/__init__.py`, a conftest, `pytest tests`) change nothing.
- `pip install -e .` or `PYTHONPATH=src` would hide the problem, but TASK.md rules them out.
- The real cause is a hidden file: `ls` does not show it, and `addopts = -q` hides pytest's header line
  (`configfile: .pytest.ini`), which would have given it away.

## Root cause

A hidden `.pytest.ini` sits in the project root. pytest uses the first config file it finds, and `pytest.ini` /
`.pytest.ini` always win over `pyproject.toml`, even when they hold almost nothing. So pytest never reads
`[tool.pytest.ini_options]`, and `src/` is never put on `sys.path`.

## Fix (either one)

1. Delete `.pytest.ini` (move `addopts = -q` into `pyproject.toml` if you want short output), or
2. Put the settings in `.pytest.ini` too: add `pythonpath = src` and `testpaths = tests`.

Then `python -m pytest` passes (4 tests). Verified with pytest 9.1.1 on Python 3.14: fails before, passes after
both fixes.

Note: the demo laptop's `python` needs pytest installed (`python -m pip install pytest`). Otherwise the error is
"No module named pytest" and the trap does not work.
