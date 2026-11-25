
import os
import json
import time
import random
import re
import html
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

# Scraping base
try:
    import requests
    from bs4 import BeautifulSoup
    SCRAPING_AVAILABLE = True
except ImportError:
    requests = None
    BeautifulSoup = None
    SCRAPING_AVAILABLE = False
    print("⚠️ Módulos de scraping no disponibles (requests/beautifulsoup4)")

# curl_cffi opcional
try:
    from curl_cffi import requests as crequests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    crequests = None
    CURL_CFFI_AVAILABLE = False


# =========================
# CONFIGURACIÓN
# =========================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
ASSOCIATE_TAG = os.environ.get("ASSOCIATE_TAG")
FORCE_PUBLISH = os.environ.get("FORCE_PUBLISH", "").lower() in ("1", "true", "yes")

CATEGORY_URL = "https://www.amazon.es/gp/bestsellers/pet-supplies"
CATEGORY_LABEL = "Mascotas"

# Archivos exclusivos de este proyecto
RANKING_FILE = "previous_ranking.json"
LAST_PRODUCTS_FILE = "last_products.json"

MAX_PRODUCTS = 10
MAX_TITLE_LENGTH = 100
TELEGRAM_DELAY_SECONDS = 1.2
TIME_ZONE = ZoneInfo("Europe/Madrid")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

URLS = [
    CATEGORY_URL,
]


# =========================
# UTILIDADES GENERALES
# =========================
def now_local():
    """Fecha y hora de España peninsular."""
    return datetime.now(TIME_ZONE)


def get_position_change(asin, current_position, previous_ranking):
    """Devuelve el movimiento del ASIN respecto al ranking anterior."""
    if asin not in previous_ranking:
        return "🆕 Nueva entrada", None

    previous_position = int(previous_ranking[asin])
    difference = previous_position - current_position

    if difference > 0:
        unit = "posición" if difference == 1 else "posiciones"
        return f"🔼 Sube {difference} {unit}", previous_position

    if difference < 0:
        amount = abs(difference)
        unit = "posición" if amount == 1 else "posiciones"
        return f"🔽 Baja {amount} {unit}", previous_position

    return "➡️ Sin cambios", previous_position


def save_ranking(products):
    ranking = {
        product["asin"]: position
        for position, product in enumerate(products, start=1)
        if product.get("asin")
    }

    with open(RANKING_FILE, "w", encoding="utf-8") as file:
        json.dump(ranking, file, indent=2, ensure_ascii=False)


def load_previous_ranking():
    if not os.path.exists(RANKING_FILE):
        return {}

    try:
        with open(RANKING_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}

    except (json.JSONDecodeError, OSError, TypeError):
        print("⚠️ No se pudo leer el histórico del ranking.")
        return {}


def save_last_products(products):
    with open(LAST_PRODUCTS_FILE, "w", encoding="utf-8") as file:
        json.dump(products, file, ensure_ascii=False, indent=2)


def load_last_products():
    if not os.path.exists(LAST_PRODUCTS_FILE):
        return []

    try:
        with open(LAST_PRODUCTS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, list) else []

    except (json.JSONDecodeError, OSError, TypeError):
        print("⚠️ No se pudo leer la caché de productos.")
        return []


def shorten_title(title, max_length=MAX_TITLE_LENGTH):
    """Limpia y acorta el título sin añadir características."""
    title = html.unescape(str(title or ""))
    title = re.sub(r"\s+", " ", title).strip(" \t\r\n|,-–—")

    if len(title) <= max_length:
        return title

    shortened = title[:max_length - 1]

    if " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0]

    return shortened.rstrip(" ,;:-") + "…"


def get_product_url(asin):
    tag = quote(ASSOCIATE_TAG or "", safe="-")
    return f"https://www.amazon.es/dp/{asin}/?tag={tag}"


def get_category_url():
    separator = "&" if "?" in CATEGORY_URL else "?"
    tag = quote(ASSOCIATE_TAG or "", safe="-")
    return f"{CATEGORY_URL}{separator}tag={tag}"


def get_position_badge(position):
    return {
        1: "🥇",
        2: "🥈",
        3: "🥉",
    }.get(position)


# =========================
# SCRAPING
# =========================
def _fetch_html(max_attempts=3):
    for attempt in range(1, max_attempts + 1):
        url = URLS[(attempt - 1) % len(URLS)]

        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9",
            "Connection": "keep-alive",
        }

        try:
            if CURL_CFFI_AVAILABLE:
                response = crequests.get(
                    url,
                    headers=headers,
                    impersonate="chrome124",
                    timeout=15,
                )
            elif SCRAPING_AVAILABLE:
                response = requests.get(
                    url,
                    headers=headers,
                    timeout=15,
                )
            else:
                print("❌ No hay ningún cliente HTTP disponible.")
                return None

            size = len(response.content)

            print(
                f"   intento {attempt}: HTTP {response.status_code} · "
                f"{size} bytes · cffi={CURL_CFFI_AVAILABLE}"
            )

            if response.status_code == 200 and size > 200_000:
                return response.content

            if response.status_code in (429, 503):
                print("   ↳ Amazon ha limitado temporalmente la petición.")

        except Exception as error:
            print(f"   intento {attempt}: excepción {error}")

        if attempt < max_attempts:
            time.sleep(2 ** attempt * 2)

    return None


def extract_image_url(item):
    image_element = item.find("img")

    if not image_element:
        return None

    dynamic_images = image_element.get("data-a-dynamic-image")

    if dynamic_images:
        try:
            image_options = json.loads(dynamic_images)

            if image_options:
                return max(
                    image_options.items(),
                    key=lambda entry: entry[1][0] * entry[1][1]
                )[0]

        except (ValueError, TypeError, IndexError, KeyError):
            pass

    return (
        image_element.get("data-old-hires")
        or image_element.get("data-src")
        or image_element.get("src")
    )


def get_products_with_scraping():
    if not SCRAPING_AVAILABLE:
        print("❌ Scraping no disponible.")
        return []

    print(f"📡 Scraping de {CATEGORY_LABEL}…")

    page_html = _fetch_html(max_attempts=3)

    if not page_html:
        print("❌ Todas las peticiones a Amazon fallaron.")
        return []

    soup = BeautifulSoup(page_html, "html.parser")

    items = (
        soup.find_all("div", {"class": "zg-grid-general-faceout"})
        or soup.select('div[id^="gridItemRoot"]')
        or soup.select("li.zg-item-immersion")
        or soup.select("div[data-asin]")
    )

    print(f"   {len(items)} elementos encontrados en la página.")

    products = []
    seen_asins = set()

    for item in items:
        try:
            link = item.select_one(
                'a[href*="/dp/"], a[href*="/gp/product/"]'
            )

            if not link or not link.get("href"):
                continue

            asin_match = re.search(
                r"/(?:dp|gp/product)/([A-Z0-9]{10})",
                link["href"]
            )

            if not asin_match:
                continue

            asin = asin_match.group(1)

            if asin in seen_asins:
                continue

            title_element = (
                item.find(
                    "div",
                    {"class": "_cDEzb_p13n-sc-css-line-clamp-2_EWgCb"}
                )
                or item.find(
                    "div",
                    {"class": "_cDEzb_p13n-sc-css-line-clamp-1_1Fn1y"}
                )
                or item.find(
                    "div",
                    {"class": "p13n-sc-truncate"}
                )
            )

            if title_element:
                title = title_element.get_text(" ", strip=True)
            else:
                image_element = item.find("img", alt=True)
                title = (
                    image_element.get("alt", "Título no disponible")
                    if image_element
                    else "Título no disponible"
                )

            image_url = extract_image_url(item)

            products.append({
                "asin": asin,
                "title": title,
                "image": image_url,
            })

            seen_asins.add(asin)

            if len(products) >= MAX_PRODUCTS:
                break

        except Exception as error:
            print(f"⚠️ Error procesando un producto: {error}")

    print(f"✅ Scraping completado: {len(products)} productos obtenidos.")

    return products


def get_products():
    return get_products_with_scraping()


# =========================
# LÓGICA ADAPTATIVA
# =========================
def should_publish(products, previous_ranking, force=False):
    if force:
        return True, "publicación forzada por FORCE_PUBLISH"

    if not previous_ranking:
        return True, "primera ejecución (sin histórico)"

    current_top = {
        product["asin"]: position
        for position, product in enumerate(products[:MAX_PRODUCTS], start=1)
    }

    new_entries = sum(
        1 for asin in current_top
        if asin not in previous_ranking
    )

    dropped = sum(
        1 for asin in previous_ranking
        if asin not in current_top
    )

    movement = sum(
        abs(int(previous_ranking[asin]) - position)
        for asin, position in current_top.items()
        if asin in previous_ranking
    )

    reason = (
        f"nuevos={new_entries}, salidos={dropped}, "
        f"movimiento acumulado={movement}"
    )

    if now_local().weekday() == 0:
        return True, f"lunes (actualización semanal); {reason}"

    if new_entries >= 3:
        return True, f"{new_entries} productos nuevos; {reason}"

    if dropped >= 3:
        return True, f"{dropped} productos han salido del TOP; {reason}"

    if movement >= 8:
        return True, f"movimiento importante; {reason}"

    return False, f"sin novedades suficientes ({reason})"


# =========================
# GENERACIÓN DE MENSAJES
# =========================
def build_product_caption(product, position, previous_ranking):
    asin = product["asin"]

    title = shorten_title(
        product.get("title", "Título no disponible")
    )

    movement, previous_position = get_position_change(
        asin,
        position,
        previous_ranking,
    )

    badge = get_position_badge(position)

    safe_title = html.escape(title)
    product_url = html.escape(
        get_product_url(asin),
        quote=True
    )

    if badge:
        title_line = (
            f"{badge} <b>N.º {position} · {safe_title}</b>"
        )
    else:
        title_line = f"<b>{position}. {safe_title}</b>"

    lines = [title_line]

    if movement == "➡️ Sin cambios":
        lines.append("\n➡️ Sin cambios")

    elif movement.startswith("🆕"):
        lines.append(f"\n{movement}")

    else:
        lines.append(
            f"\n{movement} (antes #{previous_position})"
        )

    lines.append(
        f'\n<a href="{product_url}">🛒 Ver producto en Amazon</a>'
    )

    return "\n".join(lines)


def build_header():
    date_text = now_local().strftime("%d/%m/%Y")

    safe_category = html.escape(CATEGORY_LABEL.upper())

    return (
        f"🐾 <b>TOP {MAX_PRODUCTS} {safe_category} MÁS VENDIDOS</b>\n"
        f"📅 {date_text}"
    )


def build_footer():
    return (
        "🐾 <b>¿Quieres descubrir más productos para tus mascotas?</b>\n"
        "Consulta el ranking completo de Amazon."
    )


# =========================
# PUBLICACIÓN EN TELEGRAM
# =========================
async def send_one_product(bot, product, position, previous_ranking):
    caption = build_product_caption(
        product,
        position,
        previous_ranking,
    )

    image_url = product.get("image")

    if image_url:
        try:
            await bot.send_photo(
                chat_id=TELEGRAM_CHANNEL_ID,
                photo=image_url,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )

            print(
                f"✅ Producto #{position} publicado con fotografía."
            )
            return

        except Exception as error:
            print(
                f"⚠️ No se pudo enviar la fotografía del producto "
                f"#{position}: {error}. Se intentará enviar solo texto."
            )

    await bot.send_message(
        chat_id=TELEGRAM_CHANNEL_ID,
        text=caption,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

    print(f"✅ Producto #{position} publicado como texto.")


async def send_ranking_with_individual_photos(
    bot,
    products,
    previous_ranking
):
    top_products = products[:MAX_PRODUCTS]

    if not top_products:
        print("❌ No hay productos para publicar.")
        return

    # 1. Cabecera
    await bot.send_message(
        chat_id=TELEGRAM_CHANNEL_ID,
        text=build_header(),
        parse_mode=ParseMode.HTML,
    )

    await asyncio.sleep(TELEGRAM_DELAY_SECONDS)

    # 2. Un mensaje por producto con fotografía individual
    for position, product in enumerate(top_products, start=1):
        await send_one_product(
            bot,
            product,
            position,
            previous_ranking,
        )

        if position < len(top_products):
            await asyncio.sleep(TELEGRAM_DELAY_SECONDS)

    # 3. Cierre y botón al ranking completo
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🛒 Ver todos los más vendidos",
            url=get_category_url(),
        )
    ]])

    await asyncio.sleep(TELEGRAM_DELAY_SECONDS)

    await bot.send_message(
        chat_id=TELEGRAM_CHANNEL_ID,
        text=build_footer(),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )

    print(
        f"✅ Publicación completada: cabecera + "
        f"{len(top_products)} productos + cierre."
    )


# =========================
# MAIN
# =========================
async def main():
    missing = []

    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_TOKEN")

    if not TELEGRAM_CHANNEL_ID:
        missing.append("TELEGRAM_CHANNEL_ID")

    if not ASSOCIATE_TAG:
        missing.append("ASSOCIATE_TAG")

    if missing:
        print(
            "❌ Faltan variables de entorno: "
            + ", ".join(missing)
        )
        return

    previous_ranking = load_previous_ranking()

    products = get_products()

    if not products:
        print(
            "⚠️ El scraping ha fallado. "
            "Intentando recuperar la caché…"
        )

        products = load_last_products()

        if not products:
            print(
                "❌ No hay caché disponible. "
                "No se publicará nada."
            )
            return

        print(
            f"♻️ Recuperados {len(products)} productos de la caché."
        )

    # Filtra entradas incompletas
    products = [
        product for product in products
        if product.get("asin") and product.get("title")
    ][:MAX_PRODUCTS]

    if not products:
        print("❌ No hay productos válidos para publicar.")
        return

    print(
        f"📚 Ranking obtenido: {len(products)} productos."
    )

    publish, reason = should_publish(
        products,
        previous_ranking,
        force=FORCE_PUBLISH,
    )

    if not publish:
        print(f"⏭️ Se omite la publicación: {reason}")

        # Actualiza la caché, pero no el histórico publicado
        save_last_products(products)
        return

    print(f"✅ Se publicará el ranking: {reason}")

    async with Bot(token=TELEGRAM_TOKEN) as bot:
        await send_ranking_with_individual_photos(
            bot,
            products,
            previous_ranking,
        )

    # Actualiza el histórico después de completar la publicación
    save_ranking(products)
    save_last_products(products)

    print("💾 Histórico y caché actualizados.")


if __name__ == "__main__":
    asyncio.run(main())
