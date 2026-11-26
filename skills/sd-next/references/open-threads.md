# Open threads

Run when the operator says "open threads" or "collect followups".
List every open thread from this session: followups, recommendations not acted on, bugs or suggestions found but not filed, review advisories, and questions no sd note holds yet.

1. File each unfiled one as an sd item in its repo, or as a note on an existing item.
2. Decide the routine ones yourself, and record each ruling with its reason: `sd task note N "<ruling: reason>" --kind decision`.
3. Walk the operator through the rest one at a time with `AskUserQuestion`: one or two lines of context, your recommended option first.
4. Record each answer at once as a decision note: `sd task note N "<ruling>" --kind decision --answers <note>`.

End with a table: item, what it is, ruling or answer, next step.
