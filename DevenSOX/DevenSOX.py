import os
import json
import time
import hmac
import hashlib
import uuid
import fcntl
import requests
import boto3

from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlencode

# ============================================================
# DEVENSOX - INVENTORY BACKED SELL GRID
# ============================================================

BOT_NAME = "DevenSOX"
SECRET_NAME = "Deven"
AWS_REGION = "ap-south-1"

BASE_URL = "https://api.india.delta.exchange"
SYMBOL = "SOXLBUSD"

BOT_PREFIX = "DVSOX-"

BASE_DIR = "/home/ec2-user/delta-bot/DevenSOX"
STATE_FILE = BASE_DIR + "/DevenSOX_state.json"
LOCK_FILE = BASE_DIR + "/DevenSOX.lock"

POLL_SECONDS = 3
RETRY_SECONDS = 10
PAGE_SIZE = 100

session = requests.Session()

session.headers.update({
    "User-Agent": "python-rest-client",
    "Accept": "application/json",
    "Content-Type": "application/json"
})


# ============================================================
# LOGGING
# ============================================================

def log(message):
    print(
        time.strftime("%Y-%m-%d %H:%M:%S"),
        "|",
        message,
        flush=True
    )


# ============================================================
# AWS SECRET
# ============================================================

def load_config():

    sm = boto3.client(
        "secretsmanager",
        region_name=AWS_REGION
    )

    result = sm.get_secret_value(
        SecretId=SECRET_NAME
    )

    data = json.loads(result["SecretString"])

    required = [
        "API_Key",
        "API_Secret_Key",
        "Max_Position",
        "Order_Size",
        "Step",
        "Counter_Step"
    ]

    for key in required:
        if key not in data:
            raise RuntimeError("Missing secret field: " + key)

    def read_number(key):

        value = str(data[key]).strip()

        value = (
            value.replace("$", "")
            .replace(",", "")
            .replace("lots", "")
            .replace("lot", "")
            .strip()
        )

        number = Decimal(value)

        if not number.is_finite() or number <= 0:
            raise RuntimeError("Invalid secret value: " + key)

        return number

    maximum = read_number("Max_Position")
    quantity = read_number("Order_Size")
    step = read_number("Step")
    counter = read_number("Counter_Step")

    if maximum != maximum.to_integral_value():
        raise RuntimeError("Max_Position must be whole lots")

    if quantity != quantity.to_integral_value():
        raise RuntimeError("Order_Size must be whole lots")

    if maximum < quantity or maximum % quantity != 0:
        raise RuntimeError("Invalid Max_Position / Order_Size")

    return (
        str(data["API_Key"]).strip(),
        str(data["API_Secret_Key"]).strip(),
        int(maximum),
        int(quantity),
        step,
        counter
    )


API_KEY, API_SECRET, MAX_POSITION, ORDER_SIZE, STEP, COUNTER_STEP = load_config()


# ============================================================
# DELTA AUTHENTICATION
# ============================================================

def api_request(method, path, params=None, body=None, authenticated=True):

    params = params or {}

    query = urlencode(params)

    payload = ""

    if body is not None:
        payload = json.dumps(
            body,
            separators=(",", ":")
        )

    headers = {}

    if authenticated:

        timestamp = str(int(time.time()))

        signature_text = (
            method.upper()
            + timestamp
            + path
            + ("?" + query if query else "")
            + payload
        )

        signature = hmac.new(
            API_SECRET.encode(),
            signature_text.encode(),
            hashlib.sha256
        ).hexdigest()

        headers = {
            "api-key": API_KEY,
            "timestamp": timestamp,
            "signature": signature
        }

    url = BASE_URL + path

    if query:
        url += "?" + query

    response = session.request(
        method.upper(),
        url,
        data=payload if body is not None else None,
        headers=headers,
        timeout=(5, 25)
    )

    try:
        data = response.json()
    except Exception:
        raise RuntimeError(
            "Delta returned non-JSON response: "
            + str(response.status_code)
        )

    if not response.ok or data.get("success") is False:
        raise RuntimeError(
            "Delta API error: " + json.dumps(data)[:700]
        )

    return data


def unwrap(data):
    return data.get("result", data)


# ============================================================
# PRODUCT DISCOVERY
# ============================================================

def resolve_product():

    data = api_request(
        "GET",
        "/v2/products/" + SYMBOL,
        authenticated=False
    )

    product = unwrap(data)

    if not isinstance(product, dict):
        raise RuntimeError("Invalid product response")

    if product.get("symbol") != SYMBOL:
        raise RuntimeError("Wrong Delta product returned")

    if product.get("state") != "live":
        raise RuntimeError("SOXLBUSD is not live")

    product_id = product.get("id")

    if product_id is None:
        raise RuntimeError("Product ID missing")

    tick = Decimal(str(product.get("tick_size", "0")))

    if tick <= 0:
        raise RuntimeError("Invalid product tick size")

    return int(product_id), tick


PRODUCT_ID, TICK_SIZE = resolve_product()


# ============================================================
# PRICE HANDLING
# ============================================================

def round_price(value):

    value = Decimal(str(value))

    rounded = value.quantize(
        TICK_SIZE,
        rounding=ROUND_HALF_UP
    )

    return rounded


def price_text(value):
    return format(
        Decimal(str(value)).normalize(),
        "f"
    )


# ============================================================
# MARKET PRICE
# ============================================================

def get_cmp():

    data = api_request(
        "GET",
        "/v2/tickers/" + SYMBOL,
        authenticated=False
    )

    ticker = unwrap(data)

    for key in ("close", "last_price", "mark_price", "spot_price"):

        if ticker.get(key) is not None:

            price = Decimal(str(ticker[key]))

            if price > 0:
                return price

    raise RuntimeError("Cannot read SOXLBUSD market price")


# ============================================================
# ACCOUNT POSITION
# ============================================================

def get_position():

    data = api_request(
        "GET",
        "/v2/positions",
        params={"product_id": PRODUCT_ID}
    )

    position = unwrap(data)

    if not isinstance(position, dict):
        raise RuntimeError("Invalid position response")

    return int(position.get("size", 0))


# ============================================================
# OPEN ORDERS
# ============================================================

def get_open_orders():

    result = []
    cursor = None

    while True:

        params = {
            "product_id": PRODUCT_ID,
            "state": "open",
            "page_size": PAGE_SIZE
        }

        if cursor:
            params["after"] = cursor

        data = api_request(
            "GET",
            "/v2/orders",
            params=params
        )

        result.extend(data.get("result", []))

        cursor = data.get("meta", {}).get("after")

        if not cursor:
            break

    return result


def get_bot_open_orders():

    return {
        str(order["id"]): order
        for order in get_open_orders()
        if str(order.get("client_order_id", "")).startswith(BOT_PREFIX)
    }


def get_reserved_sell_quantity():

    total = 0

    # Reserve ALL open sell orders on this product,
    # including manual orders and other bots.
    # They are never modified or cancelled.

    for order in get_open_orders():

        if str(order.get("side", "")).lower() != "sell":
            continue

        remaining = order.get("unfilled_size")

        if remaining is None:
            raise RuntimeError(
                "Cannot verify outstanding sell order quantity"
            )

        total += int(remaining)

    return total


# ============================================================
# STATE
# ============================================================

def default_state():

    return {
        "version": 1,
        "initialized": False,
        "anchor": None,
        "sequence": 1,
        "orders": {},
        "pending": {},
        "processed_fills": {},
        "created_at": int(time.time())
    }


def load_state():

    if not os.path.exists(STATE_FILE):
        return default_state()

    with open(STATE_FILE) as file:
        state = json.load(file)

    if state.get("version") != 1:
        raise RuntimeError("Invalid DevenSOX state version")

    state.setdefault("orders", {})
    state.setdefault("pending", {})
    state.setdefault("processed_fills", {})
    state.setdefault("sequence", 1)

    return state


STATE = None


def save_state():

    temporary = STATE_FILE + ".tmp"

    with open(temporary, "w") as file:

        json.dump(
            STATE,
            file,
            indent=2
        )

        file.flush()
        os.fsync(file.fileno())

    os.replace(temporary, STATE_FILE)


# ============================================================
# CLIENT ORDER IDS
# ============================================================

def new_client_id():

    sequence = STATE["sequence"]

    STATE["sequence"] += 1

    save_state()

    return (
        BOT_PREFIX
        + str(sequence)
        + "-"
        + uuid.uuid4().hex[:8]
    )


def counter_client_id(parent_order_id, cumulative_fill):

    digest = hashlib.sha256(
        (
            str(parent_order_id)
            + ":"
            + str(cumulative_fill)
        ).encode()
    ).hexdigest()[:24]

    return BOT_PREFIX + "C-" + digest


# ============================================================
# ORDER LOOKUP
# ============================================================

def get_order_by_id(order_id):

    data = api_request(
        "GET",
        "/v2/orders/" + str(order_id)
    )

    return unwrap(data)


def get_order_by_client_id(client_id):

    data = api_request(
        "GET",
        "/v2/orders/client_order_id/" + client_id
    )

    return unwrap(data)


# ============================================================
# ORDER QUEUE
# ============================================================

def queue_order(side, price, quantity, role, client_id=None):

    price = round_price(price)

    quantity = int(quantity)

    if quantity <= 0:
        return

    if client_id is None:
        client_id = new_client_id()

    if client_id in STATE["pending"]:
        return

    for order in STATE["orders"].values():

        if order.get("client_order_id") == client_id:
            return

    STATE["pending"][client_id] = {
        "side": side,
        "price": str(price),
        "size": quantity,
        "role": role,
        "client_order_id": client_id,
        "last_attempt": 0
    }

    save_state()


# ============================================================
# INVENTORY SAFETY
# ============================================================

def can_sell(quantity):

    position = get_position()

    if position <= 0:
        return False

    reserved = get_reserved_sell_quantity()

    available = position - reserved

    if available < quantity:
        log(
            "SELL BLOCKED | "
            f"Position={position} | "
            f"Reserved sells={reserved} | "
            f"Available={available}"
        )

        return False

    return True


# ============================================================
# PLACE ORDER
# ============================================================

def place_order(intent):

    side = intent["side"]

    quantity = int(intent["size"])

    price = round_price(intent["price"])

    if side == "sell":

        if not can_sell(quantity):
            return None

    client_id = intent["client_order_id"]

    body = {
        "product_id": PRODUCT_ID,
        "size": quantity,
        "side": side,
        "order_type": "limit_order",
        "limit_price": price_text(price),
        "time_in_force": "gtc",
        "post_only": False,
        "reduce_only": side == "sell",
        "client_order_id": client_id
    }

    data = api_request(
        "POST",
        "/v2/orders",
        body=body
    )

    order = unwrap(data)

    if not isinstance(order, dict) or not order.get("id"):
        raise RuntimeError("Order was not confirmed")

    order_id = str(order["id"])

    STATE["orders"][order_id] = {
        "client_order_id": client_id,
        "side": side,
        "price": str(price),
        "size": quantity,
        "filled_processed": 0,
        "role": intent["role"]
    }

    STATE["pending"].pop(client_id, None)

    save_state()

    log(
        f"ORDER CONFIRMED | {side.upper()} "
        f"{quantity} lots @ ${price_text(price)} "
        f"| ID={order_id}"
    )

    return order_id


# ============================================================
# INITIAL SELL GRID
# ============================================================

def initialize_grid():

    if STATE["initialized"]:
        return

    cmp = get_cmp()

    anchor = round_price(cmp)

    position = get_position()

    if position <= 0:
        raise RuntimeError(
            "No net long SOXLBUSD inventory available"
        )

    reserved = get_reserved_sell_quantity()

    available = min(
        position - reserved,
        MAX_POSITION
    )

    levels = available // ORDER_SIZE

    STATE["anchor"] = str(anchor)

    log("====================================")
    log("DEVENSOX INITIAL SELL GRID")
    log("====================================")

    log(f"CMP: ${price_text(cmp)}")
    log(f"Anchor: ${price_text(anchor)}")
    log(f"Net long position: {position}")
    log(f"Existing reserved sells: {reserved}")
    log(f"Available inventory: {available}")
    log(f"Order size: {ORDER_SIZE}")
    log(f"Maximum allocation: {MAX_POSITION}")
    log(f"Step: ${price_text(STEP)}")
    log(f"Counter step: ${price_text(COUNTER_STEP)}")

    for level in range(1, levels + 1):

        price = anchor + STEP * level

        queue_order(
            "sell",
            price,
            ORDER_SIZE,
            "INITIAL"
        )

    STATE["initialized"] = True

    save_state()

    log(f"INITIAL SELL GRID QUEUED | Levels={levels}")


# ============================================================
# COUNTER ORDER RECOVERY
# ============================================================

def submit_pending():

    for client_id, intent in list(
        STATE["pending"].items()
    ):

        if (
            int(time.time())
            - int(intent.get("last_attempt", 0))
            < RETRY_SECONDS
        ):
            continue

        try:

            # Check whether Delta accepted the previous attempt.
            try:

                existing = get_order_by_client_id(client_id)

                if isinstance(existing, dict) and existing.get("id"):

                    order_id = str(existing["id"])

                    STATE["orders"][order_id] = {
                        "client_order_id": client_id,
                        "side": intent["side"],
                        "price": str(intent["price"]),
                        "size": int(intent["size"]),
                        "filled_processed": 0,
                        "role": intent["role"]
                    }

                    STATE["pending"].pop(client_id, None)

                    save_state()

                    log(
                        f"RECOVERED EXISTING ORDER | {order_id}"
                    )

                    continue

            except Exception as lookup_error:

                message = str(lookup_error).lower()

                if (
                    "404" not in message
                    and "not found" not in message
                    and "order_not_found" not in message
                ):
                    raise

            intent["last_attempt"] = int(time.time())

            save_state()

            place_order(intent)

        except Exception as error:

            log(
                f"ORDER RETRY | {client_id} | {error}"
            )


# ============================================================
# PROCESS FILLS AND PARTIAL FILLS
# ============================================================

def process_orders():

    for order_id, info in list(
        STATE["orders"].items()
    ):

        try:

            order = get_order_by_id(order_id)

            status = str(
                order.get("state", "")
            ).lower()

            total = int(
                order.get("size", info["size"])
            )

            remaining = order.get("unfilled_size")

            if remaining is None:
                raise RuntimeError(
                    "Missing unfilled_size for " + order_id
                )

            filled = total - int(remaining)

            if filled < 0 or filled > total:
                raise RuntimeError("Invalid filled quantity")

            already_processed = int(
                info.get("filled_processed", 0)
            )

            new_fill = filled - already_processed

            if new_fill > 0:

                fill_price = order.get(
                    "average_fill_price"
                )

                if fill_price is None:
                    fill_price = order.get(
                        "limit_price",
                        info["price"]
                    )

                fill_price = round_price(fill_price)

                if info["side"] == "sell":

                    counter_side = "buy"

                    counter_price = (
                        fill_price - COUNTER_STEP
                    )

                else:

                    counter_side = "sell"

                    counter_price = (
                        fill_price + COUNTER_STEP
                    )

                # Save fill progress before submitting its counter.
                info["filled_processed"] = filled

                save_state()

                cid = counter_client_id(
                    order_id,
                    filled
                )

                queue_order(
                    counter_side,
                    counter_price,
                    new_fill,
                    "COUNTER",
                    cid
                )

                log(
                    f"FILL DETECTED | {info['side'].upper()} "
                    f"{new_fill} lots @ ${price_text(fill_price)} "
                    f"| Counter {counter_side.upper()} "
                    f"@ ${price_text(counter_price)}"
                )

            if status in (
                "cancelled",
                "canceled",
                "rejected",
                "expired"
            ):

                unfilled = total - filled

                if unfilled > 0:

                    retry_id = new_client_id()

                    queue_order(
                        info["side"],
                        info["price"],
                        unfilled,
                        "RETRY",
                        retry_id
                    )

                    log(
                        f"FAILED ORDER RECOVERY | "
                        f"{order_id} | Remaining={unfilled}"
                    )

                if status in (
                    "cancelled",
                    "canceled",
                    "rejected",
                    "expired"
                ):
                    info["terminal"] = True

                    save_state()

        except Exception as error:

            log(
                f"ORDER RECOVERY ERROR | "
                f"{order_id} | {error}"
            )


# ============================================================
# RECONCILE MISSING ORDERS
# ============================================================

def reconcile_orders():

    live = get_bot_open_orders()

    live_ids = set(live.keys())

    for order_id, info in list(
        STATE["orders"].items()
    ):

        if info.get("terminal"):
            continue

        if order_id in live_ids:
            continue

        # Order disappeared from active orders.
        # Query its final state instead of assuming it filled.
        try:

            order = get_order_by_id(order_id)

            status = str(
                order.get("state", "")
            ).lower()

            if status in (
                "closed",
                "cancelled",
                "canceled",
                "rejected",
                "expired"
            ):
                continue

            if status in ("open", "pending"):
                log(
                    f"ORDER MISSING FROM ACTIVE LIST | "
                    f"{order_id} | State={status}"
                )

        except Exception as error:

            log(
                f"MISSING ORDER LOOKUP RETRY | "
                f"{order_id} | {error}"
            )


# ============================================================
# MAIN
# ============================================================

def main():

    global STATE

    os.makedirs(
        BASE_DIR,
        exist_ok=True
    )

    lock = open(LOCK_FILE, "w")

    try:

        fcntl.flock(
            lock,
            fcntl.LOCK_EX | fcntl.LOCK_NB
        )

    except BlockingIOError:

        raise RuntimeError(
            "DevenSOX is already running"
        )

    STATE = load_state()

    log("====================================")
    log("STARTING DEVENSOX")
    log("====================================")

    log(f"Secret: {SECRET_NAME}")
    log(f"Symbol: {SYMBOL}")
    log(f"Product ID: {PRODUCT_ID}")
    log(f"Max position: {MAX_POSITION}")
    log(f"Order size: {ORDER_SIZE}")
    log(f"Step: {STEP}")
    log(f"Counter step: {COUNTER_STEP}")

    log("SELL orders use reduce_only.")
    log("Manual inventory is included in account position.")
    log("Manual orders are never modified.")

    log(
        f"CURRENT CMP: ${price_text(get_cmp())}"
    )

    log(
        f"CURRENT NET POSITION: {get_position()} lots"
    )

    initialize_grid()

    log("DEVENSOX RUNNING")

    while True:

        try:

            process_orders()

            reconcile_orders()

            submit_pending()

            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:

            log("DEVENSOX STOPPED")
            break

        except Exception as error:

            log(
                f"MAIN LOOP ERROR | "
                f"{type(error).__name__}: {error}"
            )

            time.sleep(8)


if __name__ == "__main__":
    main()

