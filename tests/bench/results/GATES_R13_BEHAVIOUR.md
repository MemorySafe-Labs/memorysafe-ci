# Opt-in memory gates — behaviour notes (round 13)

PROTOTYPE. Every gate is OFF unless its `MEMORYSAFE_GATE_*` flag is set. With every flag off, the
code path, the tool schemas and the database are those of 0.4.19. The gates are lexical heuristics,
loosely analogous to (not models of) capacity limits, novelty and extinction in learning research.

## What changed in round 13 (review fixes)

- **No sidecar file.** The round-4 `<database>.gates-index` file (it held normalised memory text,
  and could keep the text of a memory forgotten in the same process) is gone. Nothing is written
  next to the database. Opening a store, flags on or off, and every `forget`, delete any such file
  (and `<database>.gates-index.<pid>.tmp`) that an older prototype left. `forget` also drops what
  the running process's gate caches hold about the memory.
- **Change log instead of a fingerprint.** When a capacity gate is on, the database holds a table
  `memorysafe_gate_changes(seq, row_id)` kept by three triggers on `memories`: the rowid of every
  inserted, updated or deleted memory row, by any process. It holds row ids and sequence numbers
  only — never text — and keeps the newest 4096 entries (a trigger trims it). Cached snapshots
  re-read exactly the rows changed since they were built, or rebuild when the log no longer reaches
  back. It is never created with the flags off. Uninstall/purge removes it with the database.
- **Open conflicts are never moved out** by any gate (lazy ranking, reference ranking, capacity-only
  gate, consolidate), and the UPDATE that moves a memory re-checks it. If every remaining
  candidate is protected, in an open conflict or just written, the store stays above capacity.
- **Possibly-stale flags are narrower.** A flag needs a note whose wording reads as a present
  status, sentence by sentence: "was installed … in 1980", "had been pending" are historical and
  never flagged. Age alone flags only a note that is entirely a present status claim; newer
  activity on a name flags a note only when the name sits in one of its present status sentences.
  This is word-pattern matching. It does not understand the text, and a durable fact worded
  like a present status can still be flagged.
- **Stale cleanup does not depend on the flags.** An explicit `restore` clears the memory's
  possibly-stale flags whatever the configuration; `resolve_conflict` with `revert` or `keep_both`
  withdraws the extinction AND stale evidence the two memories gave against each other (age
  evidence and evidence from other memories stays).
- **One ordering over the relation graph** in `find` (gates on): a flagged claim ranks below every
  newer memory that flagged it (chains and several flaggers included); with ORDER_RULE on, an
  extinguished-evidence pair ranks newer-first when the newer is at least as trusted; open
  conflicts likewise. Applied to all candidates, not only the returned window; cycles are broken
  deterministically (newest first, then id). `ORDER_RULE=0` disables only the extinction ordering.
- **Overrides are range-checked** (`MEMORYSAFE_GATE_R6_*`): non-numbers, NaN and infinities fall
  back to the default; weights are clamped at 0, the weakening cut at 0.95, shares at 1.
- **Host visibility (flags on):** `find` results carry `stale_reason` (stale on) and `weakened`
  (extinction on); the MCP `remember` result carries `extinguished_memory_ids`; the MCP `restore`
  result carries `weakening_undone`, `stale_cleared` and `would_undo_weakening`.
- **Cold first write.** The gate modules are imported only when a gate flag is on; the lazy
  ranking stops on (bound, rowid) instead of visiting every row that ties the last kept bound.

## Statements to keep accurate

- **Keep-both:** the two memories of a keep-both resolution never supersede each other and are
  never stale-flagged against each other. A LATER, third memory can still supersede either side
  through the ordinary lifecycle, exactly as in 0.4.19.
- **Restore and capacity:** restore can take the store above its capacity (a restored memory is
  never moved out by the restore itself); the next write brings the store back to the cap.
- **Age:** elapsed age is used by the possibly-stale age rule (`gates_stale`, present-status
  notes only) and, in the older capacity-only prototype, by the optional `MEMORYSAFE_GATE_AGE_GRACE`
  fade (off by default). Ranking ties fall back to insertion order. No gate deletes anything.
- **Evidence "distinct"** means distinct stored evidence strings/source ids, not independent
  observations: a correction and a recall-then-correct can come from the same write.
- `prune_merge_copies` (surprise gate, not in the candidate set) deletes hidden merge copies and
  is irreversible; it is dry-run by default and plans nothing unless a window or count is given.
