from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.main import _flatten_taxonomy
from app.models import Base, Product, _ensure_schema


def test_ensure_schema_adds_missing_columns_to_stale_database():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        session.add(Product(name="stale row", price="9.99", quantity=1))
        session.commit()
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE product DROP COLUMN variations"))
        conn.execute(text("ALTER TABLE product DROP COLUMN variants"))

    _ensure_schema(engine)

    with engine.begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(product)"))}
        row = conn.execute(text("SELECT name, variations, variants FROM product")).fetchone()
    assert {"variations", "variants"} <= columns
    assert row == ("stale row", None, None)


def test_ensure_schema_is_idempotent():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    _ensure_schema(engine)
    _ensure_schema(engine)

    with engine.begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(product)"))}
    assert "variations" in columns
    assert "variants" in columns


def test_flatten_taxonomy_explodes_nested_tree():
    tree = [
        {
            "id": 1,
            "name": "Accessories",
            "children": [
                {
                    "id": 2172,
                    "name": "Belts",
                    "children": [{"id": 3, "name": "Buckle", "children": []}],
                }
            ],
        }
    ]
    flat = _flatten_taxonomy(tree)
    assert [(n["node_id"], n["full_path_taxonomy_paths"][0]) for n in flat] == [
        (1, ["Accessories"]),
        (2172, ["Accessories", "Belts"]),
        (3, ["Accessories", "Belts", "Buckle"]),
    ]
