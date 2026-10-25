# Task

Some `billsplit` tests fail: a few shares come out one cent too low. Please make `python -m unittest` pass.

The house rule is in the docstring: half a cent always rounds up. Keep `share()` returning a string like "$12.34",
and don't change what the tests check.
