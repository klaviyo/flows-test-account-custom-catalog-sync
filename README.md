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
- `stock`
- `categories`, `brand`, `sku`, and `radius`

The feed does not currently model product variants because Varun's example
feeds do not contain a variant structure to follow.

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

Enter the hosted feed URL and indicate whether it uses basic authentication.
The checker downloads the JSON and reports good, bad, and total row counts. A
good product must contain `id`, `title`, `link`, `image_link`, and
`description`; these field names do not use a `$` prefix.

The full terminal instructions from the internal runbook are also included as
comments at the top of `custom_catalog_checker.py`.

## Test

Run the standard-library test suite:

```bash
python3 -m unittest discover -s tests
```
