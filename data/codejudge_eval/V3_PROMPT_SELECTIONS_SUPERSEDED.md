# Prompt-v3 selections superseded before execution

`selection_v3a.json` and `selection_v3b.json` are immutable audit artifacts
bound to prompt bundle hash
`581755e5404b4a3ed929c0d4835d090daf4467960b71df38fc6575d478eaa03b`.
They were never used for a model run and must not be accepted by the formal
runner.

The active v3-A/v3-B selections are `selection_v3a_v4.json` and
`selection_v3b_v4.json`, bound to prompt bundle v4.  Bundle v4 removes the
unregistered root-cause `error_type` field from all three granularity
conditions.  The task/candidate sampling algorithm, seeds, entries and
holdout exclusions are otherwise unchanged.
