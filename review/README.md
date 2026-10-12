# Local Morning review editor

Run with Python 3.9+ on macOS or Linux. No packages need installing.

```sh
cd /Users/yuku/f/git/meid
python3 review/app.py --open
```

Open http://127.0.0.1:5173/ (the server listens only on this computer).
Use `--port 5174` if 5173 is already occupied. Stop with Ctrl+C.

The middle column reads `materials/in/meid-am.txt`; the right column previews
only that finding's suggested corrections. **Edit & apply suggestion** opens an
editable proposal based on the latest file. You can adjust it before pressing
**Apply this suggestion**. Opening or cancelling a proposal never writes the file.
Other findings on the same date are not automatically applied.

Yellow highlights locate the passage under review and are never saved. `<i>…</i>`
is retained and shown in the live preview. If a passage has already changed and
cannot be matched safely, your current wording is kept in the editor and the
suggestion is shown separately for manual incorporation. Identical duplicate
October 27 blocks are updated together; divergent duplicates are rejected.

Use **Reload meid-am.txt** after changing the file in another editor. Applied
findings, including your custom wording, are recorded locally and omitted from
the list. External edits to that date invalidate this record so it can be checked
again. Applying another finding on the same date preserves the application record.

Review checks live in `review/.local/progress.sqlite3`. Each check is tied to the
entry contents: editing a date resets that date's checks. Checks survive server
restarts. Export/import uses the same progress JSON format as the hosted report.
Import merges checked items into the current wording; it never unchecks existing
items. Download a backup from the hosted report to transfer its progress.

Every application that changes the source creates a complete before-save snapshot in
`review/.local/backups/`. Keep this directory in your regular computer backups.
Backups are not removed automatically. To restore, stop the server, copy the
chosen snapshot over `materials/in/meid-am.txt`, and restart. Nothing is committed
or published by the app.

Stale editors are rejected instead of overwriting newer changes. On a conflict,
your unsaved text remains in the dialog: copy it before closing, reopen the
entry, and merge. Close the app before running non-cooperating scripts that
rewrite the source simultaneously. The app uses atomic replacement and checks
for external changes immediately before writing.

The immutable before-edit and initially applied snapshots in `data/` support the
comparison table. English highlights are exact phrases in
`data/english-highlights.json`, without whole-sentence expansion. English and
Indonesian italics are rendered from source markup. Whole-entry highlighting for
October 31 reflects its complete Morning/Evening replacement.

Run the persistence, conflict, source-integrity, and rendering checks with:

```sh
python3 -m unittest discover -s review -p 'test_*.py'
```

The list omits findings when every suggested correction is present in the current
file, ignoring whitespace and italics. It also recognizes the individually assessed
wording alternatives in `data/correction-variants.json`. Partial corrections and
unrecognized wording remain for checking. Reloading recomputes the list; saving
removes newly corrected findings for that date. Original audit records and review
progress are retained. Removing a finding does not mark it reviewed.

## Resume on another machine

Clone or pull this repository, then restore the committed progress checkpoint
once on the new machine before starting the app:

```sh
mkdir -p review/.local
cp -n review/progress-checkpoint.sqlite3 review/.local/progress.sqlite3
python3 review/app.py --open
```

The checkpoint includes review checks and individually applied findings as of
October 12, 2026 (five applied findings and no review checks). It is a transfer
snapshot, not automatic synchronization. The app continues saving new progress
to the ignored `.local` database. The copy command preserves any progress database
already on that machine.
