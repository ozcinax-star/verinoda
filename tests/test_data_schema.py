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
    assert t["shop_invoice"]["columns"] == ["id", "customer_id", "total"]   # Django's implicit primary key
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


# -- review round: realistic projects -----------------------------------------------------------------

PY2_FILES = {
    "core/__init__.py": "",
    "core/models.py": '''from django.db import models


class TimeStamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True
''',
    "blog/__init__.py": "",
    "blog/models.py": '''from django.db import models

from core.models import TimeStamped


class Tag(models.Model):
    name = models.CharField(max_length=40)


class Post(TimeStamped):
    title = models.CharField(max_length=100)
    tags = models.ManyToManyField(Tag)
''',
    "blog/views.py": '''import logging

from blog.models import Post

log = logging.getLogger(__name__)


def post_list(request):
    posts = Post.objects.filter(title__startswith="a")
    log.info(f"Delete from cache failed for {request}")
    if not posts:
        raise ValueError(f"Insert into outbox failed: {request}")
    return posts


def remember(request):
    p = Post(title="x")
    pending = set()
    pending.add(p)
    seen = {}
    return seen.get(Post)
''',
    "shop/__init__.py": "",
    "shop/models.py": '''from django.db import models


class Item(models.Model):
    sku = models.CharField(max_length=20)
''',
    "shop/views.py": '''from shop.models import Item


def items(request):
    return Item.objects.all()
''',
    "shop/migrations/__init__.py": "",
    "shop/migrations/0001_initial.py": '''from django.db import migrations, models


class Migration(migrations.Migration):
    operations = [
        migrations.CreateModel(name="Item", fields=[("sku", models.CharField(max_length=20))]),
    ]
''',
    "billing/__init__.py": "",
    "billing/models.py": '''from django.db import models


class Item(models.Model):
    amount = models.IntegerField()
''',
    "ml/__init__.py": "",
    "ml/models/__init__.py": "",
    "ml/models/net.py": '''import tensorflow as tf


class Net(tf.keras.models.Model):
    def call(self, x):
        return x
''',
    "api/__init__.py": "",
    "api/db.py": '''from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Account(Base):
    # the owner's ledger
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True)
    owner = Column(String)


def get_session():
    return None
''',
    "api/deps.py": '''from typing import Annotated

from fastapi import Depends

from api.db import get_session

SessionDep = Annotated[object, Depends(get_session)]


def verify_token():
    return True
''',
    "api/routes.py": '''from fastapi import APIRouter, Depends
from sqlalchemy import select

from api.db import Account
from api.deps import SessionDep, verify_token

router = APIRouter()


@router.get("/accounts", dependencies=[Depends(verify_token)])
def list_accounts(session: SessionDep):
    return load_accounts(session)


def load_accounts(session):
    return session.scalars(select(Account)).all()


class AccountViews:
    @router.post("/accounts", dependencies=[Depends(verify_token)])
    def open_account(self, session: SessionDep):
        session.add(Account(owner="x"))
''',
    "api/orders.py": '''def orders(request):
    return []


def main():
    return orders(None)
''',
    "db/schema.sql": '''-- TODO: drop table customers_old
CREATE TABLE orders (
    id INTEGER PRIMARY KEY, -- the key, never reused
    placed_at TEXT
);
CREATE TABLE
    shipments (
    id INTEGER PRIMARY KEY,
    order_id INTEGER
);
/* ALTER TABLE ghost ADD COLUMN x INTEGER; */
ALTER TABLE shipments
    ADD COLUMN weight INTEGER;
''',
    "store/__init__.py": "",
    "store/raw.py": '''import logging
import sqlite3

log = logging.getLogger(__name__)
INSERT_EVENT = "INSERT INTO events (kind) VALUES (?)"


class Archive:
    PURGE = "DELETE FROM archive WHERE day < ?"

    def purge(self, conn):
        conn.execute(self.PURGE, (1,))


def record(conn: sqlite3.Connection, kind):
    conn.execute(INSERT_EVENT, (kind,))
    log.warning(f"Insert into events failed for {kind}")


def yearly(conn):
    return conn.execute("SELECT EXTRACT(year FROM created_at) FROM audit_events").fetchall()


def recent(conn):
    return conn.execute("WITH recent AS (SELECT * FROM events) SELECT * FROM recent").fetchall()


def catalog(conn):
    return conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'x'").fetchall()
''',
    "store/schema.py": '''import sqlite3

SCHEMA = """
-- the event log, appended to by store.raw
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    kind TEXT
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn
''',
    "flaskapp/__init__.py": "",
    "flaskapp/models.py": '''from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class AuditEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    note = db.Column(db.String)
''',
}

SPRING_FILES = {
    "src/main/java/app/Order.java": '''package app;

import jakarta.persistence.*;

@Entity
@Table(name = "orders")
public class Order {
    @Id Long id;
    String status;
}
''',
    "src/main/java/app/PurchaseRecord.java": '''package app;

import jakarta.persistence.*;

@Entity(name = "Purchase")
public class PurchaseRecord {
    @Id Long id;
}
''',
    "src/main/java/app/OrderRepository.java": '''package app;

import java.util.List;

public interface OrderRepository extends JpaRepository<Order, Long> {
    @Query("SELECT o FROM Order o WHERE o.id = :id")
    Order byId(Long id);

    @Query(value = """
        SELECT * FROM orders WHERE status = 'OPEN'
        """, nativeQuery = true)
    List<Order> open();

    @Query("SELECT p FROM Purchase p")
    List<PurchaseRecord> purchases();
}
''',
    "src/main/java/app/AuditDao.java": '''package app;

@Repository
public class AuditDao {
    private static final String FIND = "SELECT * FROM audit WHERE id = ?";
    private final JdbcTemplate jdbc;

    public AuditDao(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public void prune() {
        // jdbc.update("DELETE FROM users_backup");
        /* jdbc.update("INSERT INTO ghost_table VALUES (1)"); */
        jdbc.update("DELETE FROM audit WHERE old = 1");
    }

    public Object find(long id) {
        return jdbc.queryForObject(FIND, Object.class, id);
    }
}
''',
    "src/main/java/app/OrderService.java": '''package app;

@Service
@RequiredArgsConstructor
public class OrderService {
    private final OrderRepository orders;

    @Autowired
    @Qualifier("primary")
    private AuditDao auditDao;

    public Order find(Long id) {
        return orders.byId(id);
    }
}
''',
    "src/main/kotlin/app/Items.kt": '''package app

import jakarta.persistence.*

@Entity @Table(name = "line_items") class LineItem(@Id val id: Long = 0)

@Entity class ShippingLabel(@Id val id: Long = 0)

@Service
class LabelService @Autowired constructor(private val auditDao: AuditDao) {
    fun count(): Int = 0
}
''',
    "src/main/resources/db/migration/V1__init.sql": '''-- Flyway: the first schema
-- DROP TABLE legacy_orders;
CREATE TABLE orders (
    id BIGINT PRIMARY KEY,
    status VARCHAR(20) -- OPEN, PAID, SHIPPED
);
/* CREATE TABLE ghost (id INT); */
CREATE TABLE
    line_items (
    id BIGINT PRIMARY KEY,
    order_id BIGINT
);
''',
}


@pytest.fixture(scope="module")
def py2(tmp_path_factory):
    return _scanned(tmp_path_factory.mktemp("ds") / "py2", PY2_FILES)


@pytest.fixture(scope="module")
def spring(tmp_path_factory):
    return _scanned(tmp_path_factory.mktemp("ds") / "spring", SPRING_FILES)


def _rels(g, relations=dataschema.RELATIONS) -> set[tuple[str, str, str]]:
    return {(g.label(u), d["relation"], g.label(v)) for u, v, d in g.edges(set(relations))}


def _line(root: Path, at: str) -> str:
    f, _, n = at.rpartition(":")
    return (root / f).read_text(encoding="utf-8").splitlines()[int(n) - 1]


def test_log_and_exception_f_strings_are_not_sql(py2):
    g = index.load(py2, augment=False)
    t = _tables(dataschema.report(g))
    assert "cache" not in t and "outbox" not in t
    assert ("post_list()", "reads_table", "blog_post") in _rels(index.load(py2))
    # a value of an f-string is no SQL marker: the lower-case log line is prose
    assert not dataschema.looks_like_sql("Delete from cache failed for {}")
    assert dataschema.sql_tables("SELECT * FROM {}") == []


def test_quoted_schema_qualified_names_give_the_table():
    assert dataschema.sql_tables('CREATE TABLE "public"."invoices" (id int)') == [("invoices", "create")]
    assert dataschema.sql_tables("SELECT * FROM [dbo].[Customers]") == [("customers", "read")]
    assert dataschema.sql_tables("CREATE TABLE [dbo].[Customers] (Id int)") == [("customers", "create")]
    assert dataschema.create_columns('CREATE TABLE "public"."invoices" (id int, total int)', "invoices") == \
        ["id", "total"]
    fixture = (ROOT / "tests_upstream" / "fixtures" / "sample_plpgsql_quoted.sql").read_text(encoding="utf-8")
    names = {t["name"] for t in dataschema.file_facts(fixture, "db/sample.sql")["tables"]}
    assert names and "public" not in names


def test_from_inside_a_function_call_and_cte_names_are_not_tables(py2):
    assert dataschema.sql_tables("SELECT EXTRACT(year FROM created_at) FROM audit_events") == \
        [("audit_events", "read")]
    assert dataschema.sql_tables("SELECT trim(both ' ' FROM name) FROM people") == [("people", "read")]
    assert dataschema.sql_tables("WITH recent AS (SELECT * FROM events) SELECT * FROM recent") == [("events", "read")]
    assert sorted(dataschema.sql_tables("SELECT * FROM a WHERE id IN (SELECT a_id FROM b)")) == \
        [("a", "read"), ("b", "read")]
    t = _tables(dataschema.report(index.load(py2, augment=False)))
    assert not {"created_at", "recent", "tables"} & set(t)   # information_schema.tables is a catalog
    assert {(u["op"], u["by"]) for u in t["audit_events"]["uses"]} == {("read", "yearly()")}
    assert ("read", "recent()") in {(u["op"], u["by"]) for u in t["events"]["uses"]}


def test_jpql_names_entities_and_a_native_text_block_names_tables(spring):
    g = index.load(spring)
    t = _tables(dataschema.report(g))
    assert "order" not in t and "purchaserecord" not in t
    rels = _rels(g)
    readers = {u for u, r, v in rels if r == "reads_table" and v == "orders"}
    assert any(u.endswith("byId()") for u in readers) and any(u.endswith("open()") for u in readers), readers
    vias = {u["via"] for u in t["orders"]["uses"]}
    assert "JPQL in @Query on Order" in vias and "SQL string" in vias
    # @Entity(name = "Purchase"): the JPQL name and the default table
    assert t["purchase"]["defs"][0]["via"].startswith("JPA default")
    assert any(u["via"] == "JPQL in @Query on Purchase" for u in t["purchase"]["uses"])


def test_commented_out_java_sql_is_not_read(spring):
    g = index.load(spring)
    t = _tables(dataschema.report(g))
    assert "users_backup" not in t and "ghost_table" not in t and "ghost" not in t and "legacy_orders" not in t
    ops = {(u["op"], u["by"]) for u in t["audit"]["uses"]}
    assert any(op == "write" and by.endswith("prune()") for op, by in ops)
    # the constant's read belongs to the method that names it, not to the class
    assert any(op == "read" and by.endswith("find()") for op, by in ops), ops
    assert not any(by.endswith("AuditDao") for _op, by in ops)


def test_a_kotlin_entity_takes_its_own_table(spring):
    t = _tables(dataschema.report(index.load(spring, augment=False)))
    assert t["shipping_label"]["defs"][0]["via"].startswith("JPA default")
    assert "line_items" in t and [d["via"] for d in t["line_items"]["defs"]].count("JPA @Table(name)") == 1


def test_django_abstract_bases_and_a_keras_model(py2):
    g = index.load(py2)
    t = _tables(dataschema.report(g))
    assert t["blog_post"]["defs"][0]["via"] == "django default: <app>_<model>"
    assert t["blog_post"]["columns"] == ["id", "created_at", "title"]   # the abstract base's field, no M2M column
    assert t["blog_post_tags"]["columns"] == ["id", "post_id", "tag_id"]
    assert "core_timestamped" not in t and "ml_net" not in t
    assert ("Post", "maps_to", "blog_post") in _rels(g)


def test_a_model_in_a_set_or_a_dict_is_no_table_use(py2):
    g = index.load(py2)
    t = _tables(dataschema.report(g))
    ops = {(u["op"], u["by"]) for u in t["blog_post"]["uses"]}
    assert ("write", "remember()") not in ops and ("read", "remember()") not in ops
    rels = _rels(g)
    assert (".open_account()", "writes_table", "accounts") in rels   # handed to a session it is one


def test_django_create_model_names_its_own_app(py2):
    t = _tables(dataschema.report(index.load(py2, augment=False)))
    assert ("migrate", "Migration") in {(u["op"], u["by"]) for u in t["shop_item"]["uses"]}
    assert not any(u["op"] == "migrate" for u in t["billing_item"]["uses"])
    # two apps with an Item: the view's import says which
    assert ("read", "items()") in {(u["op"], u["by"]) for u in t["shop_item"]["uses"]}


def test_sql_files_without_comments_and_with_multi_line_statements(py2, spring):
    # a schema string that opens with a comment is still SQL
    assert dataschema.looks_like_sql("\n-- D8 question plans\nCREATE TABLE IF NOT EXISTS plans (id TEXT)")
    t = _tables(dataschema.report(index.load(py2, augment=False)))
    assert "customers_old" not in t and "ghost" not in t
    assert t["shipments"]["columns"] == ["id", "order_id", "weight"]
    assert t["orders"]["columns"] == ["id", "placed_at"]
    s = _tables(dataschema.report(index.load(spring, augment=False)))
    assert s["orders"]["columns"] == ["id", "status"] and "line_items" in s
    assert any(d["via"] == "CREATE TABLE in a migration" for d in s["line_items"]["defs"])


def test_fastapi_decorator_dependencies_and_annotated_aliases(py2):
    rels = _rels(index.load(py2), {"injects"})
    assert ("list_accounts()", "injects", "verify_token()") in rels
    assert ("list_accounts()", "injects", "get_session()") in rels       # through SessionDep
    assert (".open_account()", "injects", "verify_token()") in rels      # the method, not its class
    assert not any(u == "AccountViews" for u, _r, _v in rels)


def test_a_table_and_its_mapping_cite_the_line_naming_the_table(py2, spring):
    g = index.load(py2)
    node = g.G.nodes["table:accounts"]
    at = f"{node['source_file']}:{node['source_location'][1:]}"
    assert "accounts" in _line(py2, at)
    d = next(d for u, v, d in g.edges({"maps_to"}) if v == "table:accounts")
    assert d["confidence"] == "EXTRACTED" and "accounts" in _line(py2, f"{d['source_file']}:{d['source_location'][1:]}")
    from verinoda import graphquery

    res = graphquery.run(py2, 'match (c)-[maps_to]->(t) where t.name = "accounts" return c, t', verify=True)
    assert res["rows"] and "does not name" not in json.dumps(res)
    js = index.load(spring)
    d = next(d for u, v, d in js.edges({"maps_to"}) if v == "table:orders")
    assert "orders" in _line(spring, f"{d['source_file']}:{d['source_location'][1:]}")
    # a schema string that opens with a comment: the table is cited at its CREATE TABLE line, and the function
    # running the constant migrates it
    t = _tables(dataschema.report(g))
    schema_def = next(x["at"] for x in t["events"]["defs"] if x["at"].startswith("store/schema.py:"))
    assert "CREATE TABLE IF NOT EXISTS events" in _line(py2, schema_def)
    assert ("create", "connect()") in {(u["op"], u["by"]) for u in t["events"]["uses"]}
    assert ("connect()", "migrates", "events") in _rels(g)
    # a native text block's table is cited at the line naming it
    s = _tables(dataschema.report(js))
    native = next(u for u in s["orders"]["uses"] if u["via"] == "SQL string")
    assert "FROM orders" in _line(spring, native["at"])


def test_a_table_gives_way_to_code_of_the_same_name(py2, capsys):
    from verinoda import cli, naming

    g = index.load(py2)
    pool, aside = naming.exact_nodes(g, "orders")
    assert [g.label(n) for n in pool] == ["orders()"] and "table:orders" in aside
    assert cli.main(["trace", "main", "orders", "--repo", str(py2), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "found"
    assert cli.main(["butterfly", "orders", "--repo", str(py2), "--json"]) == 0
    assert "ambiguous" not in capsys.readouterr().out.lower()


def test_tables_follow_a_sql_only_change(tmp_path):
    root = _scanned(tmp_path / "sqlonly", {"app/__init__.py": "", "app/main.py": "def main():\n    return 1\n",
                                           "db/schema.sql": "CREATE TABLE orders (id INTEGER);\n"})
    with (root / "db" / "schema.sql").open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("CREATE TABLE refunds (id INTEGER, amount INTEGER);\n")
    st = open_store(root)
    try:
        res = workflow.update(st, root)
    finally:
        st.close()
    assert res["index_mode"] == "none"   # no graph file changed: the graph is kept
    g = index.load(root)
    assert dataschema.is_table(g, "table:refunds") and g.G.nodes["table:refunds"]["columns"] == ["id", "amount"]
    from verinoda import graphquery

    assert graphquery.run(root, 'match (t) where t.name = "refunds" return t')["rows"]


def test_the_query_language_knows_the_new_relations(py2):
    from verinoda import graphquery

    for rel in dataschema.RELATIONS:   # each is known
        graphquery.run(py2, f"match (a)-[{rel}]->(b) return a, b")
    # a graph without such an edge answers with no row (a note), not "unknown relation"
    res = graphquery.run(py2, 'match (a)-[migrates]->(b) where b.name = "no_such_table" return a, b')
    assert res["rows"] == []
    g = index.load(py2)
    for u, v, k in [(u, v, k) for u, v, k, d in g.G.edges(keys=True, data=True) if d.get("relation") == "migrates"]:
        g.G.remove_edge(u, v, k)
    res = graphquery.run(py2, "match (a)-[migrates]->(b) return a, b", graph=g)
    assert res["rows"] == [] and any("migrates" in n for n in res.get("notes") or [])


def test_an_orm_only_function_is_the_sink_and_the_text_names_the_table(py2):
    from verinoda import map_text

    g = index.load(py2)
    view = am.dataflow(g)
    path = next(p for p in view["paths"] if p.get("table") == "accounts" and p["entry"].startswith("api/routes.py"))
    assert path["sink"].startswith("api/routes.py:") and path["sink_kinds"] == ["table-read"]
    assert path["hops"][-1]["relation"] == "reads_table" and path["hops"][-2]["to"] == "load_accounts()"
    assert not any(s["symbol"] == "accounts" for s in view["sinks"])   # a table is never a sink
    text = "\n".join(map_text._dataflow(view, 40))
    assert "-> table accounts" in text and path["sink"] in text
    # a path claim's sink line: an ORM-only table read is partial support, not "shows no sink operation"
    from verinoda import entail
    from verinoda import evidence as evmod

    f, _, n = path["sink_lines"][0].rpartition(":")
    ev = evmod.source_evidence(py2, f, int(n), int(n), commit=None, meta={"sink": True})
    grade = entail._flow(py2, {"sink_kinds": path["sink_kinds"]}, ev, "", [])
    assert grade.grade == "partial" and "table" in grade.reason, grade
    hier = am.hierarchy(g)
    files = {f for s in hier["subsystems"].values() for pk in s["packages"].values() for f in pk}
    assert "db/schema.sql" not in files


def test_the_ui_follows_table_edges(py2):
    from verinoda.ui import data as uidata

    a = uidata.Atlas(py2)
    local = a.local_graph("table:accounts")
    assert any(e["relation"] in ("writes_table", "reads_table", "maps_to") for e in local["edges"])
    assert a.impact("table:accounts")["count"] >= 2
    g = index.load(py2)
    open_account = next(n for n in g.G if g.label(n).strip(".") == "open_account()")
    assert a.path(open_account, "table:accounts")["found"]


def test_the_live_database_is_read_without_writing_beside_it(py2, tmp_path, capsys):
    from verinoda import cli

    folder = tmp_path / "live"
    folder.mkdir()
    db = folder / "app.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute('CREATE TABLE "we""ird" (id INTEGER)')
    conn.commit()
    conn.close()
    before = sorted(p.name for p in folder.iterdir())
    assert before == ["app.db"]
    assert dataschema.live_sqlite(db) == {'we"ird': ["id"]}
    assert sorted(p.name for p in folder.iterdir()) == before   # no -shm / -wal left behind
    # a writer still open: committed rows in the log are read from a temporary copy
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE later (id INTEGER, note TEXT)")
    writer.commit()
    try:
        during = sorted(p.name for p in folder.iterdir())
        assert "app.db-wal" in during
        assert dataschema.live_sqlite(db)["later"] == ["id", "note"]
        assert sorted(p.name for p in folder.iterdir()) == during
    finally:
        writer.close()
    # Django: the implicit id, inherited fields and the many-to-many join table compare equal
    dj = tmp_path / "django.db"
    conn = sqlite3.connect(dj)
    conn.execute("CREATE TABLE blog_post (id INTEGER, created_at TEXT, title TEXT)")
    conn.execute("CREATE TABLE blog_post_tags (id INTEGER, post_id INTEGER, tag_id INTEGER)")
    conn.execute("CREATE TABLE blog_tag (id INTEGER, name TEXT)")
    conn.execute("CREATE TABLE unrelated (id INTEGER)")
    conn.commit()
    conn.close()
    for table in ("blog_post", "blog_post_tags", "blog_tag"):
        assert cli.main(["schema", "--repo", str(py2), "--db", str(dj), "--table", table, "--json"]) == 0, table
        live = json.loads(capsys.readouterr().out)["live"]
        assert live["columns"] == [] and live["only_in_database"] == [], live
    assert cli.main(["schema", "--repo", str(py2), "--db", str(dj), "--json"]) == 3   # the rest differs


def test_a_damaged_sidecar_entry_is_read_again(py2, capsys):
    from verinoda import cli

    g = index.load(py2, augment=False)
    files, *_ = dataschema.collect(g)
    fresh = dataschema.report(g)
    sc = index.receiver_calls_path(py2)
    side = json.loads(sc.read_text(encoding="utf-8"))
    block = side["data_schema"]
    bad = dict(block["files"])
    bad["api/db.py"] = "nope"
    bad["api/routes.py"] = {"sha256": files["api/routes.py"]["sha256"],
                            "facts": {"uses": [{"table": "x", "op": "read", "via": "SQL string"}]}}
    bad["blog/models.py"] = {"sha256": files["blog/models.py"]["sha256"], "facts": {"django": ["Post"]}}
    side["data_schema"] = {**block, "files": bad}
    sc.write_text(json.dumps(side), encoding="utf-8")
    try:
        again = dataschema.report(g)
        assert _tables(again).keys() == _tables(fresh).keys()
        assert cli.main(["schema", "--repo", str(py2)]) == 0
        assert "accounts:" in capsys.readouterr().out
    finally:
        index.refresh_receiver_sidecar(py2)


def test_review_minors(py2, spring):
    g = index.load(py2)
    t = _tables(dataschema.report(g))
    # Flask-SQLAlchemy without __tablename__
    assert t["audit_entry"]["defs"][0]["via"].startswith("Flask-SQLAlchemy default")
    assert t["audit_entry"]["columns"] == ["id", "note"]
    # SQL constants: a module constant and a class constant belong to the functions naming them
    assert ("write", "record()") in {(u["op"], u["by"]) for u in t["events"]["uses"]}
    archive = {(u["op"], u["by"]) for u in t["archive"]["uses"]}
    assert len(archive) == 1 and all(op == "write" and (by or "").endswith("purge()") for op, by in archive), archive
    # no "read by" line twice
    text = dataschema.render(dataschema.report(g))
    for line in text.splitlines():
        if "read by" in line or "written by" in line:
            items = line.split(": ", 1)[1].split("; ")
            assert len(items) == len(set(items)), line
    # Spring injections: Lombok final fields, @Autowired with @Qualifier, a Kotlin @Autowired constructor
    inj = {(i["from"].rsplit(".", 1)[-1], i["to"].rsplit(".", 1)[-1], i["via"])
           for i in dataschema.report(index.load(spring))["injections"]}
    assert ("OrderService", "OrderRepository", "Spring constructor injection (Lombok)") in inj, inj
    assert ("OrderService", "AuditDao", "Spring @Autowired") in inj, inj
    assert ("LabelService", "AuditDao", "Spring constructor injection") in inj, inj
