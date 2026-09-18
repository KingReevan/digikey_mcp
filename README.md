# DigiKey MCP Server

A Model Context Protocol (MCP) server for DigiKey's Product Search API using FastMCP.

Runs over HTTP by default so it can be started by your own launcher alongside your
other services, and shared by Claude Desktop and your application at the same time.

## Requirements

- Python 3.10+ (see the note below about 3.14)
- uv package manager
- DigiKey API credentials (CLIENT_ID and CLIENT_SECRET)

## Setup

### 1. Install dependencies

```bash
uv sync -p 3.11
```

> **Why `-p 3.11`?** uv defaults to the newest Python it can find. On Python 3.14
> the pinned `pydantic-core` has no prebuilt wheel and falls back to a source
> build that fails (`PyO3's maximum supported version (3.13)`). Pinning to 3.11
> uses prebuilt wheels and installs cleanly.

### 2. Set up environment variables

Create a `.env` file in the project root:

```
CLIENT_ID=your_digikey_client_id
CLIENT_SECRET=your_digikey_client_secret
USE_SANDBOX=false
```

Set `USE_SANDBOX=true` to use DigiKey's sandbox environment. Sandbox and
production credentials are **not** interchangeable — production credentials
return `401 Invalid clientId` against the sandbox host, and vice versa.

### 3. Run the server

```bash
uv run python digikey_mcp_server.py
```

The server validates its credentials at startup and **exits with code 1** if they
are missing or rejected, so a misconfigured install fails visibly rather than at
first use.

## Configuration

All settings are environment variables (readable from `.env`).

| Variable | Default | Purpose |
| --- | --- | --- |
| `CLIENT_ID` | — | DigiKey client id (required) |
| `CLIENT_SECRET` | — | DigiKey client secret (required) |
| `USE_SANDBOX` | `true` | `true` uses the sandbox host, `false` production |
| `MCP_TRANSPORT` | `http` | `http` or `stdio` |
| `MCP_HOST` | `127.0.0.1` | Interface to bind |
| `MCP_PORT` | *(unset)* | Pin a port; unset means pick a free one |
| `MCP_PATH` | `/mcp/` | HTTP path the MCP endpoint is served on |
| `MCP_PORT_FILE` | `mcp_server.json` | Where connection details are written |

### Binding

The server binds to `127.0.0.1` — reachable only from the same machine. It holds
your DigiKey credentials and has **no authentication of its own**, so the machine
boundary is the only thing protecting it. Do not bind it to `0.0.0.0` unless you
have put authentication in front of it.

### Ports

With `MCP_PORT` unset the server asks the operating system for a free port, which
avoids clashes on machines you do not control. It then writes the details to
`mcp_server.json` next to the server:

```json
{
  "host": "127.0.0.1",
  "port": 49716,
  "path": "/mcp/",
  "url": "http://127.0.0.1:49716/mcp/",
  "pid": 27228,
  "transport": "http"
}
```

Your launcher should read `url` from this file rather than assuming a port.

Set `MCP_PORT` to pin a specific port instead; if it is already taken the server
exits with code 1 and a message naming the port, rather than starting somewhere
unexpected.

The file is removed on a clean shutdown. A force-killed or crashed server leaves
it behind, so treat it as a hint and confirm the process named in `pid` is still
alive before trusting it.

## Connecting Claude Desktop

Claude Desktop launches MCP servers as child processes (stdio). To point it at an
already-running HTTP server, use a stdio-to-HTTP bridge:

```json
{
  "mcpServers": {
    "digikey": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:49716/mcp/"]
    }
  }
}
```

Because the port is chosen at startup, either pin `MCP_PORT` so this URL stays
stable, or have your launcher read `mcp_server.json` and write the config entry
before Claude Desktop starts.

Newer Claude Desktop builds can also add an HTTP MCP server directly as a custom
connector, which skips the bridge. Check what your target version supports.

## Available Tools

### Search Methods
- `keyword_search(keywords, limit=5, manufacturer_id=None, category_id=None, search_options=None, sort_field=None, sort_order="Ascending")` - Search DigiKey products by keyword with sorting and filtering
- `search_manufacturers()` - Get all product manufacturers
- `search_categories()` - Get all product categories
- `search_product_substitutions(product_number, limit=10, search_options=None, exclude_marketplace=False)` - Find substitute products

### Product Details
- `product_details(product_number, manufacturer_id=None, customer_id="0")` - Get detailed product information
- `get_category_by_id(category_id)` - Get specific category details
- `get_product_media(product_number)` - Get product images, documents, and videos
- `get_product_pricing(product_number, customer_id="0", requested_quantity=1)` - Get pricing for a given quantity
- `get_digi_reel_pricing(product_number, requested_quantity, customer_id="0")` - Get DigiReel pricing

### Sort Options for keyword_search
Available sort fields:
- `Packaging` - Sort by packaging type
- `ProductStatus` - Sort by product status
- `DigiKeyProductNumber` - Sort by DigiKey part number
- `ManufacturerProductNumber` - Sort by manufacturer part number
- `Manufacturer` - Sort by manufacturer name
- `MinimumQuantity` - Sort by minimum order quantity
- `QuantityAvailable` - Sort by available quantity
- `Price` - Sort by price
- `Supplier` - Sort by supplier
- `PriceManufacturerStandardPackage` - Sort by manufacturer standard package price

Sort orders: `Ascending` or `Descending`

### Search Options
Available filters for search methods:
- `LeadFree` - Lead-free products only
- `RoHSCompliant` - RoHS compliant products only
- `InStock` - In-stock products only
- `HasDatasheet` - Products with datasheets
- `HasProductPhoto` - Products with photos
- `Has3DModel` - Products with 3D models
- `NewProduct` - New products only

## Example Usage

### Search Examples

```python
# Basic keyword search
keyword_search("resistor", limit=10)

# Search with sorting by price (lowest first)
keyword_search("capacitor", limit=5, sort_field="Price", sort_order="Ascending")

# Narrow to one manufacturer and category
# (296 = Texas Instruments, 700 = Analog to Digital Converters)
keyword_search("analog to digital converter", manufacturer_id="296",
               category_id="700", search_options="HasDatasheet", limit=50)

# Get product details - part numbers containing "/" are handled
product_details("ADC0804LCN/NOPB")

# Get pricing for specific quantity
get_product_pricing("296-1395-5-ND", requested_quantity=100)
```

Use `search_manufacturers()` and `search_categories()` to discover the ids that
`manufacturer_id` and `category_id` expect.

## Behaviour notes

**Access tokens renew themselves.** DigiKey's tokens last about 10 minutes. The
server caches one, refreshes it a minute before expiry, and if a request is
rejected with a 401 anyway it fetches a fresh token and retries once. A
long-running server keeps working.

**Obsolete parts have no pricing.** DigiKey answers the pricing endpoints with a
server-side error rather than a clean 404 for discontinued parts. The server
turns that into a readable message pointing you at `product_details` to check the
product status.

**Errors carry DigiKey's own message.** A failed call reports the status code and
DigiKey's description (for example `Requested Product ... Not Found`) instead of a
bare HTTP error.
