# Flows test-account custom catalog

This repository hosts a public JSON product feed for Flows load-test accounts.
The initial `feed.json` contains 10,000 fake products and follows the shape of
Varun's `feed-large.json`.

Use this raw feed URL when configuring a custom catalog source:

```text
https://raw.githubusercontent.com/klaviyo/flows-test-account-custom-catalog-sync/master/feed.json
```

Each product contains:

- `id`, `title`, `description`, `link`, and HTTPS `image_link`
- `price` and `currency`
- `inventory_quantity` and `inventory_policy`
- `categories`, `brand`, `sku`, and `radius`

## Variant limitation

A custom catalog URL feed creates exactly one variant for each product row.
The generated variant uses the product `id` as its external ID and reads
`sku`, `price`, `inventory_quantity`, and `inventory_policy` from that row.

The URL feed format cannot define multiple variants under one catalog item.
Use the Catalog API when a product needs separate variants such as Small,
Medium, and Large.

## Add products

`generate_products.py` creates the feed when it does not exist or appends to
the existing feed. Each product uses a UUID-based `FEED-` ID, so the script
does not need to load or scan existing IDs. It streams the existing JSON into
an atomic replacement file, keeping memory use independent of feed size.

```bash
python3 generate_products.py --count 10000
python3 generate_products.py --count 5000
python3 generate_products.py --count 100 --feed another-feed.json
```

Use `--seed` to select a repeatable sequence of product attributes. IDs and
SKUs remain unique UUID-based values on every run.

## Update products

Choose either `price` or `inventory`, specify the maximum number of products,
and provide exactly one of `--drop-amount` or `--target-amount`.

Without `--categories`, updates start at the top of the feed:

```bash
python3 update_products.py price --count 100 --drop-amount 5.00
python3 update_products.py price --count 100 --drop-amount 20%
python3 update_products.py inventory --count 100 --target-amount 0
```

Use `--categories` to update the first products containing any listed category.
Matching is case-insensitive and checks every level of each product's category
path:

```bash
python3 update_products.py price \
  --count 50 \
  --categories Electronics "Art Supplies" \
  --drop-amount 10%

python3 update_products.py inventory \
  --count 50 \
  --categories Shoes \
  --drop-amount 5
```

Raw inventory values must be whole numbers. Percentage inventory changes are
rounded to the nearest whole unit. Price values are rounded to cents. Drops
never reduce either field below zero.

## Check a hosted feed

Install the checker dependency:

```bash
python3 -m pip install -r requirements.txt
```

Run the interactive checker:

```bash
python3 custom_catalog_checker.py
```

Enter the public raw feed URL:

```text
https://raw.githubusercontent.com/klaviyo/flows-test-account-custom-catalog-sync/refs/heads/master/feed.json
```

The checker downloads the JSON and reports good, bad, and total row counts. A
good product must contain `id`, `title`, `link`, `image_link`, and
`description`; these field names do not use a `$` prefix.

The full terminal instructions from the internal runbook are also included as
comments at the top of `custom_catalog_checker.py`.

## Create profiles

Install the dependencies and provide a private API key for the target test
account. The key must have the `profiles:write` scope.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export KLAVIYO_PRIVATE_API_KEY="$(python -c \
  'import getpass; print(getpass.getpass("Paste the private API key: "))')"
```

The virtual environment keeps these dependencies separate from other Python
projects. The API key prompt works in both zsh and bash, hides the value while
you type, and avoids saving the key in shell history.

Run the script with the number of new profiles to create as its only argument:

```bash
python3 create_profiles.py 500
```

Each profile receives a Faker-generated readable name, an email such as
`steve_smith_<uuid>@example.com`, a UUID-based external ID, and custom
properties identifying it as FP-4236 load-test data. Requests are paced at 370
per minute—below the 750/minute steady limit—and retry when the API responds
with `429` or times out. The script prints confirmed progress every 25 profiles.
If the batch stops, it reports the confirmed count; the final timed-out request
may still have reached the API.

## Create catalog events

`create_events.py` uses existing profiles and catalog products to submit
item-level events through the Bulk Create Events API. Use a private API key for
the target test account with these scopes:

- `profiles:read`
- `catalogs:read`
- `events:write`

Set up the virtual environment and `KLAVIYO_PRIVATE_API_KEY` as shown in
[Create profiles](#create-profiles), then provide one or more metric counts:

```bash
python3 create_events.py \
  --metric viewed-product=500 \
  --metric started-checkout=500
```

Supported metric arguments are:

- `viewed-product`
- `started-checkout`
- `added-to-cart`
- `ordered-product`

Repeated arguments for the same metric are added together. The script queries
only profiles where `load_test` is `true`. It uses distinct profiles and
products first, then cycles through the available pool when an event count is
larger than that pool.

Use `--overlap-count` to make part of the requested `Ordered Product` count
exclude profiles from a catalog-change audience. Each overlapping order uses
the same profile and product as one requested inclusion event:

```bash
python3 create_events.py \
  --metric viewed-product=500 \
  --metric ordered-product=100 \
  --overlap-count 100
```

The overlap count cannot exceed either the combined inclusion-event count or
the `Ordered Product` count. It does not create extra events; every metric's
requested count remains exact.

The current script does not create any variant-level events. All generated
events contain item-level product IDs but no variant identifier. This is
sufficient for load testing Price Drop flows because Price Drop supports only
item-level products. Load testing variant-level Low Inventory flows will
require additional implementation.

The event property paths mirror Jomashop's Price Drop overrides:

- Viewed Product: top-level `ProductID` and `Price`
- Ordered Product: top-level `ProductID`
- Started Checkout: an `Items` list containing `ProductID` and `ItemPrice`
- Added to Cart: top-level `ProductID` and `Price` until a Jomashop-specific
  schema is available

The script reads `external_id` and current `price` from the Catalog Variants
API. It assumes each item and its single generated variant share the same
external ID, as they do for this repository's URL feed. Verify this before
using another company's catalog: a feed with independently identified
variants requires an explicit variant-to-parent-item mapping.

The API accepts up to 1,000 events or 5 MB per job. The script splits larger
runs, stays below the steady rate limit, retries transient failures, and prints
accepted progress. An accepted job is queued for asynchronous processing; it
does not mean every event is immediately visible in Klaviyo.

## Test

Run the standard-library test suite:

```bash
python3 -m unittest discover -s tests
```
