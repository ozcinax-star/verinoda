from flask import Flask, request

from app.service import audit, create_order, list_orders, persist
from app.store import Repo

app = Flask(__name__)


@app.route("/orders", methods=["POST"])
def post_order():
    return create_order(request.json)


@app.route("/orders", methods=["GET"])
def get_orders():
    return list_orders()


@app.route("/items", methods=["PUT"])
def put_item():
    return persist(Repo(), 1)


def not_a_handler():
    return audit("x")
