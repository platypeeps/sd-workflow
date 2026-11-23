Merge build/sd-1423 into build/sd-1623

Carry the reviewed sd:9867, sd:9869 and sd:9873 work onto the fresh
bundle branch, as lead note #74304 asks, over main at 8ba46c716.

Conflicts with pack sd:9769 (finding `instances`) resolved by keeping
both: every high fixture carries `instances` and an `evidence` repro,
`dispose` passes `instances` to `recorded_at`, `distinct_findings`
joins every distinct evidence and every instance, and `review` keeps
the `covers_from` reset after `finish_review`.

The URL contract lost its first key list, which the restated output
shape repeats: 1696 -> 1568 bytes, under its 1600. The review lane is
measured at 5075 lines (`sd-review` 3105) and its cap set to that.
