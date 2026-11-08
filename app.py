import os, re, sqlite3
from functools import wraps
from urllib.parse import quote
import requests
from flask import Flask, flash, redirect, render_template, request, session, url_for, abort

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

@app.route("/discover", methods=["POST"])
@admin
def discover():
    area = request.form.get("area", "").strip()
    kind = request.form.get("category", "shop").strip().lower()
    if not area:
        flash("Enter an area to search.", "error")
        return redirect(url_for("home"))
    try:
        g = requests.get("https://nominatim.openstreetmap.org/search",
            params={"q": area, "format": "jsonv2", "limit": 1},
            headers={"User-Agent": "AutoForgeAI/0.1"}, timeout=20)
        g.raise_for_status()
        places = g.json()
        if not places:
            flash("Area not found. Try a more specific name.", "error")
            return redirect(url_for("home"))
        lat, lon = float(places[0]["lat"]), float(places[0]["lon"])
        tag = "shop" if kind in ("shop", "store", "retail") else kind
        q = f"""[out:json][timeout:25];(
          node(around:3000,{lat},{lon})[shop];
          way(around:3000,{lat},{lon})[shop];
          node(around:3000,{lat},{lon})[amenity={tag}];
          way(around:3000,{lat},{lon})[amenity={tag}];
        );out center tags;"""
        r = requests.post("https://overpass-api.de/api/interpreter", data=q,
            headers={"User-Agent": "AutoForgeAI/0.1"}, timeout=35)
        r.raise_for_status()
        items = r.json().get("elements", [])
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
        flash("The map service is temporarily unavailable. Try again later.", "error")
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
