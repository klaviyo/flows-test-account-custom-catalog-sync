# How to prep a test account for Price Drop/Low Inventory flow load test

This guide explains how to set up an account for a catalog-change flow load
test with a custom catalog integration. Use the scripts in this repository to
create profiles, products, and events. You can also change product prices or
inventory to trigger a load test.

`feed.json` is the catalog source for the test account. Scripts that add or
update products modify this file.

## Set up API access

Before running `create_profiles.py` or `create_events.py`, obtain a private API
key from the target test account. Create a virtual environment, install the
dependencies, and save the key in the current terminal session:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export KLAVIYO_PRIVATE_API_KEY="$(python -c \
  'import getpass; print(getpass.getpass("Paste the private API key: "))')"
```

The prompt hides the key and keeps it out of shell history. The exported value
exists only in the current terminal session, so repeat the `source` and
`export` commands after opening a new terminal. Feed generation, feed updates,
and local feed validation do not require an API key.

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

**Important:** `generate_products.py` and `update_products.py` only modify
`feed.json`. Merge a PR that contains the updated feed. Then refresh the
catalog source sync to apply the changes to the test account.

<img width="1978" height="958" alt="image" src="https://github.com/user-attachments/assets/85cd5393-1d45-495f-985f-68283f3b5613" />


## Syncing a custom catalog feed

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

The catalog source configures the field mappings automatically. Update the
field mappings if necessary.

### Variant limitation

A custom catalog URL feed creates exactly one variant for each product row.
The generated variant uses the product `id` as its external ID and reads
`sku`, `price`, `inventory_quantity`, and `inventory_policy` from that row.

The URL feed format cannot define multiple variants under one catalog item.
Use the Catalog API if you want to create products with multiple variants.

### Validate the format of a hosted feed

Usually, you do not need this step. Use it to validate the catalog feed format.

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

## Enable Athena audience building for a custom catalog

Custom catalog load tests require additional configuration before Athena can
build the audience:

1. Configure the catalog-change metric override on Staffside for the test
   account. Its product ID, price, and product-list paths must match the event
   schema submitted by the integration.
2. Ensure those field paths are supported by the
   `flows_catalog_change_audience_building_events` materialized-views job
   mapping. The current event generator uses the qualified Jomashop-compatible
   paths already supported by that mapping. If the generated event schema
   changes, update and deploy the materialized-views mapping before running the
   load test.
3. Add the load-test company ID to the
   `flows_ccp_custom_catalog_athena_enabled` Statsig gate.

Do not start the Price Drop load test until all three requirements are
confirmed. Otherwise, Athena can omit the custom events or the account can use
the wrong audience-building path.

## Create profiles

Complete [Set up API access](#set-up-api-access) with a key that has the
`profiles:write` scope.

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
[Set up API access](#set-up-api-access), then provide one or more metric counts:

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

By default, the payload omits `time`, so Klaviyo assigns ingestion time. Use
`--days` to distribute explicit UTC timestamps uniformly across a historical
window. For example, 30,000 events over 30 days produces approximately 1,000
events in each consecutive 24-hour period:

```bash
python3 create_events.py --metric viewed-product=30000 --days 30
```

Use `--profiles` and `--products` to limit the resource pools. The script
fetches only the first requested number of eligible profiles and priced
products, then cycles within those subsets until it creates every requested
event:

```bash
python3 create_events.py \
  --metric ordered-product=600000 \
  --profiles 500 \
  --products 10000
```

In this example, all 600,000 events use only the first 500 load-test profiles
and first 10,000 priced products. Other profiles and products are not used.
If a requested pool size exceeds what the account contains, the script uses
all available resources in that pool.

Repeated arguments for the same metric are added together. The script queries
only profiles where `load_test` is `true`. It uses distinct profiles and
products first, then cycles through the available pool when an event count is
larger than that pool. Every event receives a unique UUID, so Events ingestion
keeps repeated profile/metric combinations distinct even when they have the
same second-level timestamp.

For example, when creating 10,000 events with 500 eligible profiles and 10,000
products, profiles and products cycle independently:

```text
Event 1     -> Profile 1,   Product 1
Event 500   -> Profile 500, Product 500
Event 501   -> Profile 1,   Product 501
Event 1000  -> Profile 500, Product 1000
Event 10000 -> Profile 500, Product 10000
```

Each product is used once, while each profile receives 20 events. With
multiple metrics, the assignment index continues across metric groups.

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

Requested event counts are the script's highest priority. `--overlap-count`
controls how many Ordered Product events intentionally reuse an inclusion
profile/product pair, but it is guidance rather than a maximum. If there are
not enough distinct profiles or products, the script cycles through them and
continues creating all requested events. This can produce more overlap than
the supplied value.

The current script does not create any variant-level events. All generated
events contain item-level product IDs but no variant identifier. This is
sufficient for load testing Price Drop flows because Price Drop supports only
item-level products. Load testing variant-level Low Inventory flows will
require additional implementation.

The event property paths mirror Jomashop's Price Drop overrides:

- Viewed Product: top-level `ProductID` and `Price`
- Ordered Product: top-level `ProductID` and `Price`; Price Drop exclusion
  matching only needs `ProductID`, but retaining `Price` keeps the event complete
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

## Run a Price Drop load test

Use this sequence as the standard Price Drop load-test procedure. Existing
test data can be reused, so first decide the target scale and skip any
preparation step whose requirement is already satisfied.

1. Ensure the feed contains the target number of products. For example, add
   50,000 products to an existing 10,000-product feed:

   ```bash
   python3 generate_products.py --count 50000
   ```

2. Merge the updated feed and refresh the custom catalog source. This baseline
   sync must finish before prices change, especially for newly added products.
   Confirm that the account contains the expected total number of products.

3. Ensure the account has enough load-test profiles. For example, add 2,000:

   ```bash
   python3 create_profiles.py 2000
   ```

4. Ensure every product has an inclusion event within the flow's lookback
   window. To cover 60,000 discovered products exactly once, create 60,000
   Viewed Product events:

   ```bash
   python3 create_events.py --metric viewed-product=60000 --days 30
   ```

   Confirm that the script discovers 60,000 products before approving the
   submission.

5. Wait until the events are visible on Athena and have aged beyond the
   EventLog fallback window. Do not continue until both conditions are true.

6. Shortly before changing prices, create a small recent-event population:

   ```bash
   python3 create_events.py --metric viewed-product=100
   ```

   Wait until the accepted jobs finish processing and confirm that the recent
   events are visible in the account before changing prices.

   These events exercise the EventLog fallback while the older events exercise
   Athena. Some recent events may reach Athena before their parent executes;
   asynchronous ingestion means the script cannot guarantee that they remain
   exclusive to EventLog.

7. Drop every product's price and merge the updated feed. For a 60,000-product
   feed and a $1 drop:

   ```bash
   python3 update_products.py price --count 60000 --drop-amount 1
   ```

8. Refresh the custom catalog source again. Record the sync time and monitor
   side-effect verification, parent creation, audience building, internal
   events, and end-to-end completion.

Products, profiles, and historical events do not need to be recreated for
every run. For example, after a recent test whose events remain inside the
flow's lookback window, it may be sufficient to create only the 100 recent
events, change prices, and sync again. Always perform the baseline catalog sync
when adding products, and verify the current product count before relying on
one event per product.

## Test

Run the standard-library test suite:

```bash
python3 -m unittest discover -s tests
```
