"""Users, sessions and login attempts in SQLite. Clock is injectable for tests."""
import re
import sqlite3
import threading
import time

from . import crypto

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, pw TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (digest TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS attempts (email TEXT PRIMARY KEY, failures INTEGER NOT NULL, locked_until REAL NOT NULL);
"""
MAX_FAILURES, LOCK_S, SESSION_S = 5, 300, 7 * 24 * 3600


class AuthError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Store:
    def __init__(self, path=":memory:", clock=time.time, iterations=crypto.ITERATIONS):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.clock, self.iterations, self.lock = clock, iterations, threading.Lock()

    def register(self, email, password):
        email = (email or "").strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise AuthError(400, "a valid email is required")
        if len(password or "") < 10:
            raise AuthError(400, "password must be at least 10 characters")
        with self.lock:
            try:
                cur = self.db.execute("INSERT INTO users (email, pw) VALUES (?, ?)",
                                      (email, crypto.hash_password(password, iterations=self.iterations)))
                self.db.commit()
            except sqlite3.IntegrityError:
                raise AuthError(409, "cannot register that email") from None
        return cur.lastrowid

    def login(self, email, password):
        """-> (user id, session token). Same error for unknown email and wrong password."""
        email = (email or "").strip().lower()
        now = self.clock()
        with self.lock:
            row = self.db.execute("SELECT failures, locked_until FROM attempts WHERE email=?", (email,)).fetchone()
            if row and row[1] > now:
                raise AuthError(429, "too many attempts, try again later")
            user = self.db.execute("SELECT id, pw FROM users WHERE email=?", (email,)).fetchone()
            if not user or not crypto.verify_password(password or "", user[1]):
                failures = (row[0] if row else 0) + 1
                locked = now + LOCK_S if failures >= MAX_FAILURES else 0
                self.db.execute("INSERT OR REPLACE INTO attempts VALUES (?, ?, ?)",
                                (email, 0 if locked else failures, locked))
                self.db.commit()
                raise AuthError(401, "wrong email or password")
            self.db.execute("DELETE FROM attempts WHERE email=?", (email,))
            token = crypto.new_session_token()
            self.db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (crypto.digest(token), user[0], now + SESSION_S))
            self.db.commit()
        return user[0], token

    def session_user(self, token):
        row = self.db.execute("SELECT user_id, expires FROM sessions WHERE digest=?", (crypto.digest(token or ""),)).fetchone()
        return row[0] if row and row[1] > self.clock() else None

    def logout(self, token):
        with self.lock:
            self.db.execute("DELETE FROM sessions WHERE digest=?", (crypto.digest(token or ""),))
            self.db.commit()

    def email_of(self, user_id):
        row = self.db.execute("SELECT email FROM users WHERE id=?", (user_id,)).fetchone()
        return row[0] if row else None
