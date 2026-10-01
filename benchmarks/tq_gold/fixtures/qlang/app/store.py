import sqlite3

DB = sqlite3.connect(":memory:")


def save_order(data):
    DB.execute("INSERT INTO orders VALUES (?)", (data,))
    DB.commit()
    return 1


def read_orders():
    return DB.execute("SELECT * FROM orders").fetchall()


def write_log(msg):
    DB.execute("INSERT INTO log VALUES (?)", (msg,))


def unused():
    return 0


class Repo:
    def save(self, x):
        DB.execute("UPDATE items SET x = ?", (x,))
