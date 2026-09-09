
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.etsy.client import EtsyError
from app.models import Base, Product, ShopProfile, SubmissionLog
from app.services.listing_builder import (
    ListingValidationError,
    build_create_params,
    build_inventory_payload,
    build_variant_matrix,
    submit_listing,
    validate_listing_inputs,
)


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        yield s
        s.rollback()


def populated_product(**overrides) -> Product:
    defaults = dict(
        name="Ceramic Mug",
        title="Hand-thrown ceramic mug, glazed",
        description="<p>A lovely mug.</p>",
        price="29.99",
        quantity=5,
        taxonomy_id=68891037,
        taxonomy_path="Home & Living > Kitchen",
        listing_type="physical",
        when_made="made_to_order",
        who_made="i_did",
        tags=["mug", "ceramic"],
        materials=["clay", "glaze"],
    )
    defaults.update(overrides)
    return Product(**defaults)


def populated_shop() -> ShopProfile:
    return ShopProfile(
        shop_id=123,
        shop_name="Test Shop",
        shipping_profile_id=111,
        readiness_state_id=222,
    )


class FakeClient:
    def __init__(self):
        self.calls = []
        self.listing_id = 4242

    async def create_draft_listing(self, shop_id, **params):
        self.calls.append(("create", shop_id, params))
        return {"listing_id": self.listing_id}

    async def upload_listing_image(self, shop_id, listing_id, image_bytes, filename, rank=None):
        self.calls.append(("image", listing_id, filename, rank))

    async def upload_listing_file(self, shop_id, listing_id, file_bytes, filename):
        self.calls.append(("file", listing_id, filename))

    async def update_listing(self, shop_id, listing_id, **params):
        self.calls.append(("update", listing_id, params))

    async def update_listing_inventory(self, listing_id, payload, max_variations_supported="2"):
        self.calls.append(("inventory", listing_id, payload))


def variation_product(**overrides) -> Product:
    product = populated_product()
    product.variations = [
        {
            "property_id": 513,
            "property_name": "Colour",
            "scale_id": None,
            "values": ["Red", "Blue"],
        }
    ]
    product.variants = [
        {"sku": "MUG-RED", "price": "29.99", "quantity": 5, "value_labels": ["Red"]},
        {"sku": "MUG-BLUE", "price": "29.99", "quantity": 5, "value_labels": ["Blue"]},
    ]
    for key, value in overrides.items():
        setattr(product, key, value)
    return product


def test_build_create_params_physical():
    params = build_create_params(populated_product(), populated_shop())
    assert params["price"] == 2999
    assert params["quantity"] == 5
    assert params["who_made"] == "i_did"
    assert params["when_made"] == "made_to_order"
    assert params["is_supply"] == 0
    assert params["tags"] == "mug,ceramic"
    assert params["materials"] == "clay,glaze"
    assert params["shipping_profile_id"] == 111
    assert params["readiness_state_id"] == 222


def test_build_create_params_digital_no_profiles():
    params = build_create_params(populated_product(listing_type="download"), populated_shop())
    assert "shipping_profile_id" not in params
    assert "readiness_state_id" not in params


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"name": "  "}, "Product name"),
        ({"title": ""}, "title"),
        ({"description": ""}, "description"),
        ({"taxonomy_id": None}, "Category"),
        ({"price": "nope"}, "price"),
        ({"quantity": 0}, "Quantity"),
    ],
)
def test_validate_listing_inputs_rejects(overrides, message):
    with pytest.raises(ListingValidationError, match=message):
        validate_listing_inputs(populated_product(**overrides), populated_shop())


def test_validate_listing_inputs_physical_requires_profiles():
    shop = populated_shop()
    shop.readiness_state_id = None
    with pytest.raises(ListingValidationError, match="shipping profile"):
        validate_listing_inputs(populated_product(), shop)


@pytest.mark.asyncio
async def test_submit_listing_full_pipeline(tmp_path, session):
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"fake-image-bytes")

    product = populated_product()
    product.images = [image.name]
    shop = populated_shop()
    client = FakeClient()

    result = await submit_listing(product, shop, client, session, tmp_path)

    names = [c[0] for c in client.calls]
    assert names == ["create", "image", "update"]
    assert result.status == "published"
    assert result.etsy_listing_id == 4242

    image_call = client.calls[1]
    assert image_call[2] == "photo.jpg"
    assert image_call[3] == 1

    update_call = client.calls[2][2]
    assert update_call["state"] == "active"

    log_steps = [log.step for log in session.query(SubmissionLog).all()]
    assert "created" in log_steps
    assert "publish" in log_steps


@pytest.mark.asyncio
async def test_submit_listing_skips_images_missing(tmp_path, session):
    product = populated_product()
    product.images = ["does-not-exist.jpg"]
    product.tags = []
    with pytest.raises(FileNotFoundError):
        await submit_listing(product, populated_shop(), FakeClient(), session, tmp_path)


@pytest.mark.asyncio
async def test_submit_listing_digital_flow(tmp_path, session):
    digifile = tmp_path / "pattern.pdf"
    digifile.write_bytes(b"pdf-bytes")
    product = populated_product(listing_type="download")
    product.digital_file = digifile.name
    client = FakeClient()

    await submit_listing(product, populated_shop(), client, session, tmp_path)

    names = [c[0] for c in client.calls]
    assert names == ["create", "file", "update", "update"]
    assert client.calls[1][1] == 4242
    assert client.calls[1][2] == "pattern.pdf"
    assert client.calls[2][2] == {"type": "download"}
    assert client.calls[3][2] == {"state": "active"}


@pytest.mark.asyncio
async def test_submit_listing_create_failure_propagates(tmp_path, session):
    class FailingClient(FakeClient):
        async def create_draft_listing(self, shop_id, **params):
            raise EtsyError("boom", status=500)

    product = populated_product()
    product.images = []
    with pytest.raises(EtsyError):
        await submit_listing(product, populated_shop(), FailingClient(), session, tmp_path)
    assert product.status == "submitting"


def test_build_inventory_payload_single_dimension_no_overrides():
    payload = build_inventory_payload(variation_product(), populated_shop())
    assert [p["sku"] for p in payload["products"]] == ["MUG-RED", "MUG-BLUE"]
    assert payload["products"][0]["property_values"] == [
        {
            "property_id": 513,
            "property_name": "Colour",
            "value_ids": [],
            "values": ["Red"],
        }
    ]
    assert payload["products"][0]["offerings"] == [
        {"price": 29.99, "quantity": 5, "is_enabled": True, "readiness_state_id": 222}
    ]
    assert "price_on_property" not in payload
    assert "quantity_on_property" not in payload


def test_build_inventory_payload_money_is_dollars_float():
    product = variation_product()
    product.price = "10.99"
    product.variants[0]["price"] = "10.99"
    product.variants[1]["price"] = "10.99"
    payload = build_inventory_payload(product, populated_shop())
    for p in payload["products"]:
        assert p["offerings"][0]["price"] == 10.99
        assert p["offerings"][0]["price"] != 1099


def test_build_inventory_payload_two_dimensions_emits_on_property():
    product = variation_product(
        variations=[
            {
                "property_id": 513,
                "property_name": "Colour",
                "scale_id": None,
                "values": ["Red", "Blue"],
            },
            {
                "property_id": 514,
                "property_name": "Size",
                "scale_id": None,
                "values": ["S", "L"],
            },
        ],
        variants=[
            {"sku": "A", "price": "29.99", "quantity": 5, "value_labels": ["Red", "S"]},
            {"sku": "B", "price": "30.00", "quantity": 5, "value_labels": ["Red", "L"]},
            {"sku": "C", "price": "29.99", "quantity": 6, "value_labels": ["Blue", "S"]},
            {"sku": "D", "price": "29.99", "quantity": 5, "value_labels": ["Blue", "L"]},
        ],
    )
    payload = build_inventory_payload(product, populated_shop())
    assert len(payload["products"]) == 4
    assert payload["price_on_property"] == [513, 514]
    assert payload["quantity_on_property"] == [513, 514]


def test_build_inventory_payload_auto_sku_and_scale_id():
    product = variation_product(
        variations=[
            {"property_id": 513, "property_name": "Size", "scale_id": 19, "values": ["M"]}
        ],
        variants=[{"sku": "", "price": "29.99", "quantity": 5, "value_labels": ["M"]}],
    )
    payload = build_inventory_payload(product, populated_shop())
    assert payload["products"][0]["sku"] == "CERAMIC-MUG-M"
    assert payload["products"][0]["property_values"][0]["scale_id"] == 19


@pytest.mark.parametrize(
    ("variations", "message"),
    [
        (
            [
                {"property_id": 513, "property_name": "Colour", "values": ["Red (dark)"]},
            ],
            "parentheses",
        ),
        (
            [
                {"property_id": 513, "property_name": "A", "values": ["1"]},
                {"property_id": 514, "property_name": "B", "values": ["2"]},
                {"property_id": 515, "property_name": "C", "values": ["3"]},
            ],
            "1-2 dimensions",
        ),
        (
            [
                {"property_id": 513, "property_name": "A", "values": [str(i) for i in range(100)]},
                {"property_id": 514, "property_name": "B", "values": [str(i) for i in range(100)]},
            ],
            "cap",
        ),
    ],
)
def test_build_inventory_payload_rejects_bad_variations(variations, message):
    with pytest.raises(ListingValidationError, match=message):
        build_inventory_payload(
            variation_product(variations=variations, variants=[]), populated_shop()
        )


def test_build_inventory_payload_rejects_duplicate_sku():
    product = variation_product(
        variants=[
            {"sku": "DUP", "price": "29.99", "quantity": 5, "value_labels": ["Red"]},
            {"sku": "DUP", "price": "29.99", "quantity": 5, "value_labels": ["Blue"]},
        ]
    )
    with pytest.raises(ListingValidationError, match="Duplicate SKU"):
        build_inventory_payload(product, populated_shop())


def test_build_inventory_payload_rejects_unknown_value():
    product = variation_product(
        variants=[
            {"sku": "A", "price": "29.99", "quantity": 5, "value_labels": ["Red"]},
            {"sku": "B", "price": "29.99", "quantity": 5, "value_labels": ["Pink"]},
        ]
    )
    with pytest.raises(ListingValidationError, match="not part of variation"):
        build_inventory_payload(product, populated_shop())


def test_build_variant_matrix_cartesian():
    variations = [
        {"property_id": 513, "property_name": "Colour", "values": ["Red", "Blue"]},
        {"property_id": 514, "property_name": "Size", "values": ["S", "L"]},
    ]
    matrix = build_variant_matrix(variations, "Ceramic Mug", "24.50", 2)
    assert len(matrix) == 4
    assert matrix[0] == {
        "sku": "CERAMIC-MUG-RED-S",
        "price": "24.50",
        "quantity": 2,
        "value_labels": ["Red", "S"],
    }
    assert [m["value_labels"] for m in matrix] == [
        ["Red", "S"],
        ["Red", "L"],
        ["Blue", "S"],
        ["Blue", "L"],
    ]


@pytest.mark.asyncio
async def test_submit_listing_includes_inventory_step(tmp_path, session):
    product = variation_product(images=[])
    client = FakeClient()

    await submit_listing(product, populated_shop(), client, session, tmp_path)

    names = [c[0] for c in client.calls]
    assert names == ["create", "inventory", "update"]
    assert client.calls[1][2]["products"][0]["offerings"][0]["price"] == 29.99
    inventory_call = client.calls[1][1]
    assert inventory_call == 4242


@pytest.mark.asyncio
async def test_submit_listing_skips_inventory_without_variations(tmp_path, session):
    product = populated_product(images=[])
    product.variations = []
    product.variants = []
    client = FakeClient()

    await submit_listing(product, populated_shop(), client, session, tmp_path)

    names = [c[0] for c in client.calls]
    assert "inventory" not in names
    assert names == ["create", "update"]


def test_validate_listing_inputs_rejects_bad_variations():
    product = variation_product(
        variations=[
            {"property_id": 513, "property_name": "Colour", "values": ["bad (paren)"]},
        ]
    )
    with pytest.raises(ListingValidationError, match="parentheses"):
        validate_listing_inputs(product, populated_shop())
