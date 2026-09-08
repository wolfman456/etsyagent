from app.services.csv_import import parse_csv


def test_parse_happy_path():
    result = parse_csv(
        "name,price,quantity,tags,when_made,listing_type\n"
        "Mug,19.99,3,\"mug, ceramic\",2020_2026,physical\n"
        "Pattern,5.00,1,pattern,download,download\n"
    )
    assert len(result.products) == 2
    assert result.errors == []
    first, second = result.products
    assert first.name == "Mug"
    assert first.price == "19.99"
    assert first.quantity == 3
    assert first.tags == ["mug", "ceramic"]
    assert first.when_made == "2020_2026"
    assert first.listing_type == "physical"
    assert second.listing_type == "download"


def test_missing_required_column():
    result = parse_csv("name,tags\nMug,tag\n")
    assert result.products == []
    assert result.errors[0].message.startswith("Missing required columns")


def test_missing_price_row_is_flagged():
    result = parse_csv("name,price\nMug,\n")
    assert result.products == []
    assert result.errors[0].row == 2
    assert result.errors[0].message == "missing price"


def test_empty_and_blank_rows_skipped():
    result = parse_csv("name,price\n\n\n")
    assert result.products == []
    assert result.errors == []


def test_when_made_normalization_fuzzy():
    result = parse_csv("name,price,when_made\nMug,1.00,2020\n")
    assert result.products[0].when_made == "2020_2026"


def test_quantity_defaults_and_supply_flag():
    result = parse_csv("name,price,is_supply,quantity\nLeather,9.00,yes,7\nWood,2.00,true,\n")
    assert result.products[0].quantity == 7
    assert result.products[0].is_supply is True
    assert result.products[1].quantity == 1
    assert result.products[1].is_supply is True


def test_utf8_bom_handled():
    content = "\ufeffname,price\nMug,4.00\n"
    result = parse_csv(content)
    assert result.products[0].name == "Mug"


def test_column_aliases():
    result = parse_csv("product_name,price\nMug,4.00\n")
    assert result.products[0].name == "Mug"
