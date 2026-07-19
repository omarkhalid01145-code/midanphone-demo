#!/usr/bin/env python3

import gzip
import json
import os
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "phones.json"

INDEX_URL = "https://data.icecat.biz/export/freexml/EN/files.index.xml.gz"
CATEGORIES_URL = "https://data.icecat.biz/export/freexml/refs/CategoriesList.xml.gz"
SUPPLIERS_URL = "https://data.icecat.biz/export/freexml/refs/SuppliersList.xml.gz"

PHONE_NAMES = (
    "mobile phone",
    "mobile phones",
    "smartphone",
    "smartphones",
    "feature phone",
    "feature phones",
)


def required_secret(name):
    value = os.environ.get(name, "").strip()

    if not value:
        raise RuntimeError("Missing GitHub secret: " + name)

    return value


def tag_name(tag):
    return tag.rsplit("}", 1)[-1].lower()


def get_attribute(element, *names):
    attributes = {
        key.lower(): value
        for key, value in element.attrib.items()
    }

    for name in names:
        value = attributes.get(name.lower())

        if value:
            return value.strip()

    return ""


def download(url, destination, headers):
    request = urllib.request.Request(url, headers=headers)

    with urllib.request.urlopen(request, timeout=180) as response:
        with open(destination, "wb") as output:
            while True:
                chunk = response.read(1024 * 1024)

                if not chunk:
                    break

                output.write(chunk)


def find_phone_categories(categories_file):
    categories = {}

    with gzip.open(categories_file, "rb") as source:
        for event, element in ET.iterparse(source, events=("end",)):
            if tag_name(element.tag) != "category":
                continue

            category_id = get_attribute(
                element,
                "ID",
                "Category_ID"
            )

            parent_id = get_attribute(
                element,
                "ParentID",
                "Parent_ID"
            )

            category_names = []

            for child in element.iter():
                if tag_name(child.tag) in ("name", "categoryname"):
                    value = get_attribute(child, "Value")

                    if not value and child.text:
                        value = child.text.strip()

                    if value:
                        category_names.append(value)

            name = " ".join(category_names)

            if category_id:
                categories[category_id] = {
                    "name": name,
                    "parent": parent_id,
                }

            element.clear()

    selected = set()

    for category_id, information in categories.items():
        category_name = information["name"].lower()

        if any(name in category_name for name in PHONE_NAMES):
            selected.add(category_id)

    changed = True

    while changed:
        old_count = len(selected)

        for category_id, information in categories.items():
            if information["parent"] in selected:
                selected.add(category_id)

        changed = len(selected) != old_count

    if not selected:
        raise RuntimeError("Phone categories were not found in Icecat")

    return selected


def read_suppliers(suppliers_file):
    suppliers = {}

    with gzip.open(suppliers_file, "rb") as source:
        for event, element in ET.iterparse(source, events=("end",)):
            if tag_name(element.tag) != "supplier":
                continue

            supplier_id = get_attribute(
                element,
                "ID",
                "Supplier_ID"
            )

            supplier_name = get_attribute(
                element,
                "Name"
            )

            if supplier_id and supplier_name:
                suppliers[supplier_id] = supplier_name

            element.clear()

    return suppliers


def create_catalog(index_file, categories_file, suppliers_file):
    phone_categories = find_phone_categories(categories_file)
    supplier_names = read_suppliers(suppliers_file)

    phones = []
    used_products = set()

    with gzip.open(index_file, "rb") as source:
        for event, element in ET.iterparse(source, events=("end",)):
            if tag_name(element.tag) != "file":
                continue

            category_id = get_attribute(
                element,
                "Catid",
                "Category_ID"
            )

            if category_id not in phone_categories:
                element.clear()
                continue

            icecat_id = get_attribute(
                element,
                "Product_ID",
                "ID"
            )

            supplier_id = get_attribute(
                element,
                "Supplier_id",
                "Supplier_ID"
            )

            brand = get_attribute(
                element,
                "Supplier_Name"
            )

            if not brand:
                brand = supplier_names.get(supplier_id, "Unknown")

            model = get_attribute(
                element,
                "Model_Name",
                "Prod_ID",
                "ProductCode"
            )

            unique_key = icecat_id or brand + ":" + model

            if not model or unique_key in used_products:
                element.clear()
                continue

            used_products.add(unique_key)

            views_text = get_attribute(
                element,
                "Product_View",
                "ViewCount"
            )

            try:
                views = int(views_text or "0")
            except ValueError:
                views = 0

            market_value = get_attribute(
                element,
                "On_Market"
            ).lower()

            phones.append({
                "icecatId": icecat_id,
                "brand": brand,
                "name": model,
                "productCode": get_attribute(
                    element,
                    "Prod_ID",
                    "ProductCode"
                ),
                "categoryId": category_id,
                "image": get_attribute(
                    element,
                    "HighPic",
                    "Pic500x500",
                    "LowPic"
                ),
                "onMarket": market_value in (
                    "1",
                    "true",
                    "yes"
                ),
                "updated": get_attribute(
                    element,
                    "Updated"
                ),
                "views": views,
            })

            element.clear()

    phones.sort(
        key=lambda phone: (
            not phone["onMarket"],
            -phone["views"],
            phone["brand"].lower(),
            phone["name"].lower(),
        )
    )

    maximum = int(os.environ.get("MAX_PRODUCTS", "10000"))

    if maximum > 0:
        phones = phones[:maximum]

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "source": "Open Icecat",
        "count": len(phones),
        "phones": phones,
    }


def main():
    username = required_secret("ICECAT_USERNAME")
    api_token = required_secret("ICECAT_API_TOKEN")
    content_token = required_secret("ICECAT_CONTENT_TOKEN")

    headers = {
        "User-Agent": "MidanPhone/1.0 (" + username + ")",
        "api-token": api_token,
        "content-token": content_token,
        "Accept": "application/xml,application/gzip,*/*",
    }

    with tempfile.TemporaryDirectory() as temporary_folder:
        temporary_folder = Path(temporary_folder)

        index_file = temporary_folder / "index.xml.gz"
        categories_file = temporary_folder / "categories.xml.gz"
        suppliers_file = temporary_folder / "suppliers.xml.gz"

        download(INDEX_URL, index_file, headers)
        download(CATEGORIES_URL, categories_file, headers)
        download(SUPPLIERS_URL, suppliers_file, headers)

        catalog = create_catalog(
            index_file,
            categories_file,
            suppliers_file
        )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    OUTPUT.write_text(
        json.dumps(
            catalog,
            ensure_ascii=False,
            separators=(",", ":")
        ),
        encoding="utf-8"
    )

    print(
        "Successfully created catalog with",
        catalog["count"],
        "phones"
    )


if __name__ == "__main__":
    main()
