import atexit
import json
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlencode

import requests
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
USE_SANDBOX = os.getenv("USE_SANDBOX", "true").lower() == "true"

# Transport / networking. Bound to loopback by default: the server holds DigiKey
# credentials and has no auth of its own, so the machine boundary is the security.
TRANSPORT = os.getenv("MCP_TRANSPORT", "http").lower()
HOST = os.getenv("MCP_HOST", "127.0.0.1")
PINNED_PORT = os.getenv("MCP_PORT")  # optional; unset means pick a free port
HTTP_PATH = os.getenv("MCP_PATH", "/mcp/")
PORT_FILE = Path(os.getenv("MCP_PORT_FILE", Path(__file__).with_name("mcp_server.json")))

# DigiKey OAuth2 token endpoint
if USE_SANDBOX:
    TOKEN_URL = "https://sandbox-api.digikey.com/v1/oauth2/token"
    API_BASE = "https://sandbox-api.digikey.com"
else:
    TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
    API_BASE = "https://api.digikey.com"

# Refresh this many seconds before the token actually expires, so a request
# already in flight cannot be caught out by the boundary.
TOKEN_EXPIRY_MARGIN = 60
REQUEST_TIMEOUT = 30

# Initialize FastMCP server
mcp = FastMCP("DigiKey MCP Server")


class _TokenManager:
    """Holds the DigiKey access token and renews it before it expires.

    DigiKey's client_credentials tokens last ~10 minutes, so a long-running
    server cannot fetch one at startup and hold it forever.
    """

    def __init__(self):
        self._token = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def get(self, force_refresh: bool = False) -> str:
        with self._lock:
            if not force_refresh and self._token and time.time() < self._expires_at:
                return self._token
            return self._fetch()

    def _fetch(self) -> str:
        """Caller must hold the lock."""
        if not CLIENT_ID or not CLIENT_SECRET:
            raise ToolError(
                "DigiKey credentials are not configured. Set CLIENT_ID and "
                "CLIENT_SECRET in the .env file next to the server."
            )

        endpoint = "SANDBOX" if USE_SANDBOX else "PRODUCTION"
        logger.info(f"Requesting {endpoint} access token (client id {CLIENT_ID[:8]}...)")

        try:
            resp = requests.post(
                TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ToolError(f"Could not reach DigiKey to obtain a token: {exc}") from exc

        if resp.status_code != 200:
            logger.error(f"OAuth error: {resp.status_code} - {resp.text}")
            raise ToolError(
                f"DigiKey rejected the credentials ({resp.status_code}): "
                f"{_error_detail(resp)}. Check CLIENT_ID/CLIENT_SECRET and whether "
                f"they are {endpoint.lower()} credentials."
            )

        payload = resp.json()
        self._token = payload["access_token"]
        expires_in = int(payload.get("expires_in", 600))
        self._expires_at = time.time() + max(expires_in - TOKEN_EXPIRY_MARGIN, 30)
        logger.info(f"Access token obtained (valid {expires_in}s)")
        return self._token


_tokens = _TokenManager()


def _error_detail(resp) -> str:
    """Pull a human-readable message out of a DigiKey error response."""
    try:
        body = resp.json()
    except ValueError:
        return (resp.text or "").strip()[:300] or "no details"
    for key in ("ErrorMessage", "detail", "title", "error_description", "error"):
        value = body.get(key)
        if value:
            extra = body.get("ErrorDetails")
            suffix = f" ({extra})" if extra else ""
            return f"{value}{suffix}"
    return json.dumps(body)[:300]


def _get_headers(customer_id: str = "0", force_refresh: bool = False):
    """Get standard headers for DigiKey API requests."""
    return {
        "Authorization": f"Bearer {_tokens.get(force_refresh=force_refresh)}",
        "X-DIGIKEY-Client-Id": CLIENT_ID,
        "Content-Type": "application/json",
        "X-DIGIKEY-Locale-Site": "US",
        "X-DIGIKEY-Locale-Language": "en",
        "X-DIGIKEY-Locale-Currency": "USD",
        "X-DIGIKEY-Customer-Id": customer_id,
    }


def _path(product_number: str) -> str:
    """Escape a value going into the URL path.

    Part numbers routinely contain a slash (e.g. ADC0804LCN/NOPB); dropped in
    raw it splits the path and DigiKey answers 404.
    """
    return quote(str(product_number).strip(), safe="")


def _query(params: dict) -> str:
    """Build a query string, dropping empties and lowercasing booleans."""
    clean = {}
    for key, value in params.items():
        if value is None:
            continue
        clean[key] = str(value).lower() if isinstance(value, bool) else value
    return f"?{urlencode(clean)}" if clean else ""


def _request(method: str, url: str, data: dict | None = None, customer_id: str = "0") -> dict:
    """Make an API request, renewing the token once if it is rejected."""
    logger.info(f"{method} {url}")

    resp: requests.Response
    for attempt in range(2):
        headers = _get_headers(customer_id, force_refresh=(attempt == 1))
        try:
            if method.upper() == "GET":
                resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            else:
                resp = requests.post(url, headers=headers, json=data, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise ToolError(f"Could not reach DigiKey: {exc}") from exc

        # A 401 mid-flight means the token lapsed; refresh and try once more.
        if resp.status_code == 401 and attempt == 0:
            logger.warning("DigiKey returned 401 - refreshing token and retrying")
            continue
        break

    logger.info(f"Response status: {resp.status_code}")
    if resp.status_code != 200:
        detail = _error_detail(resp)
        logger.error(f"API error: {resp.status_code} - {detail}")
        # DigiKey throws a server-side null reference rather than a clean 404
        # when a product has no record for the endpoint - obsolete parts have
        # no pricing, for instance. Say so instead of surfacing the raw fault.
        if resp.status_code == 500 and "NullReference" in detail:
            raise ToolError(
                f"DigiKey has no data for this product at this endpoint "
                f"(it returned a server-side error). This commonly means the "
                f"part is obsolete or discontinued; check product_details for "
                f"its status. Raw error: {detail[:120]}"
            )
        raise ToolError(f"DigiKey request failed ({resp.status_code}): {detail}")

    return resp.json()


@mcp.tool()
def keyword_search(keywords: str, limit: int = 5, manufacturer_id: str | None = None, category_id: str | None = None, search_options: str | None = None, sort_field: str | None = None, sort_order: str = "Ascending"):
    """Search DigiKey products by keyword.

    Args:
        keywords: Search terms or part numbers
        limit: Maximum number of results (default: 5)
        manufacturer_id: Filter by specific manufacturer ID
        category_id: Filter by specific category ID
        search_options: Comma-delimited filters like LeadFree,RoHSCompliant,InStock
        sort_field: Field to sort by. Options: None, Packaging, ProductStatus, DigiKeyProductNumber, ManufacturerProductNumber, Manufacturer, MinimumQuantity, QuantityAvailable, Price, Supplier, PriceManufacturerStandardPackage
        sort_order: Sort direction - Ascending or Descending (default: Ascending)
    """
    url = f"{API_BASE}/products/v4/search/keyword"

    body = {
        "Keywords": keywords,
        "Limit": limit,
    }

    # Filters belong inside FilterOptionsRequest as arrays of {"Id": ...}.
    # At the top level DigiKey ignores them silently and returns everything.
    filters = {}
    if manufacturer_id:
        filters["ManufacturerFilter"] = [{"Id": str(manufacturer_id)}]
    if category_id:
        filters["CategoryFilter"] = [{"Id": str(category_id)}]
    if search_options:
        filters["SearchOptions"] = [o.strip() for o in search_options.split(",") if o.strip()]
    if filters:
        body["FilterOptionsRequest"] = filters

    if sort_field:
        body["SortOptions"] = {
            "Field": sort_field,
            "SortOrder": sort_order,
        }

    return _request("POST", url, body)


@mcp.tool()
def product_details(product_number: str, manufacturer_id: str | None = None, customer_id: str = "0"):
    """Get detailed information for a specific product.

    Args:
        product_number: DigiKey or manufacturer part number
        manufacturer_id: Optional manufacturer ID for disambiguation
        customer_id: Customer ID for pricing (default: "0")
    """
    url = f"{API_BASE}/products/v4/search/{_path(product_number)}/productdetails"
    url += _query({"manufacturerId": manufacturer_id})
    return _request("GET", url, customer_id=customer_id)


@mcp.tool()
def search_manufacturers():
    """Search and retrieve all product manufacturers."""
    return _request("GET", f"{API_BASE}/products/v4/search/manufacturers")


@mcp.tool()
def search_categories():
    """Search and retrieve all product categories."""
    return _request("GET", f"{API_BASE}/products/v4/search/categories")


@mcp.tool()
def get_category_by_id(category_id: int):
    """Get specific category details by ID.

    Args:
        category_id: The category ID to retrieve
    """
    return _request("GET", f"{API_BASE}/products/v4/search/categories/{category_id}")


@mcp.tool()
def search_product_substitutions(product_number: str, limit: int = 10, search_options: str | None = None, exclude_marketplace: bool = False):
    """Search for product substitutions for a given product.

    Args:
        product_number: The product to get substitutions for
        limit: Number of substitutions (default: 10)
        search_options: Filters like LeadFree,RoHSCompliant,InStock
        exclude_marketplace: Exclude marketplace products (default: False)
    """
    url = f"{API_BASE}/products/v4/search/{_path(product_number)}/substitutions"
    url += _query({
        "limit": limit,
        "excludeMarketPlaceProducts": exclude_marketplace,
        "searchOptionList": search_options,
    })
    return _request("GET", url)


@mcp.tool()
def get_product_media(product_number: str):
    """Get media (images, documents, videos) for a product.

    Args:
        product_number: The product to get media for
    """
    return _request("GET", f"{API_BASE}/products/v4/search/{_path(product_number)}/media")


@mcp.tool()
def get_product_pricing(product_number: str, customer_id: str = "0", requested_quantity: int = 1):
    """Get detailed pricing information for a product.

    Args:
        product_number: The product to get pricing for
        customer_id: Customer ID for pricing (default: "0")
        requested_quantity: Quantity for pricing calculation (default: 1)
    """
    url = (
        f"{API_BASE}/products/v4/search/{_path(product_number)}"
        f"/pricingbyquantity/{requested_quantity}"
    )
    return _request("GET", url, customer_id=customer_id)


@mcp.tool()
def get_digi_reel_pricing(product_number: str, requested_quantity: int, customer_id: str = "0"):
    """Get DigiReel pricing for a product.

    Args:
        product_number: DigiKey product number (must be DigiReel compatible)
        requested_quantity: Quantity for DigiReel pricing
        customer_id: Customer ID for pricing (default: "0")
    """
    url = f"{API_BASE}/products/v4/search/{_path(product_number)}/digireelpricing"
    url += _query({"requestedQuantity": requested_quantity})
    return _request("GET", url, customer_id=customer_id)


def _free_port(host: str) -> int:
    """Ask the OS for an unused port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def _write_port_file(host: str, port: int) -> None:
    """Record where the server is listening so a launcher can find it."""
    details = {
        "host": host,
        "port": port,
        "path": HTTP_PATH,
        "url": f"http://{host}:{port}{HTTP_PATH}",
        "pid": os.getpid(),
        "transport": TRANSPORT,
    }
    try:
        PORT_FILE.write_text(json.dumps(details, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning(f"Could not write {PORT_FILE}: {exc}")
        return

    logger.info(f"Listening on {details['url']}")
    logger.info(f"Connection details written to {PORT_FILE}")
    atexit.register(_remove_port_file)


def _remove_port_file() -> None:
    try:
        PORT_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def main():
    logger.info("=== STARTING DIGIKEY MCP SERVER ===")
    logger.info(f"Environment: {'SANDBOX' if USE_SANDBOX else 'PRODUCTION'} ({API_BASE})")

    # Validate credentials up front and refuse to start if they are unusable,
    # so a misconfigured install fails visibly rather than at first use.
    try:
        _tokens.get()
    except ToolError as exc:
        logger.error(f"Cannot start: {exc}")
        sys.exit(1)

    if TRANSPORT == "stdio":
        logger.info("=== SERVER READY (stdio) ===")
        mcp.run()
        return

    if PINNED_PORT:
        port = int(PINNED_PORT)
        if not _port_is_free(HOST, port):
            logger.error(
                f"Cannot start: port {port} on {HOST} is already in use. "
                f"Free it, choose another with MCP_PORT, or unset MCP_PORT to "
                f"let the server pick one."
            )
            sys.exit(1)
    else:
        port = _free_port(HOST)

    _write_port_file(HOST, port)
    logger.info("=== SERVER READY (http) ===")
    mcp.run(transport="http", host=HOST, port=port, path=HTTP_PATH)


if __name__ == "__main__":
    main()
