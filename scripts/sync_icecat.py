#!/usr/bin/env python3
"""Build the public MidanPhone phone catalogue from the Open Icecat index.

Secrets are read only from GitHub Actions environment variables and are never
written to the generated JSON file.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import tempfile
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
import re
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "phones.json"
INDEX_URL = "https://data.icecat.biz/export/freexml/EN/files.index.xml.gz"
CATEGORIES_URL = "https://data.icecat.biz/export/freexml/refs/CategoriesList.xml.gz"
SUPPLIERS_URL = "https://data.icecat.biz/export/freexml/refs/SuppliersList.xml.gz"
PHONE_TERMS = (
    "mobile phone", "mobile phones", "smartphone", "smartphones",
    "feature phone", "feature phones",
)
API_URL = "https://live.icecat.biz/api/"
ASSET_DIR = ROOT / "assets" / "phones"


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def attr(node: ET.Element, *names: str) -> str:
    lowered = {k.lower(): v for k, v in node.attrib.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()].strip()
    return ""


def download(url: str, destination: Path, headers: dict[str, str]) -> None:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=180) as response, destination.open("wb") as target:
        while chunk := response.read(1024 * 1024):
            target.write(chunk)


def xml_events(gz_path: Path, wanted: str):
    with gzip.open(gz_path, "rb") as source:
        for _, node in ET.iterparse(source, events=("end",)):
            if local_name(node.tag).lower() == wanted.lower():
                yield node
                node.clear()


def category_ids(path: Path) -> set[str]:
    override = os.environ.get("ICECAT_PHONE_CATEGORY_IDS", "").strip()
    if override:
        return {item.strip() for item in override.split(",") if item.strip()}

    categories: dict[str, tuple[str, str]] = {}
    for node in xml_events(path, "Category"):
        category_id = attr(node, "ID", "Category_ID")
        parent_id = attr(node, "ParentID", "Parent_ID")
        names = []
        for child in node.iter():
            if local_name(child.tag).lower() in {"name", "categoryname"}:
                lang = attr(child, "langid", "lang", "lang_id")
                if not lang or lang.lower() in {"1", "en"}:
                    value = attr(child, "value") or (child.text or "").strip()
                    if value:
                        names.append(value)
        name = " ".join(names) or attr(node, "Name")
        if category_id:
            categories[category_id] = (name, parent_id)

    selected = {
        cid for cid, (name, _) in categories.items()
        if any(term in name.lower() for term in PHONE_TERMS)
    }
    changed = True
    while changed:
        before = len(selected)
        selected.update(cid for cid, (_, parent) in categories.items() if parent in selected)
        changed = len(selected) != before
    if not selected:
        raise SystemExit("Could not discover phone categories. Set ICECAT_PHONE_CATEGORY_IDS manually.")
    return selected


def suppliers(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for node in xml_events(path, "Supplier"):
        supplier_id = attr(node, "ID", "Supplier_ID")
        name = attr(node, "Name")
        if supplier_id and name:
            result[supplier_id] = name
    return result


def build(index_path: Path, category_path: Path, supplier_path: Path) -> dict:
    phone_categories = category_ids(category_path)
    supplier_names = suppliers(supplier_path)
    phones: list[dict] = []
    seen: set[str] = set()

    for node in xml_events(index_path, "file"):
        category_id = attr(node, "Catid", "Category_ID")
        if category_id not in phone_categories:
            continue
        icecat_id = attr(node, "Product_ID", "ID")
        brand = attr(node, "Supplier_Name") or supplier_names.get(attr(node, "Supplier_id", "Supplier_ID"), "")
        model = attr(node, "Model_Name", "Prod_ID", "ProductCode")
        key = icecat_id or f"{brand}:{model}"
        if not model or key in seen:
            continue
        seen.add(key)
        phones.append({
            "icecatId": icecat_id,
            "brand": brand or "Unknown",
            "name": model,
            "productCode": attr(node, "Prod_ID", "ProductCode"),
            "categoryId": category_id,
            "image": attr(node, "HighPic", "Pic500x500", "LowPic"),
            "onMarket": attr(node, "On_Market").lower() in {"1", "true", "yes"},
            "updated": attr(node, "Updated"),
            "views": int(attr(node, "Product_View", "ViewCount") or 0),
        })

    phones.sort(key=lambda p: (not p["onMarket"], -p["views"], p["brand"].lower(), p["name"].lower()))
    maximum = int(os.environ.get("MAX_PRODUCTS", "10000"))
    if maximum > 0:
        phones = phones[:maximum]
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "source": "Open Icecat",
        "licenseNote": "Product data and images are supplied AS IS by Icecat and their respective rights holders.",
        "count": len(phones),
        "phones": phones,
    }


def text_value(value) -> str:
    if isinstance(value, dict):
        value = value.get("Value") or value.get("_") or ""
    return str(value or "").strip()


def clean_html(value) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", text_value(value)))).strip()


def feature_map(data: dict) -> dict[str, str]:
    result: dict[str, str] = {}
    for group in data.get("FeaturesGroups", []) or []:
        for item in group.get("Features", []) or []:
            name = text_value((item.get("Feature") or {}).get("Name")).lower()
            value = text_value(item.get("PresentationValue") or item.get("LocalValue") or item.get("Value"))
            if name and value:
                result[name] = value
    return result


def first_feature(features: dict[str, str], *needles: str) -> str:
    for name, value in features.items():
        if any(needle in name for needle in needles):
            return value
    return ""


def fetch_json(url: str, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


def download_image(url: str, destination: Path, headers: dict[str, str]) -> bool:
    if not url or destination.exists():
        return destination.exists()
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=45) as response:
            content_type = response.headers.get("Content-Type", "")
            if not content_type.startswith("image/"):
                return False
            content = response.read(8 * 1024 * 1024 + 1)
            if not content or len(content) > 8 * 1024 * 1024:
                return False
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            return True
    except Exception:
        return False


def enrich_phone(phone: dict, username: str, headers: dict[str, str]) -> dict:
    query = urllib.parse.urlencode({
        "lang": "en", "shopname": username, "icecat_id": phone["icecatId"],
        "content": "essentialinfo,gallery,featuregroups,marketingtext,reasonstobuy",
    })
    payload = fetch_json(f"{API_URL}?{query}", headers)
    data = payload.get("data") or payload.get("Data") or {}
    if not data or str(payload.get("StatusCode", payload.get("statusCode", "0"))) not in {"0", "1"}:
        raise RuntimeError(payload.get("Message") or payload.get("message") or "No product data")
    info = data.get("GeneralInfo") or {}
    features = feature_map(data)
    image = data.get("Image") or {}
    gallery = data.get("Gallery") or []
    image_url = image.get("Pic500x500") or image.get("HighPic") or image.get("Pic") or ""
    if not image_url and gallery:
        image_url = gallery[0].get("Pic500x500") or gallery[0].get("Pic") or ""
    local_image = ASSET_DIR / f"{phone['icecatId']}.jpg"
    if download_image(image_url, local_image, headers):
        phone["image"] = f"assets/phones/{phone['icecatId']}.jpg"
    description = info.get("Description") or {}
    phone.update({
        "name": info.get("ProductName") or phone["name"],
        "screen": first_feature(features, "display diagonal", "screen size", "display size"),
        "chip": first_feature(features, "processor model", "processor family"),
        "camera": first_feature(features, "rear camera resolution", "main camera resolution"),
        "battery": first_feature(features, "battery capacity"),
        "ram": first_feature(features, "ram capacity", "internal memory"),
        "storage": first_feature(features, "internal storage capacity"),
        "os": first_feature(features, "operating system installed", "mobile operating system"),
        "description": clean_html(description.get("LongDesc") or description.get("ShortDesc") or info.get("SummaryDescription")),
        "enriched": True,
    })
    return phone


def carry_existing(payload: dict) -> None:
    if not OUT.exists():
        return
    try:
        old = json.loads(OUT.read_text(encoding="utf-8"))
        known = {str(p.get("icecatId")): p for p in old.get("phones", []) if p.get("enriched")}
        for phone in payload["phones"]:
            previous = known.get(str(phone.get("icecatId")))
            if previous:
                phone.update({k: v for k, v in previous.items() if k not in {"onMarket", "updated", "views"}})
    except (OSError, ValueError):
        pass


def enrich_batch(payload: dict, username: str, headers: dict[str, str]) -> None:
    carry_existing(payload)
    limit = int(os.environ.get("ENRICH_PRODUCTS_PER_RUN", "120"))
    targets = [p for p in payload["phones"] if p.get("icecatId") and not p.get("enriched")][:limit]
    completed = 0
    with ThreadPoolExecutor(max_workers=12) as pool:
        jobs = {pool.submit(enrich_phone, phone, username, headers): phone for phone in targets}
        for job in as_completed(jobs):
            try:
                job.result()
                completed += 1
            except Exception as exc:
                jobs[job]["enrichmentError"] = str(exc)[:160]
    payload["enrichedCount"] = sum(1 for p in payload["phones"] if p.get("enriched"))
    print(f"Enriched {completed} new phones; {payload['enrichedCount']} total")


def main() -> None:
    username = env("ICECAT_USERNAME")
    api_token = env("ICECAT_API_TOKEN")
    content_token = env("ICECAT_CONTENT_TOKEN")
    headers = {
        "User-Agent": f"MidanPhone/1.0 ({username})",
        "api-token": api_token,
        "content-token": content_token,
        "Accept": "application/xml,application/gzip,*/*",
    }
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        index_path, categories_path, suppliers_path = work / "index.xml.gz", work / "categories.xml.gz", work / "suppliers.xml.gz"
        download(INDEX_URL, index_path, headers)
        download(CATEGORIES_URL, categories_path, headers)
        download(SUPPLIERS_URL, suppliers_path, headers)
        payload = build(index_path, categories_path, suppliers_path)
        enrich_batch(payload, username, headers)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {payload['count']} phones to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Icecat sync failed: {exc}", file=sys.stderr)
        raise
