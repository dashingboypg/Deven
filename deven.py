import os
import json
import time
import uuid
import boto3
from decimal import Decimal, ROUND_HALF_UP
from delta_rest_client import DeltaRestClient

# ============================================================
# DEVEN - SOXLBUSD
# Adapted from dashingboypg/Bhavu/bhavu/bhavu.py
# ============================================================

SECRET_NAME = "Deven"
AWS_REGION = "ap-south-1"

BASE_URL = "https://api.india.delta.exchange"

SYMBOL = "SOXLBUSD"
PRODUCT_ID = 141832

STATE_FILE = os.path.expanduser(
    "~/delta-bot/deven/deven_state.json"
)

BOT_PREFIX = "DEV-"
POLL_SECONDS = 2


def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), "|", msg, flush=True)


# ============================================================
# AWS SECRETS MANAGER
# ============================================================

def load_secret():
    session = boto3.session.Session(region_name=AWS_REGION)
    sm = session.client("secretsmanager")

    response = sm.get_secret_value(SecretId=SECRET_NAME)
    data = json.loads(response["SecretString"])

    api_key = data.get("API_Key")
    api_secret = data.get("API_Secret_Key")

    if not api_key or not api_secret:
        raise RuntimeError(
            "API_Key or API_Secret_Key missing in Deven secret"
        )

    def number(key):
        value = str(data[key])
        value = value.replace("$", "").replace(",", "").strip()
        value = value.replace("lots", "").strip()
        return float(value)

    max_position = int(number("Max_Position"))
    order_size = int(number("Order_Size"))
    step = number("Step")
    counter_step = number("Counter_Step")

    if max_position <= 0 or order_size <= 0:
        raise RuntimeError("Invalid Max_Position or Order_Size")

    if step <= 0 or counter_step <= 0:
        raise RuntimeError("Invalid Step or Counter_Step")

    return (
        api_key,
        api_secret,
        max_position,
        order_size,
        step,
        counter_step
    )


API_KEY, API_SECRET, MAX_POSITION, ORDER_SIZE, STEP, COUNTER_STEP = load_secret()

log(
    f"SECRET CONFIG | Max_Position={MAX_POSITION} "
    f"| Order_Size={ORDER_SIZE} | Step={STEP} "
    f"| Counter_Step={COUNTER_STEP}"
)


# ============================================================
# DELTA CLIENT
# ============================================================

client = DeltaRestClient(
    base_url=BASE_URL,
    api_key=API_KEY,
    api_secret=API_SECRET,
    raise_for_status=True
)


# ============================================================
# STATE
# ============================================================

def default_state():
    return {
        "version": 1,
        "bot_position": 0,
        "next_sequence": 1,
        "initialized": False,
        "orders": {}
    }


def load_state():
    if not os.path.exists(STATE_FILE):
        return default_state()

    try:
        with open(STATE_FILE) as f:
            state = json.load(f)

        if not isinstance(state, dict):
            return default_state()

        state.setdefault("version", 1)
        state.setdefault("bot_position", 0)
        state.setdefault("next_sequence", 1)
        state.setdefault("initialized", False)
        state.setdefault("orders", {})

        return state

    except Exception as e:
        log(f"STATE LOAD ERROR: {e}")
        raise RuntimeError("Cannot safely load Deven state") from e


STATE = load_state()


def save_state():
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)

    temp = STATE_FILE + ".tmp"

    with open(temp, "w") as f:
        json.dump(STATE, f, indent=2)

    os.replace(temp, STATE_FILE)


# ============================================================
# PRICE
# ============================================================

def round_cmp(price):
    return int(
        Decimal(str(price)).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP
        )
    )


def price_string(price):
    if float(price).is_integer():
        return str(int(price))
    return f"{price:.2f}"


# ============================================================
# CMP
# ============================================================

def get_cmp():
    ticker = client.get_ticker(SYMBOL)

    if isinstance(ticker, dict):
        for key in (
            "close",
            "last_price",
            "mark_price",
            "spot_price"
        ):
            if ticker.get(key) is not None:
                return float(ticker[key])

    raise RuntimeError(f"Unable to read CMP: {ticker}")


# ============================================================
# ORDER ID
# ============================================================

def new_client_order_id(role):
    seq = STATE["next_sequence"]
    STATE["next_sequence"] += 1

    client_oid = (
        f"{BOT_PREFIX}{role}-{seq}-{uuid.uuid4().hex[:6]}"
    )

    save_state()
    return client_oid


# ============================================================
# OPEN ORDERS
# ============================================================

def get_live_orders():
    orders = client.get_live_orders()

    if orders is None:
        return []

    if isinstance(orders, dict):
        orders = orders.get("result", orders.get("orders", []))

    if not isinstance(orders, list):
        raise RuntimeError(f"Unexpected live orders: {orders}")

    return [
        order for order in orders
        if isinstance(order, dict)
        and str(order.get("product_id")) == str(PRODUCT_ID)
    ]


def get_bot_open_orders():
    result = {}

    for order in get_live_orders():
        client_oid = str(order.get("client_order_id", "") or "")
        order_id = order.get("id")

        if client_oid.startswith(BOT_PREFIX) and order_id is not None:
            result[str(order_id)] = order

    return result


# ============================================================
# PLACE LIMIT ORDER
# ============================================================

def place_limit(side, price, role, qty=None):
    if qty is None:
        qty = ORDER_SIZE

    price = float(price)
    qty = int(qty)

    if qty <= 0:
        raise RuntimeError("Invalid order quantity")

    client_oid = new_client_order_id(role)

    payload = {
        "product_id": PRODUCT_ID,
        "size": qty,
        "side": side,
        "order_type": "limit_order",
        "limit_price": price_string(price),
        "time_in_force": "gtc",
        "post_only": False,
        "reduce_only": False,
        "client_order_id": client_oid
    }

    log(
        f"PLACING {side.upper()} {qty} "
        f"@ ${price_string(price)} | {role}"
    )

    result = client.create_order(payload)

    if not isinstance(result, dict) or result.get("id") is None:
        raise RuntimeError(f"Order creation failed: {result}")

    order_id = str(result["id"])

    STATE["orders"][order_id] = {
        "role": role,
        "side": side,
        "price": price,
        "size": qty,
        "client_order_id": client_oid,
        "handled": False
    }

    save_state()

    log(f"ORDER CREATED | ID={order_id} | {client_oid}")

    return result


# ============================================================
# STARTUP RECOVERY
# ============================================================

def repair_initialization_state():
    if not STATE.get("initialized"):
        return

    live_orders = get_bot_open_orders()

    if live_orders:
        log(
            f"STARTUP RECOVERY | {len(live_orders)} LIVE DEV ORDERS "
            "| STATE PRESERVED"
        )
        return

    log("STARTUP RESET | No live DEV orders")

    STATE["initialized"] = False
    STATE["bot_position"] = 0
    STATE["orders"] = {}

    save_state()


# ============================================================
# INITIAL BUY GRID
# ============================================================

def initialize_grid():
    if STATE["initialized"]:
        return

    live_orders = get_bot_open_orders()

    if live_orders:
        log(
            f"RECOVERY | Found {len(live_orders)} existing DEV orders"
        )

        for order_id, order in live_orders.items():
            STATE["orders"].setdefault(
                str(order_id),
                {
                    "role": "adopted",
                    "side": order.get("side"),
                    "price": float(order.get("limit_price", 0)),
                    "size": int(order.get("size", 0)),
                    "client_order_id": order.get("client_order_id"),
                    "handled": False
                }
            )

        STATE["initialized"] = True
        save_state()
        return

    cmp = get_cmp()
    anchor = round_cmp(cmp)

    levels = MAX_POSITION // ORDER_SIZE

    log("========================================")
    log("DEVEN SOXLBUSD INITIAL GRID")
    log(f"CMP: {cmp}")
    log(f"ROUNDED CMP: {anchor}")
    log(f"MAX_POSITION: {MAX_POSITION}")
    log(f"ORDER_SIZE: {ORDER_SIZE}")
    log(f"GRID LEVELS: {levels}")
    log(f"STEP: {STEP}")
    log(f"COUNTER_STEP: {COUNTER_STEP}")
    log("========================================")

    # First BUY is CMP itself.
    # Remaining BUYs descend by Step.
    for level in range(levels):
        price = anchor - (level * STEP)

        if price <= 0:
            break

        place_limit(
            side="buy",
            price=price,
            role="B"
        )

        time.sleep(0.15)

    STATE["initialized"] = True
    save_state()

    log("INITIAL GRID COMPLETE")


# ============================================================
# ORDER STATUS
# ============================================================

def get_order_status(order_id):
    try:
        return client.get_order_by_id(int(order_id))
    except Exception as e:
        log(f"ORDER STATUS ERROR {order_id}: {e}")
        return None


# ============================================================
# BUY FILL -> SELL COUNTER
# ============================================================

def handle_buy_fill(order_id, info, order):
    buy_price = float(
        order.get(
            "average_fill_price",
            order.get("limit_price", info["price"])
        )
    )

    quantity = int(info["size"])

    STATE["bot_position"] += quantity

    sell_price = buy_price + COUNTER_STEP

    log(
        f"BUY FILLED | ID={order_id} "
        f"| PRICE={buy_price} | QTY={quantity}"
    )

    place_limit(
        side="sell",
        price=sell_price,
        role="S",
        qty=quantity
    )

    info["handled"] = True
    info["fill_price"] = buy_price

    save_state()


# ============================================================
# SELL FILL -> BUY COUNTER
# ============================================================

def handle_sell_fill(order_id, info, order):
    sell_price = float(
        order.get(
            "average_fill_price",
            order.get("limit_price", info["price"])
        )
    )

    quantity = int(info["size"])

    STATE["bot_position"] -= quantity

    if STATE["bot_position"] < 0:
        STATE["bot_position"] = 0

    buy_price = sell_price - COUNTER_STEP

    log(
        f"SELL FILLED | ID={order_id} "
        f"| PRICE={sell_price} | QTY={quantity}"
    )

    place_limit(
        side="buy",
        price=buy_price,
        role="B",
        qty=quantity
    )

    info["handled"] = True
    info["fill_price"] = sell_price

    save_state()


# ============================================================
# PROCESS TRACKED ORDERS
# ============================================================

def process_tracked_orders():
    for order_id, info in list(STATE["orders"].items()):
        if info.get("handled"):
            continue

        order = get_order_status(order_id)

        if not order:
            continue

        state = str(order.get("state", "")).lower()

        if state in ("open", "pending"):
            continue

        if state == "closed":
            side = str(info.get("side", "")).lower()

            if side == "buy":
                handle_buy_fill(order_id, info, order)

            elif side == "sell":
                handle_sell_fill(order_id, info, order)

        elif state in ("cancelled", "canceled"):
            log(f"ORDER CANCELLED | ID={order_id}")

            info["handled"] = True
            save_state()


# ============================================================
# MAIN
# ============================================================

def main():
    log("========================================")
    log("STARTING DEVEN SOXLBUSD")
    log(f"SECRET: {SECRET_NAME}")
    log(f"PRODUCT: {PRODUCT_ID}")
    log(f"MAX_POSITION: {MAX_POSITION}")
    log(f"ORDER_SIZE: {ORDER_SIZE}")
    log(f"STEP: {STEP}")
    log(f"COUNTER_STEP: {COUNTER_STEP}")
    log("========================================")

    cmp = get_cmp()
    log(f"DELTA API OK | CMP={cmp}")

    repair_initialization_state()
    initialize_grid()

    log("DEVEN RUNNING")

    while True:
        try:
            process_tracked_orders()
        except Exception as e:
            log(f"MAIN LOOP ERROR: {type(e).__name__}: {e}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
