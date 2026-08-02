# Receipt delivery

The receipt ledger handles duplicate delivery attempts idempotently. A repeated
receipt identifier keeps the first validated payload instead of overwriting it.
