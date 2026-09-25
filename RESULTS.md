# AI PR reviewers: same catch rate, twice the noise

**35 of the top-recall bot's 66 flagged issues were not bugs.**

I seeded 24 bugs into a verbatim copy of `psf/requests`, opened 34 pull requests (24 with one bug each, 8 clean, 2
controls), committed the answer key's hash to `main` before the first PR, and asked six reviewers to review every PR.
Every inline finding was then judged blind by two independent models (a finding counts only if both call it a genuine
bug). Recall = sealed bugs matched by a genuine finding on the right file within two lines. Wilson 95% intervals.

| reviewer | inline findings | genuine | recall (of 24) | precision | findings on the 8 bug-free PRs | comments per PR |
|---|---|---|---|---|---|---|
| Claude Code CLI | 26 | 25 | 23 (0.96, 0.80-0.99) | 0.96 (0.81-0.99) | 0 | 0.77 |
| Codex CLI (`codex review`) | 26 | 25 | 23 (0.96, 0.80-0.99) | 0.96 (0.81-0.99) | 0 | 0.77 |
| Greptile | 66 | 31 | 23 (0.96, 0.80-0.99) | 0.47 (0.35-0.59) | 11 | 1.94 |
| Qodo Merge | 33 | 26 | 21 (0.88, 0.69-0.96) | 0.79 (0.62-0.89) | 0 | 0.97 |
| Sourcery | 27 | 23 | 17 (0.71, 0.51-0.85) | 0.85 (0.68-0.94) | 1 | 0.79 |
| vendor D (withheld under its terms) | 50 | 24 | 19 (0.79, 0.60-0.91) | 0.48 (0.35-0.61) | 4 | 1.47 |

What the pre-registered rule says:

- **No winner on recall.** The intervals overlap; three reviewers tie at 23 of 24 and the rest are inside their bands.
- **Precision separates.** Two reviewers return one genuine bug per two flags; two return one per one. The gap between
  0.96 (0.81-0.99) and 0.47 (0.35-0.59) does not overlap.
- **The shared soft spot** is a swallowed exception: 3 to 6 of 6 caught, across all reviewers.
- Union recall is 24 of 24: every seeded bug was found by someone.

Design notes: the key was sealed on `main` (SHA of `key.sealed`) before any PR existed; the reviewers were triggered
with their own on-demand commands, one comment per PR; no PR was edited after opening; the two control PRs carry one
obvious bug each and an arm that flags neither is void (none was). Vendor D's numbers are withheld because its terms of
service forbid publishing benchmark results; the row shows only that a sixth arm existed. Free tiers throughout; no
paid API.

Everything is public: the 34 open PRs with every bot comment, the sealed key hash, and (on publication) the key and
the scoring script. Read the PRs and disagree with the judges if you like; that is the point.

## Verify the seal yourself

`key.sealed` was committed on `main` in the first commit (before any pull request). `key.json` (24 seeded bugs: file,
line, archetype, original and mutated segment, branch) and `manifest.json` (every branch's kind and mutation spec) are
added now. Check that the key reproduces the sealed hash:

```
pip install sealeval
python verify_seal.py          # prints True if sha256(salt + canonical key) == key.sealed
```

`scoring/arena.py` is the locked script (one vendor's identifiers masked as vendor_d) that built the branches, collected the reviewers' comments, ran the blind
judges and produced the table (the judging step needs the two model CLIs; the scoring function itself is pure).

## Files

- `key.json`, `manifest.json`, `key.sealed`, `verify_seal.py`
- `scoring/arena.py`, `scoring/result.json` (the sealed table with intervals; vendor D masked), `scoring/verdicts.json`
  (both judges' verdict per finding), `scoring/findings_all.json` (every collected finding with arm and location)
- the 34 open pull requests (`pr-001` .. `pr-034`) with every reviewer comment as posted
