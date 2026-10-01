"""Tables, ORM models, migrations and injection as graph nodes and edges (``verinoda.dataschema``): the dataflow
view reaches a table, each framework's naming, reads and writes, injection, the CLI and a live SQLite file."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import architecture_map as am  # noqa: E402
from verinoda import dataschema, index, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

PY_FILES = {
    "shop/__init__.py": "",
    "shop/models.py": '''from django.db import models


class Customer(models.Model):
    name = models.CharField(max_length=80)

    class Meta:
        db_table = "customers"


class Invoice(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    total = models.DecimalField()


class Base(models.Model):
    class Meta:
        abstract = True
''',
    "shop/views.py": '''from shop.models import Customer, Invoice


def list_invoices(request):
    return Invoice.objects.filter(customer__name="x")


def new_customer(request):
    c = Customer(name="y")
    c.save()
    return c
''',
    "store/__init__.py": "",
    "store/db.py": '''from sqlalchemy import Column, Integer, String, Table, MetaData
from sqlalchemy.orm import declarative_base
from sqlmodel import SQLModel, Field

Base = declarative_base()
meta = MetaData()

audit = Table("audit_log", meta, Column("id", Integer), Column("event", String))


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    title = Column("product_title", String)


class Tag(SQLModel, table=True):
    id: int = Field(primary_key=True)
    label: str = Field()
''',
    "store/service.py": '''from fastapi import APIRouter, Depends

from store.db import Product

router = APIRouter()


def get_session():
    return None


@router.post("/products")
def add_product(title: str, session=Depends(get_session)):
    session.add(Product(title=title))
    session.commit()


def report(session):
    rows = session.query(Product).all()
    session.execute("UPDATE inventory SET checked = 1 WHERE id = ?", (1,))
    return rows
''',
    "migrations/versions/0001_init.py": '''from alembic import op
import sqlalchemy as sa


def upgrade():
    op.create_table("inventory", sa.Column("id", sa.Integer), sa.Column("checked", sa.Integer))
    op.add_column("products", sa.Column("price", sa.Integer))
''',
    "db/migration/V1__init.sql": '''CREATE TABLE IF NOT EXISTS "products" (
    id INTEGER PRIMARY KEY,
    product_title TEXT,
    price INTEGER,
    CONSTRAINT x UNIQUE (id)
);
ALTER TABLE products ADD COLUMN stock INTEGER;
''',
}

JAVA_FILES = {
    "src/main/java/app/Order.java": '''package app;

import jakarta.persistence.*;

@Entity
@Table(name = "purchase_orders")
public class Order {
    @Id Long id;
}
''',
    "src/main/java/app/OrderLine.java": '''package app;

@Entity
public class OrderLine {
    Long id;
}
''',
    "src/main/java/app/OrderRepository.java": '''package app;

public interface OrderRepository extends JpaRepository<Order, Long> {
}
''',
    "src/main/java/app/OrderService.java": '''package app;

@Service
public class OrderService {
    private final OrderRepository orderRepository;

    public OrderService(OrderRepository orderRepository) {
        this.orderRepository = orderRepository;
    }

    public Order place(Order o) {
        return orderRepository.save(o);
    }

    public Iterable<Order> all() {
        return orderRepository.findAll();
    }
}
''',
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True, stdin=subprocess.DEVNULL)


def _scanned(root: Path, files: dict[str, str] | None = None, copy_from: Path | None = None) -> Path:
    if copy_from is not None:
        shutil.copytree(copy_from, root, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", "*.db"))
    for rel, text in (files or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    workflow.init(root)
    st = open_store(root)
    try:
        workflow.scan(st, root)
    finally:
        st.close()
    return root


@pytest.fixture(scope="module")
def orders(tmp_path_factory):
    return _scanned(tmp_path_factory.mktemp("ds") / "orders_app", copy_from=ROOT / "examples" / "orders_app")


@pytest.fixture(scope="module")
def py(tmp_path_factory):
    return _scanned(tmp_path_factory.mktemp("ds") / "py", PY_FILES)


@pytest.fixture(scope="module")
def jvm(tmp_path_factory):
    return _scanned(tmp_path_factory.mktemp("ds") / "jvm", JAVA_FILES)


def _tables(res: dict) -> dict:
    return {t["name"]: t for t in res["tables"]}


# -- the done-when: the dataflow view reaches a table ------------------------------------------------

def test_the_dataflow_view_reaches_a_table(orders):
    g = index.load(orders)
    assert dataschema.is_table(g, "table:orders")
    view = am.dataflow(g)
    ends = [p for p in view["paths"] if p["hops"] and p["hops"][-1]["to_id"] == "table:orders"]
    assert ends, [p["sink"] for p in view["paths"]]
    last = ends[0]["hops"][-1]
    assert last["relation"] == "writes_table" and last["confidence"] == "INFERRED"
    assert last["derived_by"] == "verinoda.dataschema" and last["at"].startswith("orders/repository.py:")
    # the sink stays the function that writes; the table is said apart
    assert "sql-write" in ends[0]["sink_kinds"] and ends[0]["table"] == "orders"
    assert "verinoda.dataschema" in view["coverage"]["method"]


def test_the_table_node_is_not_a_symbol(orders):
    g = index.load(orders)
    node = g.G.nodes["table:orders"]
    assert node["file_type"] == "schema" and node["label"] == "orders" and node["defined"] is True
    assert node["columns"] == ["id", "customer", "total"]
    assert not g.is_symbol("table:orders") and "table:orders" not in g.symbols_in("orders/repository.py")


# -- naming per framework and reads / writes --------------------------------------------------------

def test_python_frameworks(py):
    g = index.load(py, augment=False)
    res = dataschema.report(g)
    t = _tables(res)
    assert set(t) >= {"customers", "shop_invoice", "products", "audit_log", "tag", "inventory"}
    assert "shop_base" not in t   # abstract
    assert t["customers"]["defs"][0]["via"] == "django Meta.db_table"
    assert t["shop_invoice"]["defs"][0]["via"] == "django default: <app>_<model>"
    assert t["shop_invoice"]["columns"] == ["customer_id", "total"]
    assert t["tag"]["defs"][0]["via"].startswith("SQLModel default")
    assert {d["via"] for d in t["products"]["defs"]} >= {"sqlalchemy __tablename__", "CREATE TABLE in a migration"}
    assert set(t["products"]["columns"]) >= {"id", "product_title", "price", "stock"}
    assert t["audit_log"]["columns"] == ["id", "event"]

    def ops(name):
        return {(u["op"], u["by"]) for u in t[name]["uses"]}
    assert ("read", "list_invoices()") in ops("shop_invoice")
    assert ("write", "new_customer()") in ops("customers")
    assert ("write", "add_product()") in ops("products") and ("read", "report()") in ops("products")
    assert ("write", "report()") in ops("inventory") and ("migrate", "upgrade()") in ops("inventory")
    assert ("migrate", "upgrade()") in ops("products")


def test_graph_edges_and_injection(py):
    g = index.load(py)
    rel = {(g.label(u), d["relation"], g.label(v)) for u, v, d in g.edges({"maps_to", "writes_table", "reads_table",
                                                                          "migrates", "injects"})}
    assert ("Product", "maps_to", "products") in rel
    assert ("add_product()", "writes_table", "products") in rel
    assert ("add_product()", "injects", "get_session()") in rel
    declared = [d for u, v, d in g.edges({"maps_to"}) if g.label(u) == "Customer"]
    assert declared and declared[0]["confidence"] == "EXTRACTED"
    derived = [d for u, v, d in g.edges({"maps_to"}) if g.label(u) == "Invoice"]
    assert derived and derived[0]["confidence"] == "INFERRED"


def test_jpa_entities_repositories_and_injection(jvm):
    g = index.load(jvm)
    res = dataschema.report(g)
    t = _tables(res)
    assert t["purchase_orders"]["defs"][0]["via"] == "JPA @Table(name)"
    assert t["order_line"]["defs"][0]["via"].startswith("JPA default")
    ops = {(u["op"], u["by"]) for u in t["purchase_orders"]["uses"]}
    assert ("write", ".place()") in ops or any(op == "write" and (by or "").endswith("place()") for op, by in ops)
    assert any(op == "read" and (by or "").endswith("all()") for op, by in ops)
    assert any(i["via"] == "Spring constructor injection" and i["to"].endswith("OrderRepository")
               for i in res["injections"])


def test_sql_patterns():
    assert dataschema.sql_tables("INSERT INTO orders (a) VALUES (?)") == [("orders", "write")]
    assert dataschema.sql_tables("DELETE FROM public.Orders WHERE id = 1") == [("orders", "write")]
    assert dataschema.sql_tables("UPDATE `t` SET x = 1") == [("t", "write")]
    assert sorted(dataschema.sql_tables("SELECT * FROM a JOIN b ON a.id = b.id")) == [("a", "read"), ("b", "read")]
    assert dataschema.sql_tables("SELECT 1") == []
    assert dataschema.create_columns('CREATE TABLE x (id INT, "name" TEXT, PRIMARY KEY (id))', "x") == ["id", "name"]
    assert dataschema.snake("OrderLine") == "order_line" and dataschema.snake("URLMap") == "urlmap"


# -- the sidecar ---------------------------------------------------------------------------------------

def test_facts_are_reused_and_a_damaged_block_is_skipped(orders):
    g = index.load(orders, augment=False)
    files, nodes, edges, rep = dataschema.collect(g)
    files2, *_ = dataschema.collect(g, old=files)
    assert files2 == files
    g2 = index.load(orders, augment=False)
    dataschema.apply(g2, {"nodes": [["table:x", {"label": "x"}], ["bad"], "nope", ["not_a_table", {}]],
                          "edges": [["a", "b"], 3, ["table:x", "table:x", "nope"]]})
    assert "table:x" in g2.G and "not_a_table" not in g2.G


# -- the CLI and a live database -------------------------------------------------------------------------

def test_cli_text_json_and_a_live_sqlite_database(orders, capsys, tmp_path):
    from verinoda import cli

    assert cli.main(["schema", "--repo", str(orders)]) == 0
    text = capsys.readouterr().out
    assert "orders: orders/repository.py:" in text and "written by: " in text
    assert cli.main(["schema", "--repo", str(orders), "--json", "--table", "orders"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [t["name"] for t in out["tables"]] == ["orders"] and out["status"] == "strong_inference"
    db = tmp_path / "live.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE orders (id INTEGER, customer TEXT, total REAL, note TEXT)")
    conn.execute("CREATE TABLE extra (id INTEGER)")
    conn.commit()
    conn.close()
    assert cli.main(["schema", "--repo", str(orders), "--db", str(db), "--json"]) == 3
    live = json.loads(capsys.readouterr().out)["live"]
    assert live["only_in_database"] == ["extra"] and live["status"] == "observed"
    assert live["columns"] == [{"table": "orders", "only_in_code": [], "only_in_database": ["note"]}]
    assert cli.main(["schema", "--repo", str(orders), "--db", str(tmp_path / "missing.db")]) == 2
    bad = tmp_path / "not.db"
    bad.write_text("not a database", encoding="utf-8")
    assert cli.main(["schema", "--repo", str(orders), "--db", str(bad)]) == 2
    capsys.readouterr()
