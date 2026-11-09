import os, re, sqlite3
from functools import wraps
from urllib.parse import quote
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, flash, redirect, render_template, request, session, url_for, abort, jsonify

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(BASE, "autoforge.db"))
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-this-secret")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS leads (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
          category TEXT DEFAULT '', address TEXT DEFAULT '', phone TEXT DEFAULT '',
          email TEXT DEFAULT '', lat REAL, lon REAL, osm_id TEXT UNIQUE,
          status TEXT DEFAULT 'new', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS shops (
          id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT UNIQUE NOT NULL,
          name TEXT NOT NULL, tagline TEXT DEFAULT '', description TEXT DEFAULT '',
          email TEXT DEFAULT '', phone TEXT DEFAULT '', address TEXT DEFAULT '',
          approved INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS products (
          id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER NOT NULL,
          name TEXT NOT NULL, description TEXT DEFAULT '', price TEXT DEFAULT '',
          image_url TEXT DEFAULT '', FOREIGN KEY(shop_id) REFERENCES shops(id)
        );
        """)
        c.commit()

def admin(fn):
    @wraps(fn)
    def inner(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return inner

def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:48] or "local-shop"

# Retry temporary network failures and use alternate Overpass map servers.
HTTP = requests.Session()
_retry = Retry(
    total=2, connect=2, read=1, status=2, backoff_factor=0.8,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET", "POST"]),
    respect_retry_after_header=True,
)
_adapter = HTTPAdapter(max_retries=_retry)
HTTP.mount("https://", _adapter)
HTTP.mount("http://", _adapter)

OVERPASS_SERVERS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

def find_coordinates(area):
    response = HTTP.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": area, "format": "jsonv2", "limit": 1},
        headers={"User-Agent": "AutoForgeAI/0.2 (business discovery app)"},
        timeout=(8, 25),
    )
    response.raise_for_status()
    places = response.json()
    if not places:
        return None
    return float(places[0]["lat"]), float(places[0]["lon"])

def fetch_map_elements(query):
    for endpoint in OVERPASS_SERVERS:
        try:
            response = HTTP.post(
                endpoint, data={"data": query},
                headers={"User-Agent": "AutoForgeAI/0.2 (business discovery app)"},
                timeout=(10, 45),
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or "elements" not in payload:
                raise ValueError("Unexpected response from map provider")
            return payload["elements"]
        except (requests.RequestException, ValueError):
            app.logger.warning("Map provider failed: %s", endpoint, exc_info=True)
    raise requests.RequestException("All map search providers are temporarily unavailable")

@app.route("/")
def home():
    with db() as c:
        leads = c.execute("SELECT * FROM leads ORDER BY id DESC LIMIT 100").fetchall()
        shops = c.execute("SELECT * FROM shops ORDER BY id DESC").fetchall()
    return render_template("index.html", leads=leads, shops=shops, logged_in=bool(session.get("admin")))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        expected = os.environ.get("ADMIN_PASSWORD")
        if not expected:
            flash("Set ADMIN_PASSWORD in your hosting environment first.", "error")
        elif request.form.get("password") == expected:
            session["admin"] = True
            return redirect(request.args.get("next") or url_for("home"))
        else:
            flash("Incorrect password.", "error")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/discover/import", methods=["POST"])
@admin
def import_discovered():
    payload = request.get_json(silent=True) or {}
    items = payload.get("elements", [])
    if not isinstance(items, list):
        return jsonify({"error": "The map response did not contain a valid list of results."}), 400
    if len(items) > 5000:
        return jsonify({"error": "Too many map results. Please search a smaller area."}), 413
    saved = 0
    checked = len(items)
    with db() as c:
        for item in items:
            if not isinstance(item, dict):
                continue
            tags = item.get("tags") or {}
            name = str(tags.get("name") or "").strip()
            if not name or tags.get("website") or tags.get("contact:website"):
                continue
            center = item.get("center") or {}
            lat = item.get("lat", center.get("lat"))
            lon = item.get("lon", center.get("lon"))
            address = ", ".join(x for x in [
                tags.get("addr:housenumber"), tags.get("addr:street"),
                tags.get("addr:suburb"), tags.get("addr:city")
            ] if x)
            osm_type, osm_id = str(item.get("type", "unknown")), str(item.get("id", ""))
            if not osm_id:
                continue
            cur = c.execute("""INSERT OR IGNORE INTO leads
                (name,category,address,phone,email,lat,lon,osm_id)
                VALUES (?,?,?,?,?,?,?,?)""",
                (name, tags.get("shop", tags.get("amenity", "shop")), address,
                 tags.get("phone", tags.get("contact:phone", "")),
                 tags.get("email", tags.get("contact:email", "")),
                 lat, lon, f"{osm_type}:{osm_id}"))
            saved += max(cur.rowcount, 0)
        c.commit()
    return jsonify({"checked": checked, "saved": saved})

@app.route("/discover", methods=["POST"])
@admin
def discover():
    area = request.form.get("area", "").strip()
    kind = request.form.get("category", "shop").strip().lower()
    if not area:
        flash("Enter an area to search.", "error")
        return redirect(url_for("home"))
    try:
        coordinates = find_coordinates(area)
        if coordinates is None:
            flash("Area not found. Try a more specific town, city or neighbourhood.", "error")
            return redirect(url_for("home"))
        lat, lon = coordinates
        tag = "shop" if kind in ("shop", "store", "retail") else kind
        q = f"""[out:json][timeout:35];(
          node(around:3000,{lat},{lon})[shop];
          way(around:3000,{lat},{lon})[shop];
          node(around:3000,{lat},{lon})[amenity={tag}];
          way(around:3000,{lat},{lon})[amenity={tag}];
        );out center tags;"""
        items = fetch_map_elements(q)
        saved = 0
        with db() as c:
            for item in items:
                t = item.get("tags", {})
                if t.get("website") or t.get("contact:website") or not t.get("name"):
                    continue
                pos = item.get("center", {})
                item_lat = item.get("lat", pos.get("lat"))
                item_lon = item.get("lon", pos.get("lon"))
                address = ", ".join(x for x in [t.get("addr:housenumber"), t.get("addr:street"), t.get("addr:suburb"), t.get("addr:city")] if x)
                cur = c.execute("""INSERT OR IGNORE INTO leads
                    (name,category,address,phone,email,lat,lon,osm_id)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (t["name"], t.get("shop", t.get("amenity", tag)), address,
                     t.get("phone", t.get("contact:phone", "")),
                     t.get("email", t.get("contact:email", "")),
                     item_lat, item_lon, f'{item.get("type")}:{item.get("id")}'))
                saved += max(cur.rowcount, 0)
            c.commit()
        flash(f"Checked {len(items)} map records; saved {saved} new listings. Verify every result manually.", "success")
    except requests.RequestException:
        app.logger.exception("Business discovery map request failed")
        flash("Map search is temporarily busy. AutoForge AI tried its backup map servers. Please wait a minute and try again.", "error")
    except (ValueError, KeyError):
        flash("The map service returned unexpected data.", "error")
    return redirect(url_for("home"))

@app.route("/lead/<int:lead_id>/create", methods=["POST"])
@admin
def create_shop(lead_id):
    with db() as c:
        lead = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not lead: abort(404)
        base, slug, n = slugify(lead["name"]), slugify(lead["name"]), 2
        while c.execute("SELECT 1 FROM shops WHERE slug=?", (slug,)).fetchone():
            slug, n = f"{base}-{n}", n + 1
        cur = c.execute("INSERT INTO shops(slug,name,email,phone,address) VALUES(?,?,?,?,?)",
            (slug, lead["name"], lead["email"], lead["phone"], lead["address"]))
        c.execute("UPDATE leads SET status='draft-created' WHERE id=?", (lead_id,))
        c.commit()
    return redirect(url_for("edit_shop", shop_id=cur.lastrowid))

@app.route("/shop/<int:shop_id>", methods=["GET", "POST"])
@admin
def edit_shop(shop_id):
    with db() as c:
        shop = c.execute("SELECT * FROM shops WHERE id=?", (shop_id,)).fetchone()
        if not shop: abort(404)
        if request.method == "POST":
            c.execute("UPDATE shops SET tagline=?,description=?,email=?,phone=?,address=?,approved=0 WHERE id=?",
                (request.form.get("tagline", ""), request.form.get("description", ""), request.form.get("email", ""), request.form.get("phone", ""), request.form.get("address", ""), shop_id))
            c.commit()
            flash("Draft saved. Publication remains disabled.", "success")
            return redirect(url_for("edit_shop", shop_id=shop_id))
        products = c.execute("SELECT * FROM products WHERE shop_id=? ORDER BY id DESC", (shop_id,)).fetchall()
    return render_template("shop.html", shop=shop, products=products)

@app.route("/shop/<int:shop_id>/product", methods=["POST"])
@admin
def add_product(shop_id):
    name = request.form.get("name", "").strip()
    if not name:
        flash("Product name is required.", "error")
        return redirect(url_for("edit_shop", shop_id=shop_id))
    with db() as c:
        if not c.execute("SELECT 1 FROM shops WHERE id=?", (shop_id,)).fetchone(): abort(404)
        c.execute("INSERT INTO products(shop_id,name,description,price,image_url) VALUES(?,?,?,?,?)",
            (shop_id, name, request.form.get("description", ""), request.form.get("price", ""), request.form.get("image_url", "")))
        c.commit()
    return redirect(url_for("edit_shop", shop_id=shop_id))

@app.route("/store/<slug>")
def storefront(slug):
    with db() as c:
        shop = c.execute("SELECT * FROM shops WHERE slug=? AND approved=1", (slug,)).fetchone()
        if not shop: abort(404)
        products = c.execute("SELECT * FROM products WHERE shop_id=?", (shop["id"],)).fetchall()
    return render_template("storefront.html", shop=shop, products=products)

@app.route("/shop/<int:shop_id>/publish", methods=["POST"])
@admin
def publish(shop_id):
    with db() as c:
        c.execute("UPDATE shops SET approved=1 WHERE id=?", (shop_id,))
        c.commit()
    flash("Storefront published. Only do this after the owner's explicit approval.", "success")
    return redirect(url_for("edit_shop", shop_id=shop_id))

@app.route("/lead/<int:lead_id>/email")
@admin
def email_draft(lead_id):
    with db() as c:
        lead = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not lead: abort(404)
    subject = f"A website idea for {lead['name']}"
    body = (f"Hello {lead['name']} team,\\n\\nI found your business listing in "
        f"{lead['address'] or 'your area'} and wanted to offer a simple website where customers can view products and contact you.\\n\\n"
        "I can prepare a no-obligation sample for you to review. Nothing will be published without your approval.\\n\\n"
        "If you are not interested, please ignore this message and I will not follow up.\\n\\nRegards")
    mailto = f"mailto:{quote(lead['email'])}?subject={quote(subject)}&body={quote(body)}" if lead["email"] else ""
    return render_template("email.html", lead=lead, subject=subject, body=body, mailto=mailto)

@app.errorhandler(404)
def missing(_):
    return render_template("404.html"), 404

init_db()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
