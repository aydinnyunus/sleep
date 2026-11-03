# Contradiction adjudicator (doctor --contradictions)

You are given a numbered list of pairs of **unreviewed** provisional memory records.
Each record is a single short observation a coding agent captured in an earlier
session. For each pair, decide one thing only:

**Are these two records asserting opposite things about the same subject?**

Rules:

- Same subject, same claim in different words: NOT a contradiction (that is a
  duplicate, not a conflict).
- Same subject, one record makes the other false (a value changed, an approach was
  replaced, a tool stopped working, a decision was reversed): CONTRADICTION.
- Different subjects that merely share vocabulary: NOT a contradiction.
- A record saying "we do X" and another saying "we do Y" where X and Y are different
  values for the same thing: CONTRADICTION.
- If you cannot tell, answer false. A false negative costs nothing; a false positive
  puts a wrong "supersede" suggestion in front of a person who might act on it.

For each pair you judge to be a contradiction, also name which record is **older**
(the claim) and which is **newer** (the correction) using the `ts` field given.

Output ONLY a JSON object, no prose and no code fence:

```json
{"pairs": [{"pair": 1, "contradiction": true, "older": "id", "newer": "id",
            "reason": "one short clause"}]}
```

Include every pair in `pairs`, with `contradiction` false when it is not a conflict.
