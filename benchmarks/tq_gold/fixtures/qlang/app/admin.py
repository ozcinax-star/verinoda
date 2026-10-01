import sqlite3

from flask import request

from app.web import app

USERS = sqlite3.connect(":memory:")


@app.route("/admin/find", methods=["GET"])
def find_user():
    name = request.args.get("name")
    return USERS.execute("SELECT * FROM users WHERE name = '" + name + "'").fetchall()


@app.route("/admin/count", methods=["GET"])
def count_users():
    return USERS.execute("SELECT COUNT(*) FROM users").fetchone()
