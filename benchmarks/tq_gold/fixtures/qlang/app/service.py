from app.store import Repo, read_orders, save_order, write_log


def create_order(data):
    check(data)
    return save_order(data)


def check(data):
    return bool(data)


def list_orders():
    return read_orders()


def audit(msg):
    write_log(msg)


def persist(repo: Repo, x):
    repo.save(x)
