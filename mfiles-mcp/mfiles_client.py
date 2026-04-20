# -*- coding: utf-8 -*-
"""
M-Files REST API Client for MCP Server (Async Version)

Handles authentication, token caching, and API requests with retry logic.
Uses httpx for true async HTTP operations.
"""

import json
import logging
import os
import sys
import time
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
import httpx

# Configure logging to stderr (required for MCP servers)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger('mfiles_client')

# Property IDs from M-Files
PROPERTY_IDS = {
    # General
    "Name": 0,
    "Klasse": 100,
    "WorkflowStatus": 39,

    # Unit properties
    "Einheitennummer": 1280,
    "Bezeichnung": 1279,
    "Einheitentyp": 1300,
    "Einheitenstatus": 1298,
    "Miete_netto": 1282,
    "Miete_netto_projektiert": 1306,
    "Wohnflaeche": 1287,
    "Mieter": 1269,
    "Betriebskosten": 1284,
    "Heizkosten": 1285,
    "MwSt": 1286,
    "QmPreis": 1288,
    "Warmmiete": 1358,
    "Miete_brutto": 1283,

    # Hierarchy
    "Liegenschaften": 1265,
    "Liegenschaften_Mortgage": 1266,
    "Besitzer": 1241,
    "Portfolio": 1264,
    "Portfolio_Company": 1263,

    # Gebäudedaten
    "Grundstuecksflaeche": 1291,
    "Anzahl_Parkplaetze": 1319,
    "Baujahr": 1290,
    "Hausverwaltung": 1321,
    "Hausmeister": 1351,

    # Contract/Lease properties
    "Vermietung": 1431,
    "Vertragsabschluss": 1200,
    "Laufzeit_Beginn": 1191,
    "Laufzeit_Ende": 1192,
    "Mietzeitoptionen": 1370,
    "Mietzeitoptionen_Verlaengerung": 1507,

    # Mortgage properties
    "Darlehenssumme": 1537,
    "Betrag_netto": 1206,
    "Zinsen": 1209,
    "Tilgung": 1210,
    "Zahlungsintervall": 1214,
    "Zinsbindung": 1538,
    "Restwert_netto": 1539,
    "Darlehensstand_Datum": 1540,
    "Vertragsgeber": 1237,
    "Vertragsnummer": 1190,
    "Darlehenstyp": 1543,
    "Abbuchungsdatum": 1545,

    # Invoice/Document properties
    "Rechnungsbetrag_brutto": 1246,
    "Rechnungsdatum": 1053,
    "Rechnungssteller": 1114,

    # Unit projection flag
    "Projektierte_Nutzen": 1546,
}

# Object types
COMPANY_OBJECT_TYPE = "127"
PROPERTY_OBJECT_TYPE = "130"
UNIT_OBJECT_TYPE = "132"
MORTGAGE_CLASS_ID = 26
MIETVERTRAG_TYPE = "9"
DOCUMENT_TYPE = "0"

# Status values
VACANT_STATUSES = ["leer", "gekündigt"]
SOLD_STATUS = "verkauft"


def classify_unit_type(einheitentyp: str, bezeichnung: str = "", einheitennummer: str = "") -> str:
    """Klassifiziert Einheiten konsistent mit den Generatoren."""
    unit_type = einheitentyp.lower() if einheitentyp else ""
    bez = bezeichnung.lower() if bezeichnung else ""
    nr = einheitennummer.lower() if einheitennummer else ""

    if "keller" in unit_type or "lager" in unit_type:
        return "keller"
    if "stellplatz" in unit_type or "garage" in unit_type:
        if any(x in unit_type or x in bez or x in nr for x in ["fahrrad", "rad", "e-bike", "ebike"]):
            return "ebike"
        return "parking"
    return "main"


def should_include_in_rent(unit_class: str) -> bool:
    """Prüft ob Einheit in Miete-Berechnung einfließt."""
    return unit_class in ["main", "parking"]


class RetryableAPIError(Exception):
    """Raised when we get a retryable API error like 403"""
    pass


class MFilesClient:
    """M-Files REST API Client with async httpx, token caching, data caching, and retry logic"""

    CACHE_TTL = 300  # 5 minutes

    def __init__(self, config_path: Optional[str] = None):
        """Initialize the client with config from file or environment variables.

        Environment variables (checked first, for Docker Desktop MCP Toolkit):
          MFILES_SERVER_URL, MFILES_VAULT_GUID, MFILES_USERNAME, MFILES_PASSWORD

        Falls back to config file if env vars are not set.
        """
        env_url = os.environ.get("MFILES_SERVER_URL")
        env_vault = os.environ.get("MFILES_VAULT_GUID")
        env_user = os.environ.get("MFILES_USERNAME")
        env_pass = os.environ.get("MFILES_PASSWORD")

        if env_url and env_vault and env_user and env_pass:
            logger.info("Using M-Files credentials from environment variables")
            self.config = {"mfiles": {
                "server_url": env_url,
                "vault_guid": env_vault,
                "username": env_user,
                "password": env_pass,
            }}
        else:
            if config_path is None:
                config_path = os.environ.get(
                    "MFILES_CONFIG_PATH",
                    str(Path.home() / ".claude" / "skills" / "immobilien-analyst" / "config.json")
                )
            self.config = self._load_config(config_path)

        self.server_url = self.config['mfiles']['server_url']
        self.vault_guid = self.config['mfiles']['vault_guid']
        self.username = self.config['mfiles']['username']
        self.password = self.config['mfiles']['password']

        self.token: Optional[str] = None
        self.headers: Dict[str, str] = {}
        self._client: Optional[httpx.AsyncClient] = None

        # Data cache
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._property_props_cache: Dict[int, Tuple[float, Any]] = {}
        self._unit_cache: Dict[int, Tuple[float, List]] = {}
        self._mortgage_cache: Dict[int, Tuple[float, List]] = {}

        # Token refresh protection (async-safe)
        self._token_refresh_lock: Optional[asyncio.Lock] = None
        self._last_token_refresh_time: float = 0.0
        self._TOKEN_REFRESH_COOLDOWN: float = 5.0  # seconds

    async def get_client(self) -> httpx.AsyncClient:
        """Get or create async HTTP client"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    def _get_token_refresh_lock(self) -> asyncio.Lock:
        """Get or create the token refresh lock (lazy initialization)"""
        if self._token_refresh_lock is None:
            self._token_refresh_lock = asyncio.Lock()
        return self._token_refresh_lock

    async def close(self):
        """Close the HTTP client"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def _get_cached(self, cache_key: str) -> Optional[Any]:
        """Get cached data if not expired"""
        if cache_key in self._cache:
            timestamp, data = self._cache[cache_key]
            if time.time() - timestamp < self.CACHE_TTL:
                return data
            del self._cache[cache_key]
        return None

    def _set_cached(self, cache_key: str, data: Any) -> None:
        """Cache data with timestamp"""
        self._cache[cache_key] = (time.time(), data)

    def clear_cache(self) -> None:
        """Clear all caches"""
        self._cache.clear()
        self._property_props_cache.clear()
        self._unit_cache.clear()
        self._mortgage_cache.clear()
        logger.info("All caches cleared")

    def _load_config(self, config_path: str) -> Dict:
        """Load configuration from JSON file"""
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load config from {config_path}: {e}")
            raise

    async def authenticate(self) -> bool:
        """Authenticate with M-Files and get token"""
        auth_url = f"{self.server_url}/server/authenticationtokens"
        auth_data = {
            "Username": self.username,
            "Password": self.password,
            "VaultGuid": self.vault_guid
        }

        try:
            client = await self.get_client()
            response = await client.post(auth_url, json=auth_data)
            response.raise_for_status()

            self.token = response.json().get('Value')
            self.headers = {
                'Content-Type': 'application/json',
                'X-Authentication': self.token
            }
            logger.info("M-Files authentication successful")

            # Verify token works with a lightweight endpoint
            try:
                verify_url = f'/valuelists?p={self.vault_guid}&limit=1'
                client = await self.get_client()
                verify_resp = await client.get(
                    f'{self.server_url}{verify_url}',
                    headers=self.headers,
                    timeout=10.0
                )
                if verify_resp.status_code != 200:
                    logger.warning(f"Token verification returned {verify_resp.status_code}, token may be invalid")
                    return False
            except Exception as ve:
                logger.warning(f"Token verification failed: {ve}")

            return True

        except Exception as e:
            logger.error(f"M-Files authentication failed: {e}")
            return False

    async def ensure_authenticated(self):
        """Ensure we have a valid token"""
        if not self.token:
            if not await self.authenticate():
                raise RuntimeError("Failed to authenticate with M-Files")

    async def refresh_token(self) -> bool:
        """Refresh authentication token with async-safe locking and cooldown.

        This method ensures that when multiple concurrent requests receive 403 errors,
        only one actually refreshes the token while others wait and reuse the result.
        """
        lock = self._get_token_refresh_lock()

        async with lock:
            # Check if token was refreshed recently by another coroutine
            time_since_last = time.time() - self._last_token_refresh_time
            if time_since_last < self._TOKEN_REFRESH_COOLDOWN:
                logger.info(f"Token refreshed {time_since_last:.1f}s ago by another request, reusing")
                return True  # Token is already fresh from another coroutine

            # Actually refresh the token
            logger.info("Refreshing authentication token...")
            self.token = None
            success = await self.authenticate()

            if success:
                self._last_token_refresh_time = time.time()
                logger.info("Token refresh successful")
            else:
                logger.error("Token refresh failed")

            return success

    async def _request_with_retry(self, method: str, endpoint: str, attempt: int = 0, **kwargs) -> httpx.Response:
        """Make an API request with manual retry logic for async"""
        max_attempts = 3

        await self.ensure_authenticated()

        url = f"{self.server_url}{endpoint}"
        if '?' in url:
            url += f"&p={self.vault_guid}"
        else:
            url += f"?p={self.vault_guid}"

        try:
            client = await self.get_client()
            response = await client.request(method, url, headers=self.headers, **kwargs)

            if response.status_code == 403:
                if attempt < max_attempts - 1:
                    logger.warning(f"Got 403, refreshing token (attempt {attempt + 1})...")
                    if await self.refresh_token():
                        await asyncio.sleep(2 ** attempt)  # Exponential backoff
                        return await self._request_with_retry(method, endpoint, attempt + 1, **kwargs)
                raise RetryableAPIError("Token refresh failed after max attempts")

            return response

        except (httpx.ConnectError, httpx.TimeoutException) as e:
            if attempt < max_attempts - 1:
                logger.warning(f"Connection error, retrying (attempt {attempt + 1})...")
                await asyncio.sleep(2 ** attempt)
                return await self._request_with_retry(method, endpoint, attempt + 1, **kwargs)
            raise

    async def get(self, endpoint: str, **kwargs) -> Optional[Any]:
        """Async GET request"""
        try:
            response = await self._request_with_retry('GET', endpoint, **kwargs)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"GET {endpoint} failed: {e}")
            return None

    # =========================================================================
    # Write Operations (Status Changes, Comments)
    # =========================================================================

    async def put(self, endpoint: str, json_data=None, **kwargs):
        """Async PUT request for updating object properties and workflow states."""
        await self.ensure_authenticated()
        try:
            response = await self._request_with_retry('PUT', endpoint, json=json_data, **kwargs)
            if response.status_code in (200, 204):
                try:
                    return response.json()
                except Exception:
                    return True
            else:
                logger.error(f"PUT {endpoint} failed: {response.status_code} {response.text[:300]}")
                return None
        except Exception as e:
            logger.error(f"PUT {endpoint} error: {e}")
            return None

    async def put_with_details(self, endpoint: str, json_data=None, **kwargs) -> dict:
        """Async PUT request that returns error details instead of swallowing them."""
        await self.ensure_authenticated()
        try:
            response = await self._request_with_retry('PUT', endpoint, json=json_data, **kwargs)
            if response.status_code in (200, 204):
                try:
                    return {'ok': True, 'data': response.json()}
                except Exception:
                    return {'ok': True}
            else:
                error_text = response.text[:500]
                logger.error(f"PUT {endpoint} failed: {response.status_code} {error_text}")
                return {'ok': False, 'status_code': response.status_code, 'error': error_text}
        except Exception as e:
            logger.error(f"PUT {endpoint} error: {e}")
            return {'ok': False, 'error': str(e)}

    async def post_json(self, endpoint: str, json_data=None, **kwargs):
        """Async POST request with JSON body for search queries and batch operations."""
        await self.ensure_authenticated()
        try:
            response = await self._request_with_retry('POST', endpoint, json=json_data, **kwargs)
            if response.status_code in (200, 201, 204):
                try:
                    return response.json()
                except Exception:
                    return True
            else:
                logger.error(f"POST {endpoint} failed: {response.status_code} {response.text[:300]}")
                return None
        except Exception as e:
            logger.error(f"POST {endpoint} error: {e}")
            return None

    async def batch_fetch_properties(self, obj_vers: list, chunk_size: int = 50) -> list:
        """Fetch properties for multiple objects in batches using POST /objects/properties.

        This is significantly more efficient than fetching properties one-by-one
        when dealing with many objects (e.g., all mortgages or units).

        Args:
            obj_vers: List of dicts with Type, ID, Version keys (from ObjVer in search results)
            chunk_size: Number of objects per batch request (default 50)
        Returns:
            List of property arrays (one per object), or empty list on failure
        """
        await self.ensure_authenticated()
        endpoint = f'/objects/properties'
        all_results = []

        for chunk_start in range(0, len(obj_vers), chunk_size):
            chunk = obj_vers[chunk_start:chunk_start + chunk_size]
            try:
                result = await self.post_json(endpoint, json_data=chunk)
                if result is not None:
                    all_results.extend(result)
                    logger.debug(
                        f"Batch properties: {chunk_start + 1}-{chunk_start + len(chunk)} "
                        f"of {len(obj_vers)} loaded"
                    )
                else:
                    logger.error(
                        f"Batch properties failed for chunk {chunk_start + 1}-{chunk_start + len(chunk)}"
                    )
            except Exception as e:
                logger.error(f"Batch properties error: {e}")

        return all_results

    async def get_view_items(self, view_id: int, limit: int = 1000, max_items: int = 2000) -> dict:
        """Fetch items from an M-Files view with pagination.

        Args:
            view_id: M-Files View ID (e.g. 117)
            limit: Items per page
            max_items: Maximum total items to fetch
        Returns:
            Dict with 'items' list and metadata
        """
        await self.ensure_authenticated()
        all_items = []
        endpoint = f'/views/v{view_id}/items?limit={limit}'
        resp = await self.get(endpoint)
        if not resp:
            return {'items': [], 'total': 0, 'more_results': False}

        items = resp.get('Items', []) if isinstance(resp, dict) else resp
        all_items.extend(items)
        more = resp.get('MoreResults', False) if isinstance(resp, dict) else False

        while more and len(all_items) < max_items:
            offset = len(all_items)
            resp = await self.get(f'/views/v{view_id}/items?limit={limit}&s={offset}')
            if not resp:
                break
            new_items = resp.get('Items', []) if isinstance(resp, dict) else resp
            if not new_items:
                break
            all_items.extend(new_items)
            more = resp.get('MoreResults', False) if isinstance(resp, dict) else False

        logger.info(f"View {view_id}: loaded {len(all_items)} items (more={more})")
        return {
            'items': all_items,
            'total': len(all_items),
            'more_results': more
        }

    async def set_workflow_status(self, object_type: int, object_id: int, state_id: int,
                                  workflow_id: int = None) -> dict:
        """Set workflow state on an M-Files object using the dedicated /workflowstate endpoint.

        This uses PUT /objects/{type}/{id}/latest/workflowstate which only changes the
        workflow state without touching other properties. Much safer than PUT /properties
        which replaces ALL properties and requires mandatory fields (Name, Class).

        Args:
            object_type: M-Files object type (139=Vorgaenge, 0=Dokument)
            object_id: Object ID in M-Files
            state_id: Target workflow state ID
            workflow_id: Optional workflow ID (110=Sanierung, 113=Angebotspruefung).
                        If not provided, reads current workflow from object.
        Returns:
            dict with 'ok' (bool) and optional 'error' (str) with M-Files error details
        """
        await self.ensure_authenticated()

        # If no workflow_id provided, read current workflow from object
        if workflow_id is None:
            current_wf = await self.get(f'/objects/{object_type}/{object_id}/latest/workflowstate')
            if current_wf and 'WorkflowID' in current_wf:
                workflow_id = current_wf['WorkflowID']

        payload = {"StateID": state_id}
        if workflow_id is not None:
            payload["WorkflowID"] = workflow_id

        endpoint = f'/objects/{object_type}/{object_id}/latest/workflowstate'
        result = await self.put_with_details(endpoint, json_data=payload)
        if result['ok']:
            logger.info(f"Workflow state set: ObjType={object_type} ID={object_id} -> State={state_id}")
        else:
            logger.error(f"Failed to set workflow state: ObjType={object_type} ID={object_id} State={state_id} Error={result.get('error')}")
        return result

    async def add_comment(self, object_type: int, object_id: int, comment: str) -> bool:
        """Add a comment to an M-Files object using POST (not PUT) to preserve existing properties."""
        await self.ensure_authenticated()
        properties = [{"PropertyDef": 33, "TypedValue": {"DataType": 1, "Value": comment}}]
        endpoint = f'/objects/{object_type}/{object_id}/latest/properties'
        # Use POST instead of PUT: POST only overwrites sent properties, PUT replaces ALL
        result = await self.post_json(endpoint, json_data=properties)
        if result is not None:
            logger.info(f"Comment added: ObjType={object_type} ID={object_id}")
            return True
        else:
            logger.error(f"Failed to add comment: ObjType={object_type} ID={object_id}")
            return False

    async def get_object_title(self, object_type: int, object_id: int) -> str:
        """Get the title (Name-oder-Titel, PropDef 0) of an M-Files object."""
        try:
            props = await self.get(f'/objects/{object_type}/{object_id}/latest/properties')
            for p in (props or []):
                if p.get('PropertyDef') == 0:
                    return p.get('TypedValue', {}).get('DisplayValue', '?')
            return '?'
        except Exception:
            return '?'

    # =========================================================================
    # Portfolio Methods
    # =========================================================================

    async def get_all_portfolios(self) -> List[Dict[str, Any]]:
        """Fetch all unique portfolios from M-Files via Company objects"""
        logger.info("Fetching all portfolios...")

        companies = await self.get(f"/objects/{COMPANY_OBJECT_TYPE}")
        if not companies:
            return []

        items = companies.get('Items', []) if isinstance(companies, dict) else companies
        portfolios_dict: Dict[str, int] = {}

        for item in items:
            company_id = item.get('ObjVer', {}).get('ID') or item.get('ID')
            if not company_id:
                continue

            props = await self.get(f"/objects/{COMPANY_OBJECT_TYPE}/{company_id}/latest/properties")
            if not props:
                continue

            for prop in props:
                if prop.get('PropertyDef') == PROPERTY_IDS["Portfolio_Company"]:
                    typed_value = prop.get('TypedValue', {})
                    portfolio_name = None

                    if 'Lookup' in typed_value and typed_value['Lookup']:
                        portfolio_name = typed_value['Lookup'].get('DisplayValue', '')
                    elif 'Lookups' in typed_value and typed_value['Lookups']:
                        for lookup in typed_value['Lookups']:
                            pn = lookup.get('DisplayValue', '')
                            if pn:
                                portfolios_dict[pn] = portfolios_dict.get(pn, 0)
                    else:
                        portfolio_name = typed_value.get('DisplayValue', '')

                    if portfolio_name:
                        portfolios_dict[portfolio_name] = portfolios_dict.get(portfolio_name, 0)

        return [{'name': name, 'property_count': 0} for name in sorted(portfolios_dict.keys())]

    async def get_portfolio_companies(self, portfolio_name: str) -> List[int]:
        """Get company IDs belonging to a portfolio"""
        companies = await self.get(f"/objects/{COMPANY_OBJECT_TYPE}")
        if not companies:
            return []

        items = companies.get('Items', []) if isinstance(companies, dict) else companies
        company_ids = []

        for item in items:
            company_id = item.get('ObjVer', {}).get('ID') or item.get('ID')
            if not company_id:
                continue

            props = await self.get(f"/objects/{COMPANY_OBJECT_TYPE}/{company_id}/latest/properties")
            if not props:
                continue

            for prop in props:
                if prop.get('PropertyDef') == PROPERTY_IDS["Portfolio_Company"]:
                    typed_value = prop.get('TypedValue', {})
                    found_portfolio = ''

                    if 'Lookup' in typed_value and typed_value['Lookup']:
                        found_portfolio = typed_value['Lookup'].get('DisplayValue', '')
                    elif 'DisplayValue' in typed_value:
                        found_portfolio = typed_value.get('DisplayValue', '')

                    if found_portfolio == portfolio_name:
                        company_ids.append(company_id)
                        break

        return company_ids

    async def get_portfolio_properties(self, portfolio_name: str) -> List[Dict[str, Any]]:
        """Get all properties for a specific portfolio"""
        logger.info(f"Fetching properties for portfolio: {portfolio_name}")

        company_ids = await self.get_portfolio_companies(portfolio_name)
        if not company_ids:
            logger.warning(f"No companies found for portfolio '{portfolio_name}'")
            return []

        properties_data = await self.get(f"/objects/{PROPERTY_OBJECT_TYPE}")
        if not properties_data:
            return []

        items = properties_data.get('Items', []) if isinstance(properties_data, dict) else properties_data
        portfolio_properties = []

        for item in items:
            prop_id = item.get('ObjVer', {}).get('ID') or item.get('ID')
            prop_name = item.get('Title', '') or item.get('EscapedTitleWithID', '')

            if not prop_id:
                continue

            props = await self.get(f"/objects/{PROPERTY_OBJECT_TYPE}/{prop_id}/latest/properties")
            if not props:
                continue

            for prop in props:
                if prop.get('PropertyDef') == PROPERTY_IDS["Besitzer"]:
                    typed_value = prop.get('TypedValue', {})
                    owner_id = None

                    if 'Lookup' in typed_value and typed_value['Lookup']:
                        owner_id = typed_value['Lookup'].get('Item', 0)
                    elif 'Value' in typed_value:
                        owner_id = typed_value.get('Value', 0)

                    if owner_id in company_ids:
                        portfolio_properties.append({
                            'id': prop_id,
                            'name': prop_name
                        })
                        break

        logger.info(f"Found {len(portfolio_properties)} properties for portfolio '{portfolio_name}'")
        return portfolio_properties

    # =========================================================================
    # Property Methods
    # =========================================================================

    async def get_all_properties(self) -> List[Dict[str, Any]]:
        """Get all properties (Liegenschaften)"""
        properties_data = await self.get(f"/objects/{PROPERTY_OBJECT_TYPE}")
        if not properties_data:
            return []

        items = properties_data.get('Items', []) if isinstance(properties_data, dict) else properties_data
        return [
            {
                'id': item.get('ObjVer', {}).get('ID') or item.get('ID'),
                'name': item.get('Title', '') or item.get('EscapedTitleWithID', '')
            }
            for item in items
            if item.get('ObjVer', {}).get('ID') or item.get('ID')
        ]

    async def find_property_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Find a property by name (fuzzy matching)"""
        all_props = await self.get_all_properties()
        name_lower = name.lower()

        # Exact match first
        for prop in all_props:
            if prop['name'].lower() == name_lower:
                return prop

        # Partial match
        for prop in all_props:
            if name_lower in prop['name'].lower():
                return prop

        return None

    async def get_property_portfolio(self, property_id: int) -> str:
        """Get the portfolio name for a property"""
        props = await self.get(f"/objects/{PROPERTY_OBJECT_TYPE}/{property_id}/latest/properties")
        if not props:
            return ""

        for prop in props:
            if prop.get('PropertyDef') == PROPERTY_IDS["Besitzer"]:
                typed_value = prop.get('TypedValue', {})
                owner_id = None

                if 'Lookup' in typed_value and typed_value['Lookup']:
                    owner_id = typed_value['Lookup'].get('Item', 0)

                if owner_id:
                    company_props = await self.get(f"/objects/{COMPANY_OBJECT_TYPE}/{owner_id}/latest/properties")
                    if company_props:
                        for cprop in company_props:
                            if cprop.get('PropertyDef') == PROPERTY_IDS["Portfolio_Company"]:
                                tv = cprop.get('TypedValue', {})
                                if 'Lookup' in tv and tv['Lookup']:
                                    return tv['Lookup'].get('DisplayValue', '')
                                return tv.get('DisplayValue', '')

        return ""

    # =========================================================================
    # Unit Methods
    # =========================================================================

    async def get_property_units(self, property_id: int) -> List[Dict[str, Any]]:
        """Get all units for a property (CACHED)"""
        # Check cache
        if property_id in self._unit_cache:
            timestamp, data = self._unit_cache[property_id]
            if time.time() - timestamp < self.CACHE_TTL:
                logger.debug(f"Cache HIT for units of property {property_id}")
                return data

        logger.info(f"Fetching units for property {property_id} (cache miss)...")

        search_url = f"/objects/{UNIT_OBJECT_TYPE}?p{PROPERTY_IDS['Liegenschaften']}={property_id}&include=properties"
        units_data = await self.get(search_url)

        if not units_data:
            return []

        items = units_data.get('Items', []) if isinstance(units_data, dict) else units_data
        units = []

        for item in items:
            unit_id = item.get('ObjVer', {}).get('ID') or item.get('ID')
            if not unit_id:
                continue

            props = item.get('Properties', item.get('PropertyValues', []))
            if props:
                unit_info = self._parse_unit_properties(unit_id, props)
            else:
                unit_info = await self._get_unit_details(unit_id)

            if unit_info:
                units.append(unit_info)

        logger.info(f"Found {len(units)} units for property {property_id}")
        self._unit_cache[property_id] = (time.time(), units)
        return units

    async def _get_unit_details(self, unit_id: int) -> Optional[Dict[str, Any]]:
        """Get detailed information for a unit"""
        props = await self.get(f"/objects/{UNIT_OBJECT_TYPE}/{unit_id}/latest/properties")
        if not props:
            return None
        return self._parse_unit_properties(unit_id, props)

    def _parse_unit_properties(self, unit_id: int, props: List[Dict]) -> Optional[Dict[str, Any]]:
        """Parse unit properties from M-Files property list"""
        unit_data = {"id": unit_id}

        for prop in props:
            prop_id = prop.get('PropertyDef')
            typed_value = prop.get('TypedValue', {})
            display_value = typed_value.get('DisplayValue', '')
            value = typed_value.get('Value')

            if prop_id == PROPERTY_IDS["Name"]:
                unit_data["unit_name"] = display_value

            elif prop_id == PROPERTY_IDS["Einheitennummer"]:
                unit_data["unit_number"] = display_value

            elif prop_id == PROPERTY_IDS["Bezeichnung"]:
                unit_data["bezeichnung"] = display_value

            elif prop_id == PROPERTY_IDS["Einheitentyp"]:
                unit_data["unit_type"] = display_value

            elif prop_id == PROPERTY_IDS["Einheitenstatus"]:
                status = display_value.lower() if display_value else ""
                unit_data["status"] = display_value
                unit_data["is_vacant"] = status in VACANT_STATUSES
                unit_data["is_sold"] = status == SOLD_STATUS

            elif prop_id == PROPERTY_IDS["Miete_netto"]:
                unit_data["net_rent"] = self._parse_decimal(value if value is not None else display_value)

            elif prop_id == PROPERTY_IDS["Miete_netto_projektiert"]:
                unit_data["net_rent_projected"] = self._parse_decimal(value if value is not None else display_value)

            elif prop_id == PROPERTY_IDS["Wohnflaeche"]:
                unit_data["area_sqm"] = self._parse_decimal(value if value is not None else display_value)

            elif prop_id == PROPERTY_IDS["Mieter"]:
                if 'Lookup' in typed_value and typed_value['Lookup']:
                    unit_data["tenant"] = typed_value['Lookup'].get('DisplayValue', '')
                elif 'Lookups' in typed_value and typed_value['Lookups']:
                    names = [l.get('DisplayValue', '') for l in typed_value['Lookups'] if l.get('DisplayValue')]
                    unit_data["tenant"] = "; ".join(names)
                else:
                    unit_data["tenant"] = display_value

            elif prop_id == PROPERTY_IDS["Betriebskosten"]:
                unit_data["betriebskosten"] = self._parse_decimal(value if value is not None else display_value)

            elif prop_id == PROPERTY_IDS["Heizkosten"]:
                unit_data["heizkosten"] = self._parse_decimal(value if value is not None else display_value)

            elif prop_id == PROPERTY_IDS["Miete_brutto"]:
                unit_data["bruttomiete"] = self._parse_decimal(value if value is not None else display_value)

            elif prop_id == PROPERTY_IDS["Warmmiete"]:
                unit_data["warmmiete"] = self._parse_decimal(value if value is not None else display_value)

            elif prop_id == PROPERTY_IDS["QmPreis"]:
                unit_data["rent_per_sqm"] = self._parse_decimal(value if value is not None else display_value)

            elif prop_id == PROPERTY_IDS["Laufzeit_Beginn"]:
                unit_data["lease_start"] = self._format_date(display_value)

            elif prop_id == PROPERTY_IDS["Laufzeit_Ende"]:
                lease_end = self._format_date(display_value)
                if not lease_end or lease_end == "" or "unbegr" in str(display_value).lower():
                    unit_data["lease_end"] = "unbegr."
                else:
                    unit_data["lease_end"] = lease_end

            elif prop_id == PROPERTY_IDS["Mietzeitoptionen"]:
                unit_data["has_option"] = bool(display_value and display_value.strip())
                unit_data["option_details"] = display_value

            elif prop_id == PROPERTY_IDS["WorkflowStatus"]:
                if 'Lookup' in typed_value and typed_value['Lookup']:
                    unit_data["workflow_status"] = str(typed_value['Lookup'].get('Item', ''))
                    unit_data["workflow_status_label"] = typed_value['Lookup'].get('DisplayValue', '')
                else:
                    unit_data["workflow_status"] = str(value) if value else ''
                    unit_data["workflow_status_label"] = display_value

            elif prop_id == PROPERTY_IDS["Projektierte_Nutzen"]:
                display_lower = display_value.lower() if display_value else ''
                unit_data["projektierte_nutzen"] = (
                    value == True or
                    display_lower in ['true', 'ja', 'yes', '1']
                )

        # Classify unit
        unit_type = unit_data.get("unit_type", "")
        bezeichnung = unit_data.get("bezeichnung", "")
        einheitennummer = unit_data.get("unit_number", "")
        unit_data["unit_class"] = classify_unit_type(unit_type, bezeichnung, einheitennummer)
        unit_data["is_parking"] = unit_data["unit_class"] == "parking"
        unit_data["is_keller"] = unit_data["unit_class"] == "keller"
        unit_data["is_ebike"] = unit_data["unit_class"] == "ebike"
        unit_data["include_in_rent"] = should_include_in_rent(unit_data["unit_class"])

        if unit_data.get("is_sold", False):
            return None

        return unit_data

    # =========================================================================
    # Mortgage Methods
    # =========================================================================

    async def get_property_mortgages(self, property_id: int, property_name: str = "") -> List[Dict[str, Any]]:
        """Get all active mortgages for a property (CACHED)"""
        if property_id in self._mortgage_cache:
            timestamp, data = self._mortgage_cache[property_id]
            if time.time() - timestamp < self.CACHE_TTL:
                logger.debug(f"Cache HIT for mortgages of property {property_id}")
                return data

        logger.info(f"Fetching mortgages for property {property_id} (cache miss)...")

        search_url = f"/objects?p{PROPERTY_IDS['Klasse']}={MORTGAGE_CLASS_ID}&include=properties"
        mortgages_data = await self.get(search_url)

        if not mortgages_data:
            return []

        items = mortgages_data.get('Items', []) if isinstance(mortgages_data, dict) else mortgages_data
        property_mortgages = []

        for item in items:
            if 'ObjVer' not in item:
                continue

            mortgage_id = item['ObjVer']['ID']
            mortgage_type = item['ObjVer']['Type']

            props = item.get('Properties', item.get('PropertyValues', []))
            if props:
                mortgage_info = self._parse_mortgage_properties(mortgage_id, props)
            else:
                mortgage_info = await self._get_mortgage_details(mortgage_id, mortgage_type)

            if not mortgage_info:
                continue

            status = mortgage_info.get('status', '').lower()
            if 'aktiv' not in status:
                continue

            if mortgage_info.get('outstanding_balance', 0) <= 0:
                continue

            linked_props = mortgage_info.get('linked_properties', [])
            if property_name and property_name in linked_props:
                property_mortgages.append(mortgage_info)
                logger.debug(f"Mortgage {mortgage_info.get('id')} matched for property '{property_name}'")

        logger.info(f"Found {len(property_mortgages)} active mortgages for property {property_id}")
        self._mortgage_cache[property_id] = (time.time(), property_mortgages)
        return property_mortgages

    async def _get_mortgage_details(self, mortgage_id: int, mortgage_type: int) -> Optional[Dict[str, Any]]:
        """Get detailed information for a mortgage"""
        props = await self.get(f"/objects/{mortgage_type}/{mortgage_id}/latest/properties")
        if not props:
            return None
        return self._parse_mortgage_properties(mortgage_id, props)

    def _parse_mortgage_properties(self, mortgage_id: int, props: List[Dict]) -> Optional[Dict[str, Any]]:
        """Parse mortgage properties from M-Files property list"""
        mortgage_data = {"id": mortgage_id}

        for prop in props:
            prop_id = prop.get('PropertyDef')
            typed_value = prop.get('TypedValue', {})
            display_value = typed_value.get('DisplayValue', '')
            value = typed_value.get('Value')

            if prop_id == PROPERTY_IDS["Darlehenssumme"]:
                mortgage_data['loan_amount'] = self._parse_decimal(value if value is not None else display_value)

            elif prop_id == PROPERTY_IDS["Restwert_netto"]:
                mortgage_data['outstanding_balance'] = self._parse_decimal(value if value is not None else display_value)

            elif prop_id == PROPERTY_IDS["Betrag_netto"]:
                mortgage_data['payment_amount'] = self._parse_decimal(value if value is not None else display_value)

            elif prop_id == PROPERTY_IDS["Zinsen"]:
                mortgage_data['interest_rate'] = self._parse_decimal(value if value is not None else display_value)

            elif prop_id == PROPERTY_IDS["Tilgung"]:
                mortgage_data['amortization_rate'] = self._parse_decimal(value if value is not None else display_value)

            elif prop_id == PROPERTY_IDS["Zahlungsintervall"]:
                if 'Lookup' in typed_value and typed_value['Lookup']:
                    mortgage_data['payment_interval'] = typed_value['Lookup'].get('DisplayValue', '')
                else:
                    mortgage_data['payment_interval'] = display_value

            elif prop_id == PROPERTY_IDS["Laufzeit_Ende"]:
                mortgage_data['end_date'] = self._format_date(display_value)

            elif prop_id == PROPERTY_IDS["Vertragsgeber"]:
                if 'Lookup' in typed_value and typed_value['Lookup']:
                    mortgage_data['bank'] = typed_value['Lookup'].get('DisplayValue', '')
                else:
                    mortgage_data['bank'] = display_value

            elif prop_id == PROPERTY_IDS["WorkflowStatus"]:
                if 'Lookup' in typed_value and typed_value['Lookup']:
                    mortgage_data['status'] = typed_value['Lookup'].get('DisplayValue', '')
                else:
                    mortgage_data['status'] = display_value

            elif prop_id == PROPERTY_IDS["Liegenschaften_Mortgage"]:
                if 'Lookups' in typed_value and typed_value['Lookups']:
                    mortgage_data['linked_property_ids'] = [l.get('Item') for l in typed_value['Lookups'] if l.get('Item')]
                    mortgage_data['linked_properties'] = [l.get('DisplayValue', '') for l in typed_value['Lookups']]
                elif 'Lookup' in typed_value and typed_value['Lookup']:
                    mortgage_data['linked_property_ids'] = [typed_value['Lookup'].get('Item')] if typed_value['Lookup'].get('Item') else []
                    mortgage_data['linked_properties'] = [typed_value['Lookup'].get('DisplayValue', '')]
                else:
                    mortgage_data['linked_property_ids'] = []
                    mortgage_data['linked_properties'] = []

            elif prop_id == PROPERTY_IDS["Zinsbindung"]:
                mortgage_data['fixed_rate_until'] = self._format_date(display_value)

            elif prop_id == PROPERTY_IDS["Vertragsnummer"]:
                mortgage_data['contract_number'] = display_value

            elif prop_id == PROPERTY_IDS["Darlehenstyp"]:
                if 'Lookup' in typed_value and typed_value['Lookup']:
                    mortgage_data['loan_type'] = typed_value['Lookup'].get('DisplayValue', '')
                else:
                    mortgage_data['loan_type'] = display_value

            elif prop_id == PROPERTY_IDS["Darlehensstand_Datum"]:
                mortgage_data['balance_date'] = self._format_date(display_value)

            elif prop_id == PROPERTY_IDS["Abbuchungsdatum"]:
                mortgage_data['debit_date'] = display_value

            elif prop_id == PROPERTY_IDS["Laufzeit_Beginn"]:
                mortgage_data['start_date'] = self._format_date(display_value)

        if 'outstanding_balance' not in mortgage_data:
            mortgage_data['outstanding_balance'] = mortgage_data.get('loan_amount', 0)

        return mortgage_data

    async def get_all_mortgages(self) -> List[Dict[str, Any]]:
        """Get all active mortgages across all properties"""
        logger.info("Fetching all mortgages...")

        search_url = f"/objects?p{PROPERTY_IDS['Klasse']}={MORTGAGE_CLASS_ID}"
        mortgages_data = await self.get(search_url)

        if not mortgages_data:
            return []

        items = mortgages_data.get('Items', []) if isinstance(mortgages_data, dict) else mortgages_data
        all_mortgages = []

        for item in items:
            if 'ObjVer' not in item:
                continue

            mortgage_id = item['ObjVer']['ID']
            mortgage_type = item['ObjVer']['Type']

            mortgage_info = await self._get_mortgage_details(mortgage_id, mortgage_type)
            if not mortgage_info:
                continue

            status = mortgage_info.get('status', '').lower()
            if 'aktiv' not in status:
                continue

            if mortgage_info.get('outstanding_balance', 0) <= 0:
                continue

            all_mortgages.append(mortgage_info)

        logger.info(f"Found {len(all_mortgages)} active mortgages total")
        return all_mortgages

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _parse_decimal(self, value, default: float = 0.0) -> float:
        """Parse decimal value from various formats"""
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return float(value)
        if not isinstance(value, str):
            try:
                return float(str(value))
            except (ValueError, TypeError):
                return default

        cleaned = value.replace('€', '').replace('%', '').strip()

        if '.' in cleaned and ',' in cleaned:
            if cleaned.rfind('.') < cleaned.rfind(','):
                cleaned = cleaned.replace('.', '').replace(',', '.')
            else:
                cleaned = cleaned.replace(',', '')
        elif ',' in cleaned:
            cleaned = cleaned.replace(',', '.')

        try:
            return float(cleaned)
        except (ValueError, TypeError):
            return default

    def _format_date(self, date_str: str) -> Optional[str]:
        """Format date string to German format"""
        if not date_str:
            return None
        try:
            for fmt in ['%m/%d/%Y', '%Y-%m-%d', '%d.%m.%Y']:
                try:
                    date_obj = datetime.strptime(date_str.split(' ')[0], fmt)
                    return date_obj.strftime('%d.%m.%Y')
                except (ValueError, TypeError):
                    continue
            return date_str
        except (ValueError, TypeError):
            return date_str

    # =========================================================================
    # Document Methods
    # =========================================================================

    async def get_object_files(self, object_type: int, object_id: int) -> List[Dict[str, Any]]:
        """Get all files/documents attached to an M-Files object"""
        logger.info(f"Fetching files for object type {object_type}, id {object_id}...")

        files_data = await self.get(f"/objects/{object_type}/{object_id}/latest/files")
        if not files_data:
            return []

        files = []
        items = files_data if isinstance(files_data, list) else files_data.get('Items', [])

        for item in items:
            file_info = {
                'file_id': item.get('ID', 0),
                'name': item.get('Name', ''),
                'extension': item.get('Extension', ''),
                'size_bytes': item.get('Size', 0),
                'version': item.get('Version', 0),
                'object_type': object_type,
                'object_id': object_id,
            }

            if 'CreatedUtc' in item:
                file_info['created_date'] = self._format_date(item['CreatedUtc'])
            if 'LastModifiedUtc' in item:
                file_info['modified_date'] = self._format_date(item['LastModifiedUtc'])

            files.append(file_info)

        logger.info(f"Found {len(files)} files for object {object_type}/{object_id}")
        return files

    async def download_file(self, object_type: int, object_id: int, file_id: int,
                           save_path: Optional[str] = None) -> Tuple[Optional[bytes], Dict[str, Any]]:
        """Download a file from M-Files"""
        logger.info(f"Downloading file {file_id} from object {object_type}/{object_id}...")

        await self.ensure_authenticated()

        url = f"{self.server_url}/objects/{object_type}/{object_id}/latest/files/{file_id}/content?p={self.vault_guid}"

        try:
            client = await self.get_client()
            response = await client.get(url, headers=self.headers)
            response.raise_for_status()

            content = response.content
            content_type = response.headers.get('Content-Type', '')
            content_disposition = response.headers.get('Content-Disposition', '')

            filename = f"file_{file_id}"
            if 'filename=' in content_disposition:
                import re
                match = re.search(r'filename[*]?=["\']?([^"\';\n]+)', content_disposition)
                if match:
                    filename = match.group(1).strip()

            file_info = {
                'file_id': file_id,
                'filename': filename,
                'content_type': content_type,
                'size_bytes': len(content)
            }

            if save_path:
                with open(save_path, 'wb') as f:
                    f.write(content)
                file_info['saved_path'] = save_path
                logger.info(f"File saved to {save_path}")

            return content, file_info

        except Exception as e:
            logger.error(f"Failed to download file {file_id}: {e}")
            return None, {'error': str(e)}

    async def get_unit_files(self, unit_id: int) -> List[Dict[str, Any]]:
        """Get all files/documents for a unit"""
        return await self.get_object_files(int(UNIT_OBJECT_TYPE), unit_id)

    async def get_property_files(self, property_id: int) -> List[Dict[str, Any]]:
        """Get all files/documents for a property"""
        return await self.get_object_files(int(PROPERTY_OBJECT_TYPE), property_id)

    # =========================================================================
    # Contract/Mietvertrag Methods
    # =========================================================================

    async def get_unit_contracts(self, unit_id: int) -> List[Dict[str, Any]]:
        """Get all contract collections for a unit via Property 1431"""
        logger.info(f"Fetching contracts for unit {unit_id} via Property 1431...")

        props = await self.get(f"/objects/{UNIT_OBJECT_TYPE}/{unit_id}/latest/properties")
        if not props:
            return []

        contracts = []

        for prop in props:
            if prop.get('PropertyDef') == PROPERTY_IDS["Vermietung"]:
                typed_value = prop.get('TypedValue', {})

                if 'Lookups' in typed_value and typed_value['Lookups']:
                    for lookup in typed_value['Lookups']:
                        if lookup.get('ObjectType') == int(MIETVERTRAG_TYPE):
                            contract_id = lookup.get('Item')
                            contract_name = lookup.get('DisplayValue', '')
                            if contract_id:
                                contract_info = await self._get_contract_details(contract_id, contract_name)
                                if contract_info:
                                    contracts.append(contract_info)

                elif 'Lookup' in typed_value and typed_value['Lookup']:
                    lookup = typed_value['Lookup']
                    if lookup.get('ObjectType') == int(MIETVERTRAG_TYPE):
                        contract_id = lookup.get('Item')
                        contract_name = lookup.get('DisplayValue', '')
                        if contract_id:
                            contract_info = await self._get_contract_details(contract_id, contract_name)
                            if contract_info:
                                contracts.append(contract_info)
                break

        logger.info(f"Found {len(contracts)} contracts for unit {unit_id}")
        return contracts

    async def _get_contract_details(self, contract_id: int, contract_name: str = "") -> Optional[Dict[str, Any]]:
        """Get details for a contract collection including its documents"""
        logger.debug(f"Getting contract details for ID {contract_id}")

        props = await self.get(f"/objects/{MIETVERTRAG_TYPE}/{contract_id}/latest/properties")
        if not props:
            return None

        contract_data = {
            'id': contract_id,
            'name': contract_name,
            'type': 'contract_collection',
            'documents': []
        }

        for prop in props:
            prop_id = prop.get('PropertyDef')
            typed_value = prop.get('TypedValue', {})
            display_value = typed_value.get('DisplayValue', '')

            if prop_id == PROPERTY_IDS["Name"]:
                contract_data['name'] = display_value or contract_name

            elif prop_id == PROPERTY_IDS["Vertragsabschluss"]:
                contract_data['contract_date'] = self._format_date(display_value)

            elif prop_id == PROPERTY_IDS["Laufzeit_Beginn"]:
                contract_data['start_date'] = self._format_date(display_value)

            elif prop_id == PROPERTY_IDS["Laufzeit_Ende"]:
                contract_data['end_date'] = self._format_date(display_value)

            elif prop_id == 20:
                contract_data['created_date'] = self._format_date(display_value)

        contract_data['documents'] = await self.get_documents_in_collection(contract_id)
        return contract_data

    async def get_documents_in_collection(self, collection_id: int) -> List[Dict[str, Any]]:
        """Get all documents inside a contract collection"""
        logger.debug(f"Getting documents from collection ID: {collection_id}")
        documents = []

        # Check direct files
        try:
            files = await self.get(f"/objects/{MIETVERTRAG_TYPE}/{collection_id}/latest/files")
            if files:
                items = files if isinstance(files, list) else files.get('Items', [])
                for item in items:
                    doc = {
                        'file_id': item.get('ID', 0),
                        'name': item.get('Name', ''),
                        'extension': item.get('Extension', ''),
                        'size_bytes': item.get('Size', 0),
                        'object_type': int(MIETVERTRAG_TYPE),
                        'object_id': collection_id,
                        'version': item.get('Version', 0),
                    }
                    if doc['file_id']:
                        documents.append(doc)

                if documents:
                    logger.info(f"Found {len(documents)} files directly on collection {collection_id}")
                    return documents
        except Exception as e:
            logger.debug(f"No direct files on collection: {e}")

        # Try relationships
        try:
            rels = await self.get(f"/objects/{MIETVERTRAG_TYPE}/{collection_id}/latest/relationships")
            if rels:
                for rel in rels:
                    if isinstance(rel, dict):
                        obj_ver = rel.get('ObjVer', {})
                        if obj_ver.get('Type') == int(DOCUMENT_TYPE):
                            doc_id = obj_ver.get('ID')
                            if doc_id:
                                doc_files = await self.get(f"/objects/{DOCUMENT_TYPE}/{doc_id}/latest/files")
                                if doc_files:
                                    items = doc_files if isinstance(doc_files, list) else doc_files.get('Items', [])
                                    for item in items:
                                        doc = {
                                            'file_id': item.get('ID', 0),
                                            'name': item.get('Name', ''),
                                            'extension': item.get('Extension', ''),
                                            'size_bytes': item.get('Size', 0),
                                            'object_type': int(DOCUMENT_TYPE),
                                            'object_id': doc_id,
                                            'version': item.get('Version', 0),
                                            'source': 'relationship'
                                        }
                                        if doc['file_id']:
                                            documents.append(doc)

                if documents:
                    logger.info(f"Found {len(documents)} documents via relationships for collection {collection_id}")
                    return documents
        except Exception as e:
            logger.debug(f"No relationships found: {e}")

        # Try collectionmembers
        try:
            members = await self.get(f"/objects/{MIETVERTRAG_TYPE}/{collection_id}/collectionmembers")
            if members:
                items = members if isinstance(members, list) else members.get('Items', [])
                for item in items:
                    obj_ver = item.get('ObjVer', {})
                    doc_type = obj_ver.get('Type', 0)
                    doc_id = obj_ver.get('ID')

                    if doc_id and doc_type == int(DOCUMENT_TYPE):
                        doc_files = await self.get(f"/objects/{DOCUMENT_TYPE}/{doc_id}/latest/files")
                        if doc_files:
                            file_items = doc_files if isinstance(doc_files, list) else doc_files.get('Items', [])
                            for f in file_items:
                                doc = {
                                    'file_id': f.get('ID', 0),
                                    'name': f.get('Name', ''),
                                    'extension': f.get('Extension', ''),
                                    'size_bytes': f.get('Size', 0),
                                    'object_type': int(DOCUMENT_TYPE),
                                    'object_id': doc_id,
                                    'version': f.get('Version', 0),
                                    'source': 'collectionmember'
                                }
                                if doc['file_id']:
                                    documents.append(doc)

                if documents:
                    logger.info(f"Found {len(documents)} documents via collectionmembers for collection {collection_id}")
                    return documents
        except Exception as e:
            logger.debug(f"No collectionmembers found: {e}")

        logger.info(f"No documents found in collection {collection_id}")
        return documents

    async def get_unit_contract_documents(self, unit_id: int) -> List[Dict[str, Any]]:
        """Get all contract documents for a unit"""
        contracts = await self.get_unit_contracts(unit_id)
        all_documents = []

        for contract in contracts:
            contract_docs = contract.get('documents', [])
            for doc in contract_docs:
                doc['contract_id'] = contract.get('id')
                doc['contract_name'] = contract.get('name')
                doc['contract_date'] = contract.get('contract_date')
                all_documents.append(doc)

        return all_documents

    # =========================================================================
    # Version History Methods
    # =========================================================================

    async def get_object_history(self, object_type: int, object_id: int) -> List[Dict[str, Any]]:
        """Get version history for an M-Files object.

        Returns list of version dicts with ObjVer.Version, LastModifiedUtc, etc.
        """
        logger.info(f"Fetching history for object type {object_type}, id {object_id}...")

        result = await self.get(f"/objects/{object_type}/{object_id}/history")
        if not result:
            return []

        items = result if isinstance(result, list) else result.get('Items', [])
        versions = []

        for item in items:
            obj_ver = item.get('ObjVer', {})
            version_num = obj_ver.get('Version', 0)
            versions.append({
                'version': version_num,
                'last_modified_utc': item.get('LastModifiedUtc', ''),
                'object_type': obj_ver.get('Type', object_type),
                'object_id': obj_ver.get('ID', object_id),
            })

        versions.sort(key=lambda v: v['version'])
        logger.info(f"Found {len(versions)} versions for object {object_type}/{object_id}")
        return versions

    async def get_unit_version_history(self, unit_id: int) -> Dict[str, Any]:
        """Get full version history for a unit with parsed property changes.

        Fetches all versions and their properties, extracting key fields:
        - Mieter (1269), Einheitenstatus (1298), WorkflowStatus (39)
        - LastModified (21), LastModifiedBy (23)
        - Miete_netto (1282), Miete_netto_projektiert (1306)

        Returns dict with versions list and status_timeline.
        """
        logger.info(f"Fetching version history for unit {unit_id}...")

        # Get all versions
        versions = await self.get_object_history(int(UNIT_OBJECT_TYPE), unit_id)
        if not versions:
            logger.warning(f"No versions found for unit {unit_id}")
            return {'unit_id': unit_id, 'versions': [], 'total_versions': 0}

        parsed_versions = []

        for ver in versions:
            version_num = ver['version']
            props = await self.get(
                f"/objects/{UNIT_OBJECT_TYPE}/{unit_id}/{version_num}/properties"
            )
            if not props:
                parsed_versions.append({
                    'version': version_num,
                    'modified_date': self._format_date(ver.get('last_modified_utc', '')),
                })
                continue

            entry = self._parse_version_properties(version_num, props)
            # Use history-level timestamp as fallback
            if not entry.get('modified_date'):
                entry['modified_date'] = self._format_date(ver.get('last_modified_utc', ''))
            parsed_versions.append(entry)

        # Detect changes between adjacent versions
        for i in range(1, len(parsed_versions)):
            prev = parsed_versions[i - 1]
            curr = parsed_versions[i]
            changes = []

            if curr.get('tenant', '') != prev.get('tenant', ''):
                changes.append(f"Mieter: {prev.get('tenant', '') or '(leer)'} → {curr.get('tenant', '') or '(leer)'}")
            if curr.get('status', '') != prev.get('status', ''):
                changes.append(f"Status: {prev.get('status', '') or '(leer)'} → {curr.get('status', '') or '(leer)'}")
            if curr.get('net_rent', 0) != prev.get('net_rent', 0):
                changes.append(f"Miete netto: {prev.get('net_rent', 0)} → {curr.get('net_rent', 0)}")
            if curr.get('net_rent_projected', 0) != prev.get('net_rent_projected', 0):
                changes.append(f"Miete netto proj.: {prev.get('net_rent_projected', 0)} → {curr.get('net_rent_projected', 0)}")
            if curr.get('workflow_status', '') != prev.get('workflow_status', ''):
                changes.append(f"Workflow: {prev.get('workflow_status', '') or '(leer)'} → {curr.get('workflow_status', '') or '(leer)'}")

            curr['changes'] = changes

        # Build status timeline
        status_timeline = []
        prev_status = ""
        for ver_entry in parsed_versions:
            curr_status = ver_entry.get('status', '')
            if curr_status and curr_status != prev_status:
                status_timeline.append({
                    'date': ver_entry.get('modified_date', ''),
                    'from_status': prev_status,
                    'to_status': curr_status,
                    'tenant': ver_entry.get('tenant', ''),
                    'version': str(ver_entry.get('version', '')),
                })
                prev_status = curr_status

        logger.info(f"Parsed {len(parsed_versions)} versions, {len(status_timeline)} status changes for unit {unit_id}")
        return {
            'unit_id': unit_id,
            'versions': parsed_versions,
            'total_versions': len(parsed_versions),
            'status_timeline': status_timeline,
        }

    # =========================================================================
    # Object Type Discovery Methods
    # =========================================================================

    async def get_object_types(self) -> List[Dict[str, Any]]:
        """Discover all M-Files object types. Cached."""
        cache_key = "object_types"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        logger.info("Discovering M-Files object types...")
        result = await self.get("/structure/objecttypes")
        if not result:
            return []

        items = result if isinstance(result, list) else result.get('Items', [])
        object_types = []
        for item in items:
            object_types.append({
                'id': item.get('ID', 0),
                'name': item.get('Name', ''),
                'name_plural': item.get('NamePlural', ''),
                'real_object_type': item.get('RealObjectType', True),
            })

        self._set_cached(cache_key, object_types)
        logger.info(f"Discovered {len(object_types)} object types")
        return object_types

    async def find_object_type_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Find an object type by name (fuzzy matching)."""
        all_types = await self.get_object_types()
        name_lower = name.lower()

        # Exact match first
        for ot in all_types:
            if ot['name'].lower() == name_lower or ot['name_plural'].lower() == name_lower:
                return ot

        # Partial match
        for ot in all_types:
            if name_lower in ot['name'].lower() or name_lower in ot['name_plural'].lower():
                return ot

        return None

    async def _get_vorgang_type_id(self) -> Optional[int]:
        """Discover and cache the Vorgänge object type ID at runtime."""
        cache_key = "vorgang_type_id"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        # Try common names for Vorgänge
        for name in ["Vorgang", "Vorgänge", "Vorgnge", "Vorgaenge"]:
            ot = await self.find_object_type_by_name(name)
            if ot:
                type_id = ot['id']
                self._set_cached(cache_key, type_id)
                logger.info(f"Discovered Vorgänge object type ID: {type_id}")
                return type_id

        logger.warning("Could not discover Vorgänge object type")
        return None

    # =========================================================================
    # Vorgänge Methods
    # =========================================================================

    async def get_all_vorgaenge(self, property_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all Vorgänge objects from M-Files.

        Args:
            property_filter: Optional property name to filter Vorgänge by linked Liegenschaft.
        """
        type_id = await self._get_vorgang_type_id()
        if type_id is None:
            return []

        cache_key = f"vorgaenge_all_{property_filter or 'none'}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        logger.info("Fetching all Vorgänge...")
        data = await self.get(f"/objects/{type_id}")
        if not data:
            return []

        items = data.get('Items', []) if isinstance(data, dict) else data
        vorgaenge = []

        for item in items:
            obj_ver = item.get('ObjVer', {})
            vorgang_id = obj_ver.get('ID') or item.get('ID')
            if not vorgang_id:
                continue

            vorgang = {
                'id': vorgang_id,
                'name': item.get('Title', '') or item.get('EscapedTitleWithID', ''),
                'object_type': type_id,
            }
            vorgaenge.append(vorgang)

        # Fetch property details for each Vorgang in parallel. Sequential
        # fetch here (the original implementation) pushed list_vorgaenge
        # into 30s+ timeout territory whenever there were 50+ Vorgaenge,
        # triggering Hermes to mark the MCP unreachable on Telegram calls.
        summaries = await asyncio.gather(
            *(self._get_vorgang_summary(type_id, v['id'], v['name']) for v in vorgaenge),
            return_exceptions=True,
        )
        enriched: List[Dict[str, Any]] = []
        for details in summaries:
            if isinstance(details, Exception) or not details:
                continue
            if property_filter:
                linked = details.get('linked_properties', [])
                if not any(property_filter.lower() in lp.lower() for lp in linked):
                    continue
            enriched.append(details)
        vorgaenge = enriched

        self._set_cached(cache_key, vorgaenge)
        logger.info(f"Found {len(vorgaenge)} Vorgänge")
        return vorgaenge

    async def _get_vorgang_summary(self, type_id: int, vorgang_id: int, name: str = "") -> Optional[Dict[str, Any]]:
        """Get summary info for a Vorgang (lightweight, for list view)."""
        props = await self.get(f"/objects/{type_id}/{vorgang_id}/latest/properties")
        if not props:
            return None

        summary = {
            'id': vorgang_id,
            'name': name,
            'object_type': type_id,
            'class_name': '',
            'status': '',
            'workflow_status': '',
            'linked_properties': [],
            'linked_units': [],
            'linked_companies': [],
            'created_date': None,
            'modified_date': None,
        }

        for prop in props:
            prop_id = prop.get('PropertyDef')
            typed_value = prop.get('TypedValue', {})
            display_value = typed_value.get('DisplayValue', '')

            if prop_id == 0:  # Name
                summary['name'] = display_value or name
            elif prop_id == 100:  # Class
                summary['class_name'] = display_value
            elif prop_id == 39:  # WorkflowStatus
                if 'Lookup' in typed_value and typed_value['Lookup']:
                    summary['workflow_status'] = typed_value['Lookup'].get('DisplayValue', '')
                else:
                    summary['workflow_status'] = display_value
            elif prop_id == 20:  # Created
                summary['created_date'] = self._format_date(display_value)
            elif prop_id == 21:  # LastModified
                summary['modified_date'] = self._format_date(display_value)
            elif prop_id == PROPERTY_IDS.get("Liegenschaften", -1):
                summary['linked_properties'] = self._extract_lookup_names(typed_value)
            elif prop_id == PROPERTY_IDS.get("Liegenschaften_Mortgage", -1):
                # Also check mortgage-linked properties
                if not summary['linked_properties']:
                    summary['linked_properties'] = self._extract_lookup_names(typed_value)

            # Check for linked objects by object type in lookups
            if 'Lookups' in typed_value and typed_value['Lookups']:
                for lookup in typed_value['Lookups']:
                    obj_type = lookup.get('ObjectType')
                    disp = lookup.get('DisplayValue', '')
                    if obj_type == int(PROPERTY_OBJECT_TYPE) and disp:
                        if disp not in summary['linked_properties']:
                            summary['linked_properties'].append(disp)
                    elif obj_type == int(UNIT_OBJECT_TYPE) and disp:
                        if disp not in summary['linked_units']:
                            summary['linked_units'].append(disp)
                    elif obj_type == int(COMPANY_OBJECT_TYPE) and disp:
                        if disp not in summary['linked_companies']:
                            summary['linked_companies'].append(disp)
            elif 'Lookup' in typed_value and typed_value['Lookup']:
                lookup = typed_value['Lookup']
                obj_type = lookup.get('ObjectType')
                disp = lookup.get('DisplayValue', '')
                if obj_type == int(PROPERTY_OBJECT_TYPE) and disp:
                    if disp not in summary['linked_properties']:
                        summary['linked_properties'].append(disp)
                elif obj_type == int(UNIT_OBJECT_TYPE) and disp:
                    if disp not in summary['linked_units']:
                        summary['linked_units'].append(disp)
                elif obj_type == int(COMPANY_OBJECT_TYPE) and disp:
                    if disp not in summary['linked_companies']:
                        summary['linked_companies'].append(disp)

        return summary

    def _extract_lookup_names(self, typed_value: Dict) -> List[str]:
        """Extract display names from a Lookup or Lookups typed value."""
        names = []
        if 'Lookups' in typed_value and typed_value['Lookups']:
            for lookup in typed_value['Lookups']:
                name = lookup.get('DisplayValue', '')
                if name:
                    names.append(name)
        elif 'Lookup' in typed_value and typed_value['Lookup']:
            name = typed_value['Lookup'].get('DisplayValue', '')
            if name:
                names.append(name)
        return names

    async def get_vorgang_details(self, vorgang_id: int, include_documents: bool = False) -> Optional[Dict[str, Any]]:
        """Get full details of a Vorgang including ALL properties and linked entities.

        Parses every property generically — extracts name, display value, and
        identifies linked objects (Properties, Units, Companies) via Lookup fields.
        """
        type_id = await self._get_vorgang_type_id()
        if type_id is None:
            return None

        props = await self.get(f"/objects/{type_id}/{vorgang_id}/latest/properties")
        if not props:
            return None

        details = {
            'id': vorgang_id,
            'object_type': type_id,
            'name': '',
            'class_name': '',
            'status': '',
            'workflow_status': '',
            'all_properties': {},
            'linked_properties': [],
            'linked_units': [],
            'linked_companies': [],
            'linked_objects': [],  # All other linked objects
            'created_date': None,
            'modified_date': None,
            'created_by': '',
            'modified_by': '',
            'documents': [],
        }

        for prop in props:
            prop_id = prop.get('PropertyDef')
            prop_name = prop.get('PropertyDefName', f'Property_{prop_id}')
            typed_value = prop.get('TypedValue', {})
            display_value = typed_value.get('DisplayValue', '')
            value = typed_value.get('Value')
            data_type = typed_value.get('DataType', 0)

            # Store every property generically
            details['all_properties'][prop_name] = display_value

            # Extract well-known properties
            if prop_id == 0:  # Name
                details['name'] = display_value
            elif prop_id == 100:  # Class
                details['class_name'] = display_value
            elif prop_id == 39:  # WorkflowStatus
                if 'Lookup' in typed_value and typed_value['Lookup']:
                    details['workflow_status'] = typed_value['Lookup'].get('DisplayValue', '')
                else:
                    details['workflow_status'] = display_value
            elif prop_id == 38:  # Workflow
                details['all_properties']['Workflow'] = display_value
            elif prop_id == 20:  # Created
                details['created_date'] = self._format_date(display_value)
            elif prop_id == 21:  # LastModified
                details['modified_date'] = self._format_date(display_value)
            elif prop_id == 25:  # CreatedBy
                if 'Lookup' in typed_value and typed_value['Lookup']:
                    details['created_by'] = typed_value['Lookup'].get('DisplayValue', '')
                else:
                    details['created_by'] = display_value
            elif prop_id == 23:  # LastModifiedBy
                if 'Lookup' in typed_value and typed_value['Lookup']:
                    details['modified_by'] = typed_value['Lookup'].get('DisplayValue', '')
                else:
                    details['modified_by'] = display_value

            # Identify linked objects from Lookup fields
            if 'Lookups' in typed_value and typed_value['Lookups']:
                for lookup in typed_value['Lookups']:
                    obj_type = lookup.get('ObjectType')
                    disp = lookup.get('DisplayValue', '')
                    item_id = lookup.get('Item')
                    if obj_type == int(PROPERTY_OBJECT_TYPE) and disp:
                        if disp not in details['linked_properties']:
                            details['linked_properties'].append(disp)
                    elif obj_type == int(UNIT_OBJECT_TYPE) and disp:
                        if disp not in details['linked_units']:
                            details['linked_units'].append(disp)
                    elif obj_type == int(COMPANY_OBJECT_TYPE) and disp:
                        if disp not in details['linked_companies']:
                            details['linked_companies'].append(disp)
                    elif obj_type is not None and disp:
                        details['linked_objects'].append({
                            'object_type': obj_type,
                            'id': item_id,
                            'name': disp,
                            'property_name': prop_name,
                        })
            elif 'Lookup' in typed_value and typed_value['Lookup']:
                lookup = typed_value['Lookup']
                obj_type = lookup.get('ObjectType')
                disp = lookup.get('DisplayValue', '')
                item_id = lookup.get('Item')
                if obj_type == int(PROPERTY_OBJECT_TYPE) and disp:
                    if disp not in details['linked_properties']:
                        details['linked_properties'].append(disp)
                elif obj_type == int(UNIT_OBJECT_TYPE) and disp:
                    if disp not in details['linked_units']:
                        details['linked_units'].append(disp)
                elif obj_type == int(COMPANY_OBJECT_TYPE) and disp:
                    if disp not in details['linked_companies']:
                        details['linked_companies'].append(disp)
                elif obj_type is not None and disp:
                    details['linked_objects'].append({
                        'object_type': obj_type,
                        'id': item_id,
                        'name': disp,
                        'property_name': prop_name,
                    })

        if include_documents:
            details['documents'] = await self.get_vorgang_documents(vorgang_id)

        return details

    async def get_vorgang_documents(self, vorgang_id: int) -> List[Dict[str, Any]]:
        """Get all documents inside a Vorgang using 3-tier traversal.

        Checks: direct files → relationships → collectionmembers
        (same pattern as get_documents_in_collection).
        """
        type_id = await self._get_vorgang_type_id()
        if type_id is None:
            return []

        documents = []

        # Tier 1: Direct files on the Vorgang object
        try:
            files = await self.get(f"/objects/{type_id}/{vorgang_id}/latest/files")
            if files:
                items = files if isinstance(files, list) else files.get('Items', [])
                for item in items:
                    doc = {
                        'file_id': item.get('ID', 0),
                        'name': item.get('Name', ''),
                        'extension': item.get('Extension', ''),
                        'size_bytes': item.get('Size', 0),
                        'object_type': type_id,
                        'object_id': vorgang_id,
                        'version': item.get('Version', 0),
                        'source': 'direct',
                    }
                    if doc['file_id']:
                        documents.append(doc)

                if documents:
                    logger.info(f"Found {len(documents)} direct files on Vorgang {vorgang_id}")
                    return documents
        except Exception as e:
            logger.debug(f"No direct files on Vorgang: {e}")

        # Tier 2: Relationships
        try:
            rels = await self.get(f"/objects/{type_id}/{vorgang_id}/latest/relationships")
            if rels:
                for rel in rels:
                    if isinstance(rel, dict):
                        obj_ver = rel.get('ObjVer', {})
                        rel_type = obj_ver.get('Type')
                        rel_id = obj_ver.get('ID')
                        if rel_id and rel_type == int(DOCUMENT_TYPE):
                            doc_files = await self.get(f"/objects/{DOCUMENT_TYPE}/{rel_id}/latest/files")
                            if doc_files:
                                file_items = doc_files if isinstance(doc_files, list) else doc_files.get('Items', [])
                                for item in file_items:
                                    doc = {
                                        'file_id': item.get('ID', 0),
                                        'name': item.get('Name', ''),
                                        'extension': item.get('Extension', ''),
                                        'size_bytes': item.get('Size', 0),
                                        'object_type': int(DOCUMENT_TYPE),
                                        'object_id': rel_id,
                                        'version': item.get('Version', 0),
                                        'source': 'relationship',
                                    }
                                    if doc['file_id']:
                                        documents.append(doc)

                if documents:
                    logger.info(f"Found {len(documents)} documents via relationships for Vorgang {vorgang_id}")
                    return documents
        except Exception as e:
            logger.debug(f"No relationships found for Vorgang: {e}")

        # Tier 3: Collection members
        try:
            members = await self.get(f"/objects/{type_id}/{vorgang_id}/collectionmembers")
            if members:
                items = members if isinstance(members, list) else members.get('Items', [])
                for item in items:
                    obj_ver = item.get('ObjVer', {})
                    doc_type = obj_ver.get('Type', 0)
                    doc_id = obj_ver.get('ID')

                    if doc_id and doc_type == int(DOCUMENT_TYPE):
                        doc_files = await self.get(f"/objects/{DOCUMENT_TYPE}/{doc_id}/latest/files")
                        if doc_files:
                            file_items = doc_files if isinstance(doc_files, list) else doc_files.get('Items', [])
                            for f in file_items:
                                doc = {
                                    'file_id': f.get('ID', 0),
                                    'name': f.get('Name', ''),
                                    'extension': f.get('Extension', ''),
                                    'size_bytes': f.get('Size', 0),
                                    'object_type': int(DOCUMENT_TYPE),
                                    'object_id': doc_id,
                                    'version': f.get('Version', 0),
                                    'source': 'collectionmember',
                                }
                                if doc['file_id']:
                                    documents.append(doc)

                if documents:
                    logger.info(f"Found {len(documents)} documents via collectionmembers for Vorgang {vorgang_id}")
                    return documents
        except Exception as e:
            logger.debug(f"No collectionmembers found for Vorgang: {e}")

        logger.info(f"No documents found in Vorgang {vorgang_id}")
        return documents

    def _parse_version_properties(self, version_num: int, props: List[Dict]) -> Dict[str, Any]:
        """Parse properties for a specific version of a unit object."""
        entry: Dict[str, Any] = {'version': version_num, 'changes': []}

        for prop in props:
            prop_id = prop.get('PropertyDef')
            typed_value = prop.get('TypedValue', {})
            display_value = typed_value.get('DisplayValue', '')
            value = typed_value.get('Value')

            if prop_id == PROPERTY_IDS["Mieter"]:
                if 'Lookup' in typed_value and typed_value['Lookup']:
                    entry['tenant'] = typed_value['Lookup'].get('DisplayValue', '')
                elif 'Lookups' in typed_value and typed_value['Lookups']:
                    names = [l.get('DisplayValue', '') for l in typed_value['Lookups'] if l.get('DisplayValue')]
                    entry['tenant'] = "; ".join(names)
                else:
                    entry['tenant'] = display_value

            elif prop_id == PROPERTY_IDS["Einheitenstatus"]:
                entry['status'] = display_value

            elif prop_id == PROPERTY_IDS["WorkflowStatus"]:
                if 'Lookup' in typed_value and typed_value['Lookup']:
                    entry['workflow_status'] = typed_value['Lookup'].get('DisplayValue', '')
                else:
                    entry['workflow_status'] = display_value

            elif prop_id == 21:  # LastModified
                entry['modified_date'] = self._format_date(display_value)

            elif prop_id == 23:  # LastModifiedBy
                if 'Lookup' in typed_value and typed_value['Lookup']:
                    entry['modified_by'] = typed_value['Lookup'].get('DisplayValue', '')
                else:
                    entry['modified_by'] = display_value

            elif prop_id == PROPERTY_IDS["Miete_netto"]:
                entry['net_rent'] = self._parse_decimal(value if value is not None else display_value)

            elif prop_id == PROPERTY_IDS["Miete_netto_projektiert"]:
                entry['net_rent_projected'] = self._parse_decimal(value if value is not None else display_value)

            elif prop_id == PROPERTY_IDS["Name"]:
                entry['unit_name'] = display_value

            elif prop_id == PROPERTY_IDS["Einheitennummer"]:
                entry['unit_number'] = display_value

        return entry
