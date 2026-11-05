# __NAME__

A Flask starter: app factory, JSON API, one HTML page, SQLite.

    pip install -r requirements.txt
    flask --app __PKG__ run          # http://127.0.0.1:5000
    python -m pytest -q              # Flask tests skip (not fail) until Flask is installed

## Endpoints

| method | path | result |
|--------|------|--------|
| GET | / | HTML list of notes |
| GET | /api/notes | `[{"id","title","body"}]` |
| POST | /api/notes | `{"title","body"}` -> 201 note, 400 `{"error","fields"}` |
| DELETE | /api/notes/<id> | 204, 404 `{"error"}` |

`__PKG__/validate.py` has no Flask import, so its tests always run.
