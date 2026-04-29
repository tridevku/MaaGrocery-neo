# MaaGrocery

MaaGrocery is a dynamic grocery e-commerce website built with Flask, HTML, CSS, JavaScript, Oracle-ready database access through `oracledb`, admin product management, cart, checkout, COD, and Paytm payment gateway integration.

## Features

- Home page with hero, search, categories, offers, and best sellers
- Product listing with search, category filters, images, prices, stock, quantity selector, and AJAX add to cart
- Product detail page
- Cart with quantity update, remove item, subtotal, delivery charge, and total
- Checkout with customer details, Paytm online payment, and cash on delivery
- Order success page with order ID, customer details, payment status, and order summary
- Admin login, dashboard, add/edit/delete products, stock management, order viewing, and order status updates
- Oracle Database support via `oracledb`
- Local SQLite demo database fallback for quick testing

## Admin Login

- Username: `admin`
- Password: `admin123`

## Run Locally

1. Create and activate a virtual environment:

```bash
python -m venv .venv
.venv\Scripts\activate
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create your local environment file:

```bash
copy .env.example .env
```

4. Start the website:

```bash
python app.py
```

5. Open:

```text
http://127.0.0.1:5000
```

The app creates tables and sample grocery data automatically when `AUTO_INIT_DB=true`.

## Oracle Database Setup

The project defaults to local demo mode with `database.db`. To use Oracle Database, edit `.env`:

```env
DB_BACKEND=oracle
ORACLE_USER=your_oracle_user
ORACLE_PASSWORD=your_oracle_password
ORACLE_DSN=localhost:1521/XEPDB1
```

Then run:

```bash
python app.py
```

The same tables are created in Oracle: `users`, `products`, `categories`, `cart`, `orders`, `order_items`, `payments`, and `admin`.

## Paytm Setup

Set real Paytm staging or production credentials in `.env`:

```env
PAYTM_MID=your_merchant_id
PAYTM_MERCHANT_KEY=your_merchant_key
PAYTM_WEBSITE=WEBSTAGING
PAYTM_INDUSTRY_TYPE=Retail
PAYTM_CHANNEL_ID=WEB
```

When real credentials are present, MaaGrocery creates a Paytm transaction token, opens Paytm JS Checkout, handles the callback, verifies payment status, and stores transaction details in `payments`.

Official Paytm references used for this implementation:

- Initiate Transaction API: https://business.paytm.com/docs/api/initiate-transaction-api/
- JS Checkout invoke flow: https://business.paytm.com/docs/jscheckout-invoke-payment/
- Transaction Status API: https://business.paytm.com/docs/api/v3/transaction-status-api
- Checksum implementation: https://business.paytm.com/docs/checksum-implementation

If Paytm credentials are still placeholders, online checkout saves the order as pending so the local demo stays usable.
