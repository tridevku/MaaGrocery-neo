import json
import os
import re
import sqlite3
import uuid
from datetime import datetime
from decimal import Decimal
from functools import wraps
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

try:
    import oracledb
except ImportError:  # The local SQLite demo can run before Oracle is installed.
    oracledb = None


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / os.getenv("SQLITE_DATABASE", "database.db")
UPLOAD_DIR = BASE_DIR / "static" / "images" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-this-dev-secret-key")

ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}
DELIVERY_CHARGE = Decimal("39.00")
FREE_DELIVERY_LIMIT = Decimal("499.00")


class Database:
    """Small database helper that supports Oracle and a local SQLite demo DB."""

    def __init__(self):
        backend = os.getenv("DB_BACKEND", "").strip().lower()
        oracle_requested = backend == "oracle" or (not backend and os.getenv("ORACLE_DSN"))
        self.backend = "oracle" if oracle_requested else "sqlite"

    @property
    def is_oracle(self):
        return self.backend == "oracle"

    def connect(self):
        if self.is_oracle:
            if oracledb is None:
                raise RuntimeError("Install the oracledb package before using DB_BACKEND=oracle.")
            return oracledb.connect(
                user=os.getenv("ORACLE_USER"),
                password=os.getenv("ORACLE_PASSWORD"),
                dsn=os.getenv("ORACLE_DSN"),
            )

        conn = sqlite3.connect(DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _rows_to_dicts(self, cursor, rows):
        if not rows:
            return []
        if self.is_oracle:
            columns = [column[0].lower() for column in cursor.description]
            return [dict(zip(columns, row)) for row in rows]
        return [dict(row) for row in rows]

    def fetch_all(self, sql, params=None):
        conn = self.connect()
        cursor = conn.cursor()
        try:
            cursor.execute(sql, params or {})
            rows = cursor.fetchall()
            return self._rows_to_dicts(cursor, rows)
        finally:
            cursor.close()
            conn.close()

    def fetch_one(self, sql, params=None):
        rows = self.fetch_all(sql, params)
        return rows[0] if rows else None

    def execute(self, sql, params=None):
        conn = self.connect()
        cursor = conn.cursor()
        try:
            cursor.execute(sql, params or {})
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    def insert(self, table, data):
        columns = list(data.keys())
        column_sql = ", ".join(columns)
        bind_sql = ", ".join(f":{column}" for column in columns)

        conn = self.connect()
        cursor = conn.cursor()
        try:
            if self.is_oracle:
                new_id = cursor.var(oracledb.NUMBER)
                params = dict(data)
                params["new_id"] = new_id
                cursor.execute(
                    f"INSERT INTO {table} ({column_sql}) VALUES ({bind_sql}) RETURNING id INTO :new_id",
                    params,
                )
                conn.commit()
                value = new_id.getvalue()
                if isinstance(value, list):
                    value = value[0]
                return int(value)

            cursor.execute(f"INSERT INTO {table} ({column_sql}) VALUES ({bind_sql})", data)
            conn.commit()
            return int(cursor.lastrowid)
        finally:
            cursor.close()
            conn.close()

    def list_tables(self):
        if self.is_oracle:
            rows = self.fetch_all("SELECT table_name FROM user_tables")
            return {row["table_name"].lower() for row in rows}
        rows = self.fetch_all("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {row["name"].lower() for row in rows}

    def limit(self, sql, count):
        return f"{sql} FETCH FIRST {count} ROWS ONLY" if self.is_oracle else f"{sql} LIMIT {count}"


db = Database()


def decimal_value(value):
    if value is None:
        return Decimal("0.00")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@app.template_filter("money")
def money_filter(value):
    return f"Rs. {decimal_value(value):,.2f}"


def slugify(text):
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return text or uuid.uuid4().hex[:8]


def unique_slug(name, table, current_id=None):
    base_slug = slugify(name)
    slug = base_slug
    counter = 2
    while True:
        params = {"slug": slug}
        sql = f"SELECT id FROM {table} WHERE slug = :slug"
        if current_id:
            sql += " AND id <> :current_id"
            params["current_id"] = current_id
        if not db.fetch_one(sql, params):
            return slug
        slug = f"{base_slug}-{counter}"
        counter += 1


def product_price(product):
    price = decimal_value(product.get("price"))
    discount = decimal_value(product.get("discount_price"))
    return discount if discount and discount < price else price


def schema_statements():
    if db.is_oracle:
        return {
            "users": """
                CREATE TABLE users (
                    id NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    name VARCHAR2(120) NOT NULL,
                    mobile VARCHAR2(20) NOT NULL,
                    email VARCHAR2(180),
                    address CLOB NOT NULL,
                    city VARCHAR2(80) NOT NULL,
                    pincode VARCHAR2(12) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """,
            "categories": """
                CREATE TABLE categories (
                    id NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    name VARCHAR2(100) NOT NULL,
                    slug VARCHAR2(120) UNIQUE NOT NULL,
                    image_url VARCHAR2(600),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """,
            "products": """
                CREATE TABLE products (
                    id NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    category_id NUMBER REFERENCES categories(id),
                    name VARCHAR2(160) NOT NULL,
                    slug VARCHAR2(180) UNIQUE NOT NULL,
                    description CLOB NOT NULL,
                    image_url VARCHAR2(600),
                    price NUMBER(10, 2) NOT NULL,
                    discount_price NUMBER(10, 2),
                    stock NUMBER DEFAULT 0 NOT NULL,
                    unit VARCHAR2(30) DEFAULT 'piece' NOT NULL,
                    is_best_seller NUMBER(1) DEFAULT 0 NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """,
            "cart": """
                CREATE TABLE cart (
                    id NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    session_id VARCHAR2(120) NOT NULL,
                    product_id NUMBER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    quantity NUMBER DEFAULT 1 NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT cart_session_product_unique UNIQUE (session_id, product_id)
                )
            """,
            "orders": """
                CREATE TABLE orders (
                    id NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    order_number VARCHAR2(60) UNIQUE NOT NULL,
                    user_id NUMBER REFERENCES users(id),
                    customer_name VARCHAR2(120) NOT NULL,
                    mobile VARCHAR2(20) NOT NULL,
                    email VARCHAR2(180),
                    address CLOB NOT NULL,
                    city VARCHAR2(80) NOT NULL,
                    pincode VARCHAR2(12) NOT NULL,
                    subtotal NUMBER(10, 2) NOT NULL,
                    delivery_charge NUMBER(10, 2) NOT NULL,
                    total_amount NUMBER(10, 2) NOT NULL,
                    payment_method VARCHAR2(30) NOT NULL,
                    payment_status VARCHAR2(40) DEFAULT 'Pending' NOT NULL,
                    order_status VARCHAR2(40) DEFAULT 'Placed' NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """,
            "order_items": """
                CREATE TABLE order_items (
                    id NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    order_id NUMBER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    product_id NUMBER REFERENCES products(id) ON DELETE SET NULL,
                    product_name VARCHAR2(160) NOT NULL,
                    price NUMBER(10, 2) NOT NULL,
                    quantity NUMBER NOT NULL,
                    total NUMBER(10, 2) NOT NULL
                )
            """,
            "payments": """
                CREATE TABLE payments (
                    id NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    order_id NUMBER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    paytm_order_id VARCHAR2(80),
                    txn_id VARCHAR2(120),
                    bank_txn_id VARCHAR2(120),
                    amount NUMBER(10, 2) NOT NULL,
                    status VARCHAR2(40) NOT NULL,
                    response_code VARCHAR2(30),
                    response_message VARCHAR2(500),
                    gateway_name VARCHAR2(100),
                    raw_response CLOB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """,
            "admin": """
                CREATE TABLE admin (
                    id NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    username VARCHAR2(80) UNIQUE NOT NULL,
                    password_hash VARCHAR2(255) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """,
        }

    return {
        "users": """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                mobile TEXT NOT NULL,
                email TEXT,
                address TEXT NOT NULL,
                city TEXT NOT NULL,
                pincode TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """,
        "categories": """
            CREATE TABLE categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug TEXT UNIQUE NOT NULL,
                image_url TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """,
        "products": """
            CREATE TABLE products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER REFERENCES categories(id),
                name TEXT NOT NULL,
                slug TEXT UNIQUE NOT NULL,
                description TEXT NOT NULL,
                image_url TEXT,
                price REAL NOT NULL,
                discount_price REAL,
                stock INTEGER DEFAULT 0 NOT NULL,
                unit TEXT DEFAULT 'piece' NOT NULL,
                is_best_seller INTEGER DEFAULT 0 NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """,
        "cart": """
            CREATE TABLE cart (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                quantity INTEGER DEFAULT 1 NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (session_id, product_id)
            )
        """,
        "orders": """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_number TEXT UNIQUE NOT NULL,
                user_id INTEGER REFERENCES users(id),
                customer_name TEXT NOT NULL,
                mobile TEXT NOT NULL,
                email TEXT,
                address TEXT NOT NULL,
                city TEXT NOT NULL,
                pincode TEXT NOT NULL,
                subtotal REAL NOT NULL,
                delivery_charge REAL NOT NULL,
                total_amount REAL NOT NULL,
                payment_method TEXT NOT NULL,
                payment_status TEXT DEFAULT 'Pending' NOT NULL,
                order_status TEXT DEFAULT 'Placed' NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """,
        "order_items": """
            CREATE TABLE order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
                product_name TEXT NOT NULL,
                price REAL NOT NULL,
                quantity INTEGER NOT NULL,
                total REAL NOT NULL
            )
        """,
        "payments": """
            CREATE TABLE payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                paytm_order_id TEXT,
                txn_id TEXT,
                bank_txn_id TEXT,
                amount REAL NOT NULL,
                status TEXT NOT NULL,
                response_code TEXT,
                response_message TEXT,
                gateway_name TEXT,
                raw_response TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """,
        "admin": """
            CREATE TABLE admin (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """,
    }


def init_database():
    existing_tables = db.list_tables()
    for table, ddl in schema_statements().items():
        if table not in existing_tables:
            db.execute(ddl)
    seed_database()


def seed_database():
    if not db.fetch_one("SELECT id FROM admin WHERE username = :username", {"username": "admin"}):
        db.insert(
            "admin",
            {
                "username": "admin",
                "password_hash": generate_password_hash("admin123"),
            },
        )

    if db.fetch_one("SELECT id FROM categories"):
        return

    categories = [
        {
            "name": "Fresh Fruits",
            "slug": "fresh-fruits",
            "image_url": "https://images.unsplash.com/photo-1619566636858-adf3ef46400b?auto=format&fit=crop&w=900&q=80",
        },
        {
            "name": "Vegetables",
            "slug": "vegetables",
            "image_url": "https://images.unsplash.com/photo-1542838132-92c53300491e?auto=format&fit=crop&w=900&q=80",
        },
        {
            "name": "Dairy & Bakery",
            "slug": "dairy-bakery",
            "image_url": "https://images.unsplash.com/photo-1628088062854-d1870b4553da?auto=format&fit=crop&w=900&q=80",
        },
        {
            "name": "Staples",
            "slug": "staples",
            "image_url": "https://images.unsplash.com/photo-1586201375761-83865001e31c?auto=format&fit=crop&w=900&q=80",
        },
        {
            "name": "Snacks & Drinks",
            "slug": "snacks-drinks",
            "image_url": "https://images.unsplash.com/photo-1621939514649-280e2ee25f60?auto=format&fit=crop&w=900&q=80",
        },
    ]

    category_ids = {}
    for category in categories:
        category_ids[category["slug"]] = db.insert("categories", category)

    products = [
        {
            "category_id": category_ids["fresh-fruits"],
            "name": "Robusta Banana",
            "slug": "robusta-banana",
            "description": "Naturally sweet bananas sourced daily from trusted farms. Perfect for breakfast bowls, shakes, and quick snacks.",
            "image_url": "https://images.unsplash.com/photo-1603833665858-e61d17a86224?auto=format&fit=crop&w=900&q=80",
            "price": 68.00,
            "discount_price": 54.00,
            "stock": 80,
            "unit": "1 dozen",
            "is_best_seller": 1,
        },
        {
            "category_id": category_ids["fresh-fruits"],
            "name": "Kashmir Apple",
            "slug": "kashmir-apple",
            "description": "Crisp, juicy apples with a deep red finish. Washed, graded, and packed for doorstep delivery.",
            "image_url": "https://images.unsplash.com/photo-1567306226416-28f0efdc88ce?auto=format&fit=crop&w=900&q=80",
            "price": 220.00,
            "discount_price": 189.00,
            "stock": 45,
            "unit": "1 kg",
            "is_best_seller": 1,
        },
        {
            "category_id": category_ids["vegetables"],
            "name": "Farm Tomato",
            "slug": "farm-tomato",
            "description": "Firm red tomatoes for curries, salads, chutneys, and daily cooking.",
            "image_url": "https://images.unsplash.com/photo-1592924357228-91a4daadcfea?auto=format&fit=crop&w=900&q=80",
            "price": 48.00,
            "discount_price": 39.00,
            "stock": 120,
            "unit": "1 kg",
            "is_best_seller": 1,
        },
        {
            "category_id": category_ids["vegetables"],
            "name": "Baby Spinach",
            "slug": "baby-spinach",
            "description": "Tender spinach leaves cleaned and bundled for soups, stir-fries, and smoothies.",
            "image_url": "https://images.unsplash.com/photo-1576045057995-568f588f82fb?auto=format&fit=crop&w=900&q=80",
            "price": 60.00,
            "discount_price": 49.00,
            "stock": 35,
            "unit": "250 g",
            "is_best_seller": 0,
        },
        {
            "category_id": category_ids["vegetables"],
            "name": "Green Broccoli",
            "slug": "green-broccoli",
            "description": "Fresh broccoli crowns, rich in fiber and ideal for salads, pasta, and healthy bowls.",
            "image_url": "https://images.unsplash.com/photo-1459411621453-7b03977f4bfc?auto=format&fit=crop&w=900&q=80",
            "price": 110.00,
            "discount_price": 95.00,
            "stock": 28,
            "unit": "500 g",
            "is_best_seller": 1,
        },
        {
            "category_id": category_ids["dairy-bakery"],
            "name": "Toned Milk",
            "slug": "toned-milk",
            "description": "Fresh toned milk with balanced fat content for tea, coffee, cereal, and cooking.",
            "image_url": "https://images.unsplash.com/photo-1563636619-e9143da7973b?auto=format&fit=crop&w=900&q=80",
            "price": 68.00,
            "discount_price": 64.00,
            "stock": 90,
            "unit": "1 litre",
            "is_best_seller": 1,
        },
        {
            "category_id": category_ids["dairy-bakery"],
            "name": "Whole Wheat Bread",
            "slug": "whole-wheat-bread",
            "description": "Soft whole wheat bread baked fresh for sandwiches, toast, and quick meals.",
            "image_url": "https://images.unsplash.com/photo-1509440159596-0249088772ff?auto=format&fit=crop&w=900&q=80",
            "price": 55.00,
            "discount_price": 47.00,
            "stock": 40,
            "unit": "400 g",
            "is_best_seller": 1,
        },
        {
            "category_id": category_ids["dairy-bakery"],
            "name": "Fresh Paneer",
            "slug": "fresh-paneer",
            "description": "Soft paneer made from quality milk, perfect for tikka, curries, and rolls.",
            "image_url": "https://images.unsplash.com/photo-1631452180519-c014fe946bc7?auto=format&fit=crop&w=900&q=80",
            "price": 155.00,
            "discount_price": 139.00,
            "stock": 32,
            "unit": "200 g",
            "is_best_seller": 0,
        },
        {
            "category_id": category_ids["staples"],
            "name": "Premium Basmati Rice",
            "slug": "premium-basmati-rice",
            "description": "Long-grain basmati rice aged for aroma and fluffy everyday cooking.",
            "image_url": "https://images.unsplash.com/photo-1586201375761-83865001e31c?auto=format&fit=crop&w=900&q=80",
            "price": 640.00,
            "discount_price": 589.00,
            "stock": 60,
            "unit": "5 kg",
            "is_best_seller": 1,
        },
        {
            "category_id": category_ids["staples"],
            "name": "Toor Dal",
            "slug": "toor-dal",
            "description": "Protein-rich toor dal for homestyle dals, sambhar, khichdi, and comfort meals.",
            "image_url": "https://images.unsplash.com/photo-1604329760661-e71dc83f8f26?auto=format&fit=crop&w=900&q=80",
            "price": 185.00,
            "discount_price": 169.00,
            "stock": 75,
            "unit": "1 kg",
            "is_best_seller": 1,
        },
        {
            "category_id": category_ids["staples"],
            "name": "Sunflower Oil",
            "slug": "sunflower-oil",
            "description": "Light sunflower cooking oil for frying, sauteing, and everyday meals.",
            "image_url": "https://images.unsplash.com/photo-1474979266404-7eaacbcd87c5?auto=format&fit=crop&w=900&q=80",
            "price": 185.00,
            "discount_price": 174.00,
            "stock": 50,
            "unit": "1 litre",
            "is_best_seller": 0,
        },
        {
            "category_id": category_ids["snacks-drinks"],
            "name": "Masala Makhana",
            "slug": "masala-makhana",
            "description": "Roasted makhana tossed with a light masala blend for a crunchy evening snack.",
            "image_url": "https://images.unsplash.com/photo-1621939514649-280e2ee25f60?auto=format&fit=crop&w=900&q=80",
            "price": 145.00,
            "discount_price": 129.00,
            "stock": 38,
            "unit": "100 g",
            "is_best_seller": 0,
        },
    ]

    for product in products:
        db.insert("products", product)


def get_categories():
    return db.fetch_all("SELECT * FROM categories ORDER BY name")


def product_select_sql():
    return """
        SELECT p.*, c.name AS category_name, c.slug AS category_slug
        FROM products p
        LEFT JOIN categories c ON c.id = p.category_id
    """


def get_cart_items():
    session_id = session.get("session_id")
    if not session_id:
        return []
    rows = db.fetch_all(
        """
        SELECT c.id AS cart_id, c.quantity, p.id AS product_id, p.name, p.slug,
               p.price, p.discount_price, p.stock, p.image_url, p.unit
        FROM cart c
        JOIN products p ON p.id = c.product_id
        WHERE c.session_id = :session_id
        ORDER BY c.id DESC
        """,
        {"session_id": session_id},
    )
    for item in rows:
        item["selling_price"] = product_price(item)
        item["line_total"] = item["selling_price"] * decimal_value(item["quantity"])
    return rows


def cart_summary():
    items = get_cart_items()
    subtotal = sum((item["line_total"] for item in items), Decimal("0.00"))
    delivery = Decimal("0.00") if not items or subtotal >= FREE_DELIVERY_LIMIT else DELIVERY_CHARGE
    return {
        "items": items,
        "subtotal": subtotal,
        "delivery_charge": delivery,
        "total": subtotal + delivery,
    }


def get_cart_count():
    session_id = session.get("session_id")
    if not session_id:
        return 0
    row = db.fetch_one(
        "SELECT COALESCE(SUM(quantity), 0) AS count FROM cart WHERE session_id = :session_id",
        {"session_id": session_id},
    )
    return int(row["count"] or 0)


def clear_cart():
    if session.get("session_id"):
        db.execute("DELETE FROM cart WHERE session_id = :session_id", {"session_id": session["session_id"]})


def validate_cart_stock(items):
    for item in items:
        if int(item["quantity"]) > int(item["stock"]):
            return f"{item['name']} has only {item['stock']} {item['unit']} left in stock."
    return None


def decrement_stock(items):
    for item in items:
        db.execute(
            """
            UPDATE products
            SET stock = CASE WHEN stock >= :quantity THEN stock - :quantity ELSE 0 END
            WHERE id = :product_id
            """,
            {"quantity": int(item["quantity"]), "product_id": int(item["product_id"])},
        )


def upsert_user(customer):
    user = None
    if customer.get("email"):
        user = db.fetch_one(
            "SELECT * FROM users WHERE email = :email OR mobile = :mobile",
            {"email": customer["email"], "mobile": customer["mobile"]},
        )
    else:
        user = db.fetch_one("SELECT * FROM users WHERE mobile = :mobile", {"mobile": customer["mobile"]})

    data = {
        "name": customer["name"],
        "mobile": customer["mobile"],
        "email": customer.get("email"),
        "address": customer["address"],
        "city": customer["city"],
        "pincode": customer["pincode"],
    }

    if user:
        params = dict(data)
        params["id"] = user["id"]
        db.execute(
            """
            UPDATE users
            SET name = :name, mobile = :mobile, email = :email, address = :address,
                city = :city, pincode = :pincode
            WHERE id = :id
            """,
            params,
        )
        return user["id"]

    return db.insert("users", data)


def create_order(customer, payment_method):
    summary = cart_summary()
    items = summary["items"]
    if not items:
        raise ValueError("Your cart is empty.")

    stock_error = validate_cart_stock(items)
    if stock_error:
        raise ValueError(stock_error)

    user_id = upsert_user(customer)
    order_number = f"MG{datetime.now().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:6].upper()}"
    payment_status = "COD Pending" if payment_method == "cod" else "Pending"
    order_status = "Placed" if payment_method == "cod" else "Payment Pending"

    order_id = db.insert(
        "orders",
        {
            "order_number": order_number,
            "user_id": user_id,
            "customer_name": customer["name"],
            "mobile": customer["mobile"],
            "email": customer.get("email"),
            "address": customer["address"],
            "city": customer["city"],
            "pincode": customer["pincode"],
            "subtotal": float(summary["subtotal"]),
            "delivery_charge": float(summary["delivery_charge"]),
            "total_amount": float(summary["total"]),
            "payment_method": payment_method,
            "payment_status": payment_status,
            "order_status": order_status,
        },
    )

    for item in items:
        db.insert(
            "order_items",
            {
                "order_id": order_id,
                "product_id": item["product_id"],
                "product_name": item["name"],
                "price": float(item["selling_price"]),
                "quantity": int(item["quantity"]),
                "total": float(item["line_total"]),
            },
        )

    db.insert(
        "payments",
        {
            "order_id": order_id,
            "paytm_order_id": order_number if payment_method == "paytm" else None,
            "amount": float(summary["total"]),
            "status": payment_status,
            "response_message": "Cash on delivery selected" if payment_method == "cod" else "Paytm payment initiated",
        },
    )

    if payment_method == "cod":
        decrement_stock(items)
        clear_cart()

    return db.fetch_one("SELECT * FROM orders WHERE id = :id", {"id": order_id})


def paytm_host():
    return "https://securegw-stage.paytm.in" if os.getenv("PAYTM_WEBSITE", "WEBSTAGING") == "WEBSTAGING" else "https://securegw.paytm.in"


def paytm_configured():
    mid = os.getenv("PAYTM_MID", "").strip()
    key = os.getenv("PAYTM_MERCHANT_KEY", "").strip()
    return bool(mid and key and not mid.startswith("your_") and not key.startswith("your_"))


def generate_paytm_signature(body):
    try:
        import PaytmChecksum
    except ImportError as exc:
        raise RuntimeError("Install paytmchecksum from requirements.txt to use Paytm payments.") from exc

    body_text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    return PaytmChecksum.generateSignature(body_text, os.getenv("PAYTM_MERCHANT_KEY"))


def verify_paytm_signature(params, checksum):
    try:
        import PaytmChecksum
    except ImportError:
        return False
    return PaytmChecksum.verifySignature(params, os.getenv("PAYTM_MERCHANT_KEY"), checksum)


def initiate_paytm_transaction(order):
    if not paytm_configured():
        raise RuntimeError("Paytm credentials are not configured.")

    mid = os.getenv("PAYTM_MID")
    callback_url = url_for("paytm_callback", _external=True)
    body = {
        "requestType": "Payment",
        "mid": mid,
        "websiteName": os.getenv("PAYTM_WEBSITE", "WEBSTAGING"),
        "orderId": order["order_number"],
        "callbackUrl": callback_url,
        "txnAmount": {"value": f"{decimal_value(order['total_amount']):.2f}", "currency": "INR"},
        "userInfo": {
            "custId": f"CUST_{order['user_id']}",
            "mobile": order["mobile"],
            "email": order.get("email") or "",
        },
    }
    signature = generate_paytm_signature(body)
    payload = {"body": body, "head": {"signature": signature}}
    url = f"{paytm_host()}/theia/api/v1/initiateTransaction?mid={mid}&orderId={order['order_number']}"
    response = requests.post(url, json=payload, timeout=20)
    response.raise_for_status()
    data = response.json()
    result = data.get("body", {}).get("resultInfo", {})
    if result.get("resultStatus") != "S":
        raise RuntimeError(result.get("resultMsg", "Paytm did not accept the transaction request."))
    return data["body"]["txnToken"]


def fetch_paytm_transaction_status(order_number):
    mid = os.getenv("PAYTM_MID")
    body = {"mid": mid, "orderId": order_number}
    payload = {"body": body, "head": {"signature": generate_paytm_signature(body)}}
    response = requests.post(f"{paytm_host()}/v3/order/status", json=payload, timeout=20)
    response.raise_for_status()
    return response.json()


def latest_payment(order_id):
    return db.fetch_one(
        db.limit("SELECT * FROM payments WHERE order_id = :order_id ORDER BY id DESC", 1),
        {"order_id": order_id},
    )


def save_payment_response(order, callback_params, status_response=None, signature_valid=False):
    body = (status_response or {}).get("body", {})
    result_info = body.get("resultInfo", {})
    status = body.get("resultStatus") or result_info.get("resultStatus") or callback_params.get("STATUS", "Pending")
    response_message = result_info.get("resultMsg") or callback_params.get("RESPMSG")

    data = {
        "paytm_order_id": order["order_number"],
        "txn_id": body.get("txnId") or callback_params.get("TXNID"),
        "bank_txn_id": body.get("bankTxnId") or callback_params.get("BANKTXNID"),
        "amount": float(decimal_value(body.get("txnAmount") or callback_params.get("TXNAMOUNT") or order["total_amount"])),
        "status": status,
        "response_code": result_info.get("resultCode") or callback_params.get("RESPCODE"),
        "response_message": response_message,
        "gateway_name": body.get("gatewayName") or callback_params.get("GATEWAYNAME"),
        "raw_response": json.dumps(
            {
                "signature_valid": signature_valid,
                "callback": callback_params,
                "status_api": status_response,
            },
            default=str,
        ),
    }

    payment = latest_payment(order["id"])
    if payment:
        params = dict(data)
        params["id"] = payment["id"]
        db.execute(
            """
            UPDATE payments
            SET paytm_order_id = :paytm_order_id, txn_id = :txn_id, bank_txn_id = :bank_txn_id,
                amount = :amount, status = :status, response_code = :response_code,
                response_message = :response_message, gateway_name = :gateway_name, raw_response = :raw_response
            WHERE id = :id
            """,
            params,
        )
    else:
        db.insert("payments", {"order_id": order["id"], **data})

    if status == "TXN_SUCCESS":
        payment_status = "Paid"
        order_status = "Confirmed"
        if order.get("payment_status") != "Paid":
            order_items = db.fetch_all(
                "SELECT product_id, quantity FROM order_items WHERE order_id = :order_id AND product_id IS NOT NULL",
                {"order_id": order["id"]},
            )
            decrement_stock(order_items)
        clear_cart()
    elif status in {"TXN_FAILURE", "F"}:
        payment_status = "Failed"
        order_status = "Payment Failed"
    else:
        payment_status = "Pending"
        order_status = "Payment Pending"

    db.execute(
        """
        UPDATE orders
        SET payment_status = :payment_status, order_status = :order_status
        WHERE id = :id
        """,
        {"payment_status": payment_status, "order_status": order_status, "id": order["id"]},
    )


def admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get("admin_id"):
            flash("Please log in to continue.", "warning")
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)

    return wrapped_view


def allowed_image(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def save_uploaded_image(file_storage):
    if not file_storage or not file_storage.filename:
        return None
    if not allowed_image(file_storage.filename):
        raise ValueError("Use a PNG, JPG, JPEG, WEBP, or GIF image.")
    filename = f"{uuid.uuid4().hex}_{secure_filename(file_storage.filename)}"
    file_storage.save(UPLOAD_DIR / filename)
    return url_for("static", filename=f"images/uploads/{filename}")


@app.before_request
def ensure_cart_session():
    if "session_id" not in session:
        session["session_id"] = uuid.uuid4().hex


@app.context_processor
def inject_layout_data():
    try:
        return {
            "cart_count": get_cart_count(),
            "categories_nav": get_categories(),
            "current_year": datetime.now().year,
        }
    except Exception:
        return {"cart_count": 0, "categories_nav": [], "current_year": datetime.now().year}


@app.route("/")
def index():
    categories = get_categories()
    best_sellers = db.fetch_all(
        db.limit(
            f"{product_select_sql()} WHERE p.is_best_seller = 1 AND p.stock > 0 ORDER BY p.created_at DESC",
            8,
        )
    )
    featured_products = db.fetch_all(db.limit(f"{product_select_sql()} WHERE p.stock > 0 ORDER BY p.id DESC", 4))
    return render_template(
        "index.html",
        categories=categories,
        best_sellers=best_sellers,
        featured_products=featured_products,
    )


@app.route("/products")
def products():
    search = request.args.get("q", "").strip()
    category_slug = request.args.get("category", "").strip()
    params = {}
    filters = ["1 = 1"]

    if search:
        params["search"] = f"%{search.lower()}%"
        filters.append("(LOWER(p.name) LIKE :search OR LOWER(p.description) LIKE :search)")
    if category_slug:
        params["category_slug"] = category_slug
        filters.append("c.slug = :category_slug")

    sql = f"{product_select_sql()} WHERE {' AND '.join(filters)} ORDER BY p.name"
    product_rows = db.fetch_all(sql, params)
    return render_template(
        "products.html",
        products=product_rows,
        categories=get_categories(),
        selected_category=category_slug,
        search=search,
    )


@app.route("/product/<slug>")
def product_detail(slug):
    product = db.fetch_one(f"{product_select_sql()} WHERE p.slug = :slug", {"slug": slug})
    if not product:
        abort(404)
    related = db.fetch_all(
        db.limit(
            f"{product_select_sql()} WHERE p.category_id = :category_id AND p.id <> :id ORDER BY p.id DESC",
            4,
        ),
        {"category_id": product["category_id"], "id": product["id"]},
    )
    return render_template("product_detail.html", product=product, related=related)


@app.route("/cart")
def cart():
    return render_template("cart.html", summary=cart_summary())


@app.post("/api/cart/add")
def api_cart_add():
    data = request.get_json(silent=True) or request.form
    product_id = int(data.get("product_id", 0))
    quantity = max(1, int(data.get("quantity", 1)))
    product = db.fetch_one("SELECT * FROM products WHERE id = :id", {"id": product_id})
    if not product:
        return jsonify({"ok": False, "message": "Product not found."}), 404
    if int(product["stock"]) <= 0:
        return jsonify({"ok": False, "message": "This item is out of stock."}), 400

    existing = db.fetch_one(
        "SELECT * FROM cart WHERE session_id = :session_id AND product_id = :product_id",
        {"session_id": session["session_id"], "product_id": product_id},
    )
    new_quantity = min(quantity, int(product["stock"]))
    if existing:
        new_quantity = min(int(existing["quantity"]) + quantity, int(product["stock"]))
        db.execute("UPDATE cart SET quantity = :quantity WHERE id = :id", {"quantity": new_quantity, "id": existing["id"]})
    else:
        db.insert(
            "cart",
            {
                "session_id": session["session_id"],
                "product_id": product_id,
                "quantity": new_quantity,
            },
        )

    return jsonify({"ok": True, "message": f"{product['name']} added to cart.", "cart_count": get_cart_count()})


@app.post("/api/cart/update")
def api_cart_update():
    data = request.get_json(silent=True) or request.form
    cart_id = int(data.get("cart_id", 0))
    quantity = int(data.get("quantity", 1))
    item = db.fetch_one(
        """
        SELECT c.*, p.stock
        FROM cart c
        JOIN products p ON p.id = c.product_id
        WHERE c.id = :id AND c.session_id = :session_id
        """,
        {"id": cart_id, "session_id": session["session_id"]},
    )
    if not item:
        return jsonify({"ok": False, "message": "Cart item not found."}), 404

    if quantity <= 0:
        db.execute("DELETE FROM cart WHERE id = :id", {"id": cart_id})
    else:
        db.execute(
            "UPDATE cart SET quantity = :quantity WHERE id = :id",
            {"quantity": min(quantity, int(item["stock"])), "id": cart_id},
        )

    summary = cart_summary()
    return jsonify(
        {
            "ok": True,
            "cart_count": get_cart_count(),
            "subtotal": str(summary["subtotal"]),
            "delivery_charge": str(summary["delivery_charge"]),
            "total": str(summary["total"]),
        }
    )


@app.post("/api/cart/remove")
def api_cart_remove():
    data = request.get_json(silent=True) or request.form
    db.execute(
        "DELETE FROM cart WHERE id = :id AND session_id = :session_id",
        {"id": int(data.get("cart_id", 0)), "session_id": session["session_id"]},
    )
    return jsonify({"ok": True, "cart_count": get_cart_count()})


@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    summary = cart_summary()
    if not summary["items"]:
        flash("Add products to your cart before checkout.", "warning")
        return redirect(url_for("products"))

    if request.method == "POST":
        customer = {
            "name": request.form.get("name", "").strip(),
            "mobile": request.form.get("mobile", "").strip(),
            "email": request.form.get("email", "").strip(),
            "address": request.form.get("address", "").strip(),
            "city": request.form.get("city", "").strip(),
            "pincode": request.form.get("pincode", "").strip(),
        }
        payment_method = request.form.get("payment_method", "cod")
        missing = [label for label, value in customer.items() if label != "email" and not value]
        if missing:
            flash("Please fill in all required delivery details.", "danger")
            return render_template("checkout.html", summary=summary, customer=customer)

        try:
            order = create_order(customer, payment_method)
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template("checkout.html", summary=summary, customer=customer)

        if payment_method == "paytm":
            if not paytm_configured():
                clear_cart()
                flash("Order saved. Add real Paytm credentials in .env to collect online payment.", "warning")
                return redirect(url_for("order_success", order_number=order["order_number"]))
            try:
                txn_token = initiate_paytm_transaction(order)
                return render_template(
                    "paytm_checkout.html",
                    order=order,
                    txn_token=txn_token,
                    paytm_mid=os.getenv("PAYTM_MID"),
                    paytm_host=paytm_host(),
                    paytm_amount=f"{decimal_value(order['total_amount']):.2f}",
                )
            except Exception as exc:
                db.execute(
                    """
                    UPDATE orders
                    SET payment_status = 'Failed', order_status = 'Payment Failed'
                    WHERE id = :id
                    """,
                    {"id": order["id"]},
                )
                flash(f"Paytm could not be started: {exc}", "danger")
                return redirect(url_for("cart"))

        flash("Order placed successfully.", "success")
        return redirect(url_for("order_success", order_number=order["order_number"]))

    return render_template("checkout.html", summary=summary, customer={})


@app.route("/payment/paytm/callback", methods=["POST"])
def paytm_callback():
    params = request.form.to_dict() or (request.get_json(silent=True) or {})
    checksum = params.pop("CHECKSUMHASH", params.pop("checksumhash", ""))
    order_number = params.get("ORDERID") or params.get("orderId")
    if not order_number:
        abort(400)

    order = db.fetch_one("SELECT * FROM orders WHERE order_number = :order_number", {"order_number": order_number})
    if not order:
        abort(404)

    signature_valid = verify_paytm_signature(params, checksum) if checksum and paytm_configured() else False
    status_response = None
    if paytm_configured():
        try:
            status_response = fetch_paytm_transaction_status(order_number)
        except Exception:
            status_response = None

    if checksum and paytm_configured() and not signature_valid and status_response is None:
        params["STATUS"] = "TXN_FAILURE"
        params["RESPMSG"] = "Paytm checksum verification failed."

    save_payment_response(order, params, status_response, signature_valid)
    return redirect(url_for("order_success", order_number=order_number))


@app.route("/order-success/<order_number>")
def order_success(order_number):
    order = db.fetch_one("SELECT * FROM orders WHERE order_number = :order_number", {"order_number": order_number})
    if not order:
        abort(404)
    items = db.fetch_all("SELECT * FROM order_items WHERE order_id = :order_id", {"order_id": order["id"]})
    payment = latest_payment(order["id"])
    return render_template("order_success.html", order=order, items=items, payment=payment)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        admin = db.fetch_one("SELECT * FROM admin WHERE username = :username", {"username": username})
        if admin and check_password_hash(admin["password_hash"], password):
            session["admin_id"] = admin["id"]
            session["admin_username"] = admin["username"]
            flash("Welcome back.", "success")
            return redirect(url_for("admin_dashboard"))
        flash("Invalid admin username or password.", "danger")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_id", None)
    session.pop("admin_username", None)
    flash("You have been logged out.", "success")
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    products_rows = db.fetch_all(f"{product_select_sql()} ORDER BY p.id DESC")
    orders = db.fetch_all("SELECT * FROM orders ORDER BY id DESC")
    order_items = {}
    for order in orders:
        order_items[order["id"]] = db.fetch_all("SELECT * FROM order_items WHERE order_id = :id", {"id": order["id"]})
    stats = {
        "products": len(products_rows),
        "orders": len(orders),
        "pending": len([order for order in orders if order["order_status"] in {"Placed", "Payment Pending"}]),
        "revenue": sum(decimal_value(order["total_amount"]) for order in orders if order["payment_status"] in {"Paid", "COD Pending"}),
    }
    return render_template(
        "admin_dashboard.html",
        products=products_rows,
        orders=orders,
        order_items=order_items,
        stats=stats,
        statuses=["Placed", "Confirmed", "Packed", "Out for Delivery", "Delivered", "Cancelled", "Payment Failed"],
    )


@app.route("/admin/products/add", methods=["GET", "POST"])
@admin_required
def add_product():
    if request.method == "POST":
        try:
            image_url = save_uploaded_image(request.files.get("image_file")) or request.form.get("image_url", "").strip()
            data = {
                "category_id": int(request.form.get("category_id")),
                "name": request.form.get("name", "").strip(),
                "slug": unique_slug(request.form.get("name", "").strip(), "products"),
                "description": request.form.get("description", "").strip(),
                "image_url": image_url,
                "price": float(request.form.get("price", 0)),
                "discount_price": float(request.form.get("discount_price") or 0),
                "stock": int(request.form.get("stock", 0)),
                "unit": request.form.get("unit", "piece").strip(),
                "is_best_seller": 1 if request.form.get("is_best_seller") else 0,
            }
            if not data["name"] or not data["description"]:
                raise ValueError("Product name and description are required.")
            db.insert("products", data)
            flash("Product added successfully.", "success")
            return redirect(url_for("admin_dashboard"))
        except Exception as exc:
            flash(str(exc), "danger")
    return render_template("add_product.html", categories=get_categories())


@app.route("/admin/products/<int:product_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_product(product_id):
    product = db.fetch_one("SELECT * FROM products WHERE id = :id", {"id": product_id})
    if not product:
        abort(404)

    if request.method == "POST":
        try:
            uploaded_url = save_uploaded_image(request.files.get("image_file"))
            image_url = uploaded_url or request.form.get("image_url", "").strip() or product.get("image_url")
            name = request.form.get("name", "").strip()
            params = {
                "id": product_id,
                "category_id": int(request.form.get("category_id")),
                "name": name,
                "slug": unique_slug(name, "products", current_id=product_id),
                "description": request.form.get("description", "").strip(),
                "image_url": image_url,
                "price": float(request.form.get("price", 0)),
                "discount_price": float(request.form.get("discount_price") or 0),
                "stock": int(request.form.get("stock", 0)),
                "unit": request.form.get("unit", "piece").strip(),
                "is_best_seller": 1 if request.form.get("is_best_seller") else 0,
            }
            db.execute(
                """
                UPDATE products
                SET category_id = :category_id, name = :name, slug = :slug, description = :description,
                    image_url = :image_url, price = :price, discount_price = :discount_price,
                    stock = :stock, unit = :unit, is_best_seller = :is_best_seller
                WHERE id = :id
                """,
                params,
            )
            flash("Product updated successfully.", "success")
            return redirect(url_for("admin_dashboard"))
        except Exception as exc:
            flash(str(exc), "danger")

    return render_template("edit_product.html", product=product, categories=get_categories())


@app.post("/admin/products/<int:product_id>/delete")
@admin_required
def delete_product(product_id):
    db.execute("DELETE FROM cart WHERE product_id = :product_id", {"product_id": product_id})
    db.execute("DELETE FROM products WHERE id = :id", {"id": product_id})
    flash("Product deleted.", "success")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/products/<int:product_id>/stock")
@admin_required
def update_stock(product_id):
    db.execute(
        "UPDATE products SET stock = :stock WHERE id = :id",
        {"stock": int(request.form.get("stock", 0)), "id": product_id},
    )
    flash("Stock updated.", "success")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/orders/<int:order_id>/status")
@admin_required
def update_order_status(order_id):
    db.execute(
        "UPDATE orders SET order_status = :status WHERE id = :id",
        {"status": request.form.get("order_status", "Placed"), "id": order_id},
    )
    flash("Order status updated.", "success")
    return redirect(url_for("admin_dashboard"))


@app.cli.command("init-db")
def init_db_command():
    init_database()
    print("MaaGrocery database is ready.")


if os.getenv("AUTO_INIT_DB", "true").lower() == "true":
    init_database()


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "true").lower() == "true")
