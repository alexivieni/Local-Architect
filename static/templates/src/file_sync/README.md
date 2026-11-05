# __NAME__

One-way sync from SRC to DEST that shows its plan first.

    python -m __PKG__ ~/notes /mnt/backup/notes --dry-run      # print the plan only
    python -m __PKG__ ~/notes /mnt/backup/notes                # copy new + changed files
    python -m __PKG__ ~/notes /mnt/backup/notes --delete       # also remove files gone from SRC
    python -m __PKG__ SRC DEST --ignore "*.tmp" --ignore ".git/*"
    python -m pytest -q

Safety: deletes only with `--delete`; every write goes to a temp file renamed into place; DEST may not be
inside SRC (or the other way round); a manifest (`.sync-manifest.json` in DEST) remembers hashes so unchanged
files are compared by size+mtime only.
