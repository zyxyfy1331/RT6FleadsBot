# Team Lead Manager v4 — Auto Detect + Card BIN Groups

The bot still auto-detects both upload formats:

- repeated `Personal Information` blocks -> one block per lead
- otherwise -> one non-empty line per lead

For leads containing a labelled line such as:

`Card Number: 1234567890123456`

the bot stores only the first 6 digits as the lead's BIN group:

`123456`

After upload, if BINs are detected, the admin sees the BIN groups and can tap
which one should be active. `/lead` then serves only that BIN.

Admin command:
`/bins`

`ALL BINS` restores normal behaviour and serves all leads.

The full original lead remains stored as before for the existing private
lead workflow. Group posts do not add or expose card numbers.


## Strict Personal Information splitting

If `Personal Information` appears anywhere in an uploaded file, every occurrence
starts a new lead. Everything until the next occurrence stays in that lead.

If the file has no `Personal Information` marker, every non-empty line is one lead.

BIN grouping continues to read `Card Number:` from within each complete lead.
