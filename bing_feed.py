#!/usr/bin/env python3
"""
NOAVVO - Microsoft (Bing) Shopping feed generator
==================================================
Pulls all ACTIVE, published products from Shopify and writes a
Google-spec-compatible TSV feed (feed.txt) that Microsoft Merchant
Center can fetch on a schedule.

Env vars required:
  SHOPIFY_SHOP           e.g. k9zkug-ur.myshopify.com
  SHOPIFY_CLIENT_ID      Dev Dashboard app Client ID
  SHOPIFY_CLIENT_SECRET  Dev Dashboard app Client secret
  (or SHOPIFY_ADMIN_TOKEN, if you have a legacy admin custom-app token)
  STORE_URL              optional, default https://noavvo.com

Output: feed.txt (tab-separated, UTF-8) in the working directory.
"""

import csv
import html
import os
import re
import sys
import time

import requests

SHOP = os.environ["SHOPIFY_SHOP"]
TOKEN = os.environ.get("SHOPIFY_ADMIN_TOKEN")
CLIENT_ID = os.environ.get("SHOPIFY_CLIENT_ID")
CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET")
STORE = os.environ.get("STORE_URL", "https://noavvo.com").rstrip("/")
API_VERSION = "2025-10"
OUT_FILE = "feed.txt"

FREE_SHIPPING_THRESHOLD = 250.00   # GBP, UK zone
UK_STANDARD_RATE = "9.99"

COLOR_WORDS = {
    "black", "white", "red", "blue", "green", "pink", "purple", "beige",
    "brown", "gray", "grey", "gold", "silver", "multicolor", "orange",
    "yellow", "turquoise", "navy", "cream", "ivory", "khaki", "burgundy",
    "violet", "fuchsia", "rose gold", "black and red",
}

session = requests.Session()


def get_access_token():
    """Use a legacy token if provided, otherwise exchange Dev Dashboard
    client credentials for a fresh Admin API access token."""
    if TOKEN:
        return TOKEN
    if not (CLIENT_ID and CLIENT_SECRET):
        sys.exit("Set SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET "
                 "(or SHOPIFY_ADMIN_TOKEN).")
    resp = requests.post(
        f"https://{SHOP}/admin/oauth/access_token",
        json={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_all_products():
    """Paginate through every active product via the REST Admin API."""
    products = []
    url = f"https://{SHOP}/admin/api/{API_VERSION}/products.json"
    params = {
        "limit": 250,
        "status": "active",
        "fields": "id,title,handle,body_html,vendor,tags,variants,images,"
                  "image,published_at,options",
    }
    while True:
        resp = session.get(url, params=params, timeout=60)
        resp.raise_for_status()
        batch = resp.json().get("products", [])
        products.extend(batch)
        link = resp.headers.get("Link", "")
        nxt = re.search(r'<([^>]+)>;\s*rel="next"', link)
        if not nxt:
            break
        url, params = nxt.group(1), None
        time.sleep(0.5)  # stay well inside rate limits
    return products


def strip_html(text):
    t = re.sub(r"<[^>]+>", " ", text or "")
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def sanitize(value):
    """TSV-safe: no tabs / newlines inside fields."""
    return str(value).replace("\t", " ").replace("\n", " ").replace("\r", " ").strip()


def detect_color(tags):
    for tag in tags:
        if tag.lower() in COLOR_WORDS:
            return tag
    return ""


def size_option_index(product):
    """Return 1-based option index whose name looks like a size, else None."""
    for opt in product.get("options", []):
        if "size" in (opt.get("name") or "").lower():
            return opt.get("position")
    return None


def valid_gtin(barcode):
    b = (barcode or "").strip()
    return b if b.isdigit() and len(b) in (8, 12, 13, 14) else ""


def build_rows(products):
    rows = []
    skipped_unpublished = 0
    for p in products:
        if not p.get("published_at"):
            skipped_unpublished += 1
            continue

        tags = [t.strip() for t in (p.get("tags") or "").split(",") if t.strip()]
        color = detect_color(tags)
        description = strip_html(p.get("body_html"))[:4900]
        size_idx = size_option_index(p)

        images = p.get("images") or []
        main_image = (p.get("image") or {}).get("src") or (
            images[0]["src"] if images else ""
        )
        extra_images = ",".join(
            img["src"] for img in images[1:4] if img.get("src")
        )

        for v in p.get("variants", []):
            qty = v.get("inventory_quantity") or 0
            policy = (v.get("inventory_policy") or "deny").lower()
            if qty <= 0 and policy != "continue":
                continue  # keep the feed clean: in-stock offers only

            price = float(v.get("price") or 0)
            if price <= 0:
                continue

            shipping = (
                f"GB:::0.00 GBP"
                if price >= FREE_SHIPPING_THRESHOLD
                else f"GB:::{UK_STANDARD_RATE} GBP"
            )

            size = ""
            if size_idx:
                size = v.get(f"option{size_idx}") or ""
            elif (v.get("title") or "") not in ("", "Default Title"):
                size = v["title"]

            variant_image = ""
            if v.get("image_id"):
                for img in images:
                    if img["id"] == v["image_id"]:
                        variant_image = img["src"]
                        break

            rows.append([
                sanitize(v.get("sku") or v["id"]),                      # id
                sanitize(p["id"]),                                      # item_group_id
                sanitize(p["title"])[:150],                             # title
                sanitize(description),                                  # description
                f"{STORE}/products/{p['handle']}?variant={v['id']}",    # link
                variant_image or main_image,                            # image_link
                extra_images,                                           # additional_image_link
                f"{price:.2f} GBP",                                     # price
                "in stock",                                             # availability
                "new",                                                  # condition
                sanitize(p.get("vendor") or ""),                        # brand
                sanitize(v.get("sku") or ""),                           # mpn
                valid_gtin(v.get("barcode")),                           # gtin
                sanitize(size),                                         # size
                sanitize(color),                                        # color
                shipping,                                               # shipping
            ])

    print(f"Skipped (unpublished): {skipped_unpublished}")
    return rows


HEADER = [
    "id", "item_group_id", "title", "description", "link", "image_link",
    "additional_image_link", "price", "availability", "condition",
    "brand", "mpn", "gtin", "size", "color", "shipping",
]


def main():
    session.headers.update({"X-Shopify-Access-Token": get_access_token()})
    products = fetch_all_products()
    print(f"Fetched products: {len(products)}")
    rows = build_rows(products)
    print(f"Feed offers (variants in stock): {len(rows)}")
    if not rows:
        print("ERROR: no rows generated - aborting so we don't publish an empty feed.")
        sys.exit(1)
    with open(OUT_FILE, "w", encoding="utf-8", newline="") as f:
        f.write("\t".join(HEADER) + "\n")
        for row in rows:
            f.write("\t".join(row) + "\n")
    print(f"Wrote {OUT_FILE}")


if __name__ == "__main__":
    main()
