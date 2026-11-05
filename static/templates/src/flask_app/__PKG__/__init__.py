"""__NAME__: a Flask app factory. `flask --app __PKG__ run` finds create_app()."""
import os
import sqlite3

from .validate import note as validate_note


def create_app(db_path=None):
    from flask import Flask, abort, g, jsonify, render_template, request

    app = Flask(__name__)
    app.config["DB"] = db_path or os.environ.get("NOTES_DB", "notes.db")

    def db():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DB"])
            g.db.row_factory = sqlite3.Row
            g.db.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL)")
        return g.db

    @app.teardown_appcontext
    def close(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    def all_notes():
        return [dict(r) for r in db().execute("SELECT id, title, body FROM notes ORDER BY id")]

    @app.get("/")
    def index():
        return render_template("index.html", notes=all_notes())

    @app.get("/api/notes")
    def list_notes():
        return jsonify(all_notes())

    @app.post("/api/notes")
    def add_note():
        clean, errors = validate_note(request.get_json(silent=True))
        if errors:
            return jsonify({"error": "invalid input", "fields": errors}), 400
        cur = db().execute("INSERT INTO notes (title, body) VALUES (?, ?)", (clean["title"], clean["body"]))
        db().commit()
        return jsonify({"id": cur.lastrowid, **clean}), 201

    @app.delete("/api/notes/<int:note_id>")
    def delete_note(note_id):
        cur = db().execute("DELETE FROM notes WHERE id=?", (note_id,))
        db().commit()
        if not cur.rowcount:
            abort(404)
        return "", 204

    @app.errorhandler(404)
    def not_found(_e):
        return jsonify({"error": "not found"}), 404

    return app
