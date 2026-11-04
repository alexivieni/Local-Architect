---
name: sql-and-migrations
description: Changing a database schema without losing data or locking a table
when: migration, schema, column, table, index, alter, alembic, makemigrations, database
files: *migrations*, *models*, *.sql
---
# Schema changes

A schema change is the one kind of change you cannot simply revert.

- **Add, then backfill, then switch, then drop.** Never rename or drop a column in the same change that starts using the new one.
  Add the new column nullable, fill it, move the readers, and only then remove the old one in a later change.
- **A new column on a large table needs a default that does not rewrite it.** Adding `NOT NULL DEFAULT x` rewrites every row on
  many engines. Add it nullable first.
- **Index creation locks.** Say in the change whether the index can be built concurrently on the engine in use.
- **Write the down path.** If the framework generates one, read it; a generated reverse step that drops a column will lose data.
- **A migration is code, so it gets a test.** At minimum, apply it to a copy of the schema and read the result.

Say which database engine you assumed. The same SQL behaves differently on SQLite, Postgres and MySQL, and guessing wastes a turn.
