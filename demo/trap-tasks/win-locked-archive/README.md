# dbarchive

Nightly job for the POS terminals: exports the day's sales to CSV, computes the totals for the summary e-mail,
then moves the terminal's SQLite database into `archive/<date>/`.

Run the tests from this folder:

```
python -m unittest
```
