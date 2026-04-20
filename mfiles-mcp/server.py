#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
M-Files MCP Server for Birnbaum Group Real Estate Data (FastMCP Version)
=========================================================================

Modern MCP server using FastMCP framework with:
- Pydantic input validation
- Tool annotations (readOnlyHint, etc.)
- JSON and Markdown response formats
- Async httpx client

Tools (30 total):
1. mfiles_list_portfolios - List all portfolios
2. mfiles_get_portfolio_properties - Properties for a portfolio
3. mfiles_get_units - Units for a property (Rent Roll)
4. mfiles_get_mortgages - Mortgages for a property
5. mfiles_get_metrics - Comprehensive property metrics
6. mfiles_simulate_scenario - What-if simulation
7. mfiles_search - Fuzzy search properties
8. mfiles_get_tenants - Tenant list
9. mfiles_get_vacancy - Vacancy analysis
10. mfiles_compare - Compare properties
11. mfiles_refinancing_scenarios - Refinancing scenarios
12. mfiles_portfolio_summary - Portfolio aggregates
13. mfiles_expiring_leases - Expiring leases
14. mfiles_upcoming_refinancing - Upcoming refinancing
15. mfiles_get_invoices - Invoices (placeholder)
16. mfiles_get_unit_docs - Unit documents
17. mfiles_get_property_docs - Property documents
18. mfiles_download_doc - Download document
19. mfiles_get_unit_history - Unit version history (tenant/status changes)
20. mfiles_discover_object_types - Discover all M-Files object types
21. mfiles_list_vorgaenge - List Vorgaenge (Mietermeldung, Sanierung, etc.)
22. mfiles_get_vorgang_details - Full Vorgang metadata + linked entities
23. mfiles_get_vorgang_documents - Download + extract text from Vorgang documents

Usage:
    python mfiles_mcp_server.py
"""

import asyncio
import json
import logging
import sys
import tempfile
import os
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

from pydantic import BaseModel, ConfigDict, Field
from mcp.server.fastmcp import FastMCP

from models import (
    # Input models
    ResponseFormat,
    ListPortfoliosInput, GetPortfolioPropertiesInput,
    GetUnitsInput, GetMortgagesInput, GetMetricsInput,
    SimulateScenarioInput, SearchInput, GetTenantsInput,
    GetVacancyInput, ComparePropertiesInput, RefinancingScenariosInput,
    PortfolioSummaryInput, ExpiringLeasesInput, UpcomingRefinancingInput,
    GetInvoicesInput, GetUnitDocsInput, GetPropertyDocsInput, DownloadDocInput,
    GetUnitHistoryInput,
    DiscoverObjectTypesInput, ListVorgaengeInput,
    GetVorgangDetailsInput, GetVorgangDocumentsInput,
    # Output models
    PortfolioList, PortfolioInfo, PortfolioProperties, PropertySummary,
    PropertyUnits, UnitInfo, PropertyMortgages, MortgageInfo,
    PropertyMetrics, ScenarioResult, ScenarioDelta, SearchResults, SearchResult,
    CategorySummary, VacancyCategorySummary, UnitTypeSummary,
    PropertyTenants, TenantInfo, VacancyAnalysis, VacantUnitInfo,
    PropertyComparison, PropertyRefinancingAnalysis, MortgageRefinancing,
    RefinancingScenario, PortfolioSummary as PortfolioSummaryModel, ExpiringLeases, ExpiringItem,
    UpcomingRefinancing,
    DocumentInfo, UnitDocuments, PropertyDocuments, DocumentContent,
    UnitVersionEntry, UnitVersionHistory,
    ObjectTypeInfo, ObjectTypeList, VorgangSummary, VorgaengeList,
    LinkedObjectInfo, VorgangDetails, VorgangDocumentWithContent, VorgangDocuments,
    # Write tool input models
    SetVorgangStatusInput, SetAngebotStatusInput,
    SetSanierungStatusInput, AddVorgangCommentInput,
    GetViewItemsInput,
    # Status maps
    MIETERMELDUNG_STATUS_MAP, ANGEBOT_STATUS_MAP, SANIERUNG_STATUS_MAP,
)
from mfiles_client import MFilesClient
from calculations import (
    calculate_property_metrics, simulate_scenario, fuzzy_search,
    aggregate_unit_metrics, calculate_debt_service,
    classify_unit_for_metrics,
    analyze_vacancy, calculate_refinancing_scenarios,
    calculate_days_until, aggregate_portfolio_metrics
)

# Configure logging to stderr (required for MCP servers)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger('mfiles_mcp')

# Global client instance
_client: Optional[MFilesClient] = None


async def get_client() -> MFilesClient:
    """Get or initialize the M-Files client"""
    global _client
    if _client is None:
        _client = MFilesClient()
    return _client


@asynccontextmanager
async def app_lifespan(app):
    """Manage client lifecycle"""
    global _client
    _client = MFilesClient()
    yield {"client": _client}
    if _client:
        await _client.close()


# Initialize FastMCP server with proper naming
mcp = FastMCP("mfiles_mcp", lifespan=app_lifespan)


# =============================================================================
# Shared document-text extraction (used by get_vorgang_documents AND by
# the batched vorgaenge_recap_bundle tool below). Pulling it out of the
# tool bodies means both paths extract identical content from PDFs / MSG
# / plain text.
# =============================================================================
def _decode_doc_content(content: bytes, ext: str, filename: str) -> str:
    ext = (ext or "").lower()
    if ext == "pdf":
        try:
            try:
                import PyPDF2
                import io
                reader = PyPDF2.PdfReader(io.BytesIO(content))
                return "\n\n".join((page.extract_text() or "") for page in reader.pages)
            except ImportError:
                try:
                    import pdfplumber
                    import io
                    with pdfplumber.open(io.BytesIO(content)) as pdf:
                        return "\n\n".join((page.extract_text() or "") for page in pdf.pages)
                except ImportError:
                    return "[PDF - PyPDF2/pdfplumber nicht installiert]"
        except Exception as e:
            return f"[Fehler bei PDF-Extraktion: {e}]"
    if ext in {"txt", "csv", "xml", "json", "html", "htm"}:
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return content.decode("latin-1")
            except Exception:
                return "[Konnte Textdatei nicht dekodieren]"
    if ext == "msg":
        try:
            import extract_msg
            import io
            msg = extract_msg.openMsg(io.BytesIO(content))
            parts = []
            if msg.sender:
                parts.append(f"Von: {msg.sender}")
            if msg.to:
                parts.append(f"An: {msg.to}")
            if msg.cc:
                parts.append(f"Cc: {msg.cc}")
            if msg.date:
                parts.append(f"Datum: {msg.date}")
            if msg.subject:
                parts.append(f"Betreff: {msg.subject}")
            body = msg.body or ""
            if not body and getattr(msg, "htmlBody", None):
                import re as _re
                body = _re.sub(r"<[^>]+>", "", msg.htmlBody or "")
            parts.append("")
            parts.append(body.strip())
            return "\n".join(parts)
        except ImportError:
            return "[MSG - extract-msg nicht installiert]"
        except Exception as e:
            return f"[Fehler bei MSG-Extraktion: {e}]"
    return f"[Binaerdatei: {filename}.{ext}, {len(content)} bytes]"


async def _fetch_vorgang_docs_with_text(
    client, vorgang_id: int, object_type: int, max_docs: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Load docs + extract text for one Vorgang. Parallel file downloads."""
    try:
        raw = await client.get_vorgang_documents(vorgang_id)
    except Exception as e:
        return [{"error": f"Doc-list fehlgeschlagen: {e}"}]
    readable = [d for d in raw if d.get("extension", "").lower() in ("msg", "pdf", "eml", "txt", "html", "htm")]
    if max_docs is not None:
        readable = readable[:max_docs]
    if not readable:
        return []

    async def _one(doc):
        content, file_info = await client.download_file(
            doc.get("object_type", object_type), doc.get("object_id", vorgang_id), doc["file_id"]
        )
        if content is None:
            return {
                "name": doc.get("name", "?"),
                "ext": doc.get("extension", ""),
                "error": file_info.get("error", "Download failed"),
            }
        text = _decode_doc_content(content, doc.get("extension", ""), doc.get("name", "doc"))
        return {
            "name": doc.get("name", "?"),
            "ext": doc.get("extension", ""),
            "size_bytes": doc.get("size_bytes", 0),
            "text": text,
        }

    return list(await asyncio.gather(*[_one(d) for d in readable], return_exceptions=False))


# =============================================================================
# Helper Functions
# =============================================================================

async def resolve_property(
    property_id: Optional[int],
    property_name: Optional[str]
) -> tuple[Optional[int], str]:
    """Resolve property ID and name from either input"""
    client = await get_client()

    if property_id:
        props = await client.get_all_properties()
        for p in props:
            if p['id'] == property_id:
                return property_id, p['name']
        return property_id, f"Property {property_id}"

    elif property_name:
        prop = await client.find_property_by_name(property_name)
        if prop:
            return prop['id'], prop['name']

    return None, ""


def format_currency(value: float) -> str:
    """Format value as German currency"""
    return f"{value:,.2f} EUR".replace(",", "X").replace(".", ",").replace("X", ".")


def format_percent(value: float) -> str:
    """Format value as percentage"""
    return f"{value:.2f}%".replace(".", ",")


def to_markdown_portfolios(result: PortfolioList) -> str:
    """Convert portfolio list to markdown"""
    lines = ["# Portfolios der Birnbaum Group", ""]
    lines.append(f"**Gesamt:** {result.total_portfolios} Portfolios, {result.total_properties} Liegenschaften")
    lines.append("")
    lines.append("| Portfolio | Liegenschaften |")
    lines.append("|-----------|----------------|")
    for p in result.portfolios:
        lines.append(f"| {p.name} | {p.property_count} |")
    return "\n".join(lines)


def to_markdown_properties(result: PortfolioProperties) -> str:
    """Convert portfolio properties to markdown"""
    lines = [f"# Portfolio: {result.portfolio_name}", ""]
    lines.append(f"**Gesamt:** {format_currency(result.total_monthly_rent)}/Monat IST, {format_currency(result.total_monthly_rent_projected)}/Monat SOLL")
    lines.append(f"**Durchschn. Leerstand:** {format_percent(result.average_vacancy_rate)}")
    lines.append("")
    lines.append("| Liegenschaft | Miete IST | Miete SOLL | Leerstand | DSCR | Einheiten |")
    lines.append("|--------------|-----------|------------|-----------|------|-----------|")
    for p in result.properties:
        dscr = f"{p.dscr:.2f}" if p.dscr else "-"
        lines.append(f"| {p.name} | {format_currency(p.monthly_rent)} | {format_currency(p.monthly_rent_projected)} | {format_percent(p.vacancy_rate)} | {dscr} | {p.unit_count} ({p.vacant_unit_count} leer) |")
    return "\n".join(lines)


def to_markdown_metrics(result: PropertyMetrics) -> str:
    """Convert property metrics to markdown"""
    lines = [f"# Kennzahlen: {result.property_name}", ""]

    lines.append("## Miete")
    lines.append(f"- **IST monatlich:** {format_currency(result.monthly_rent_actual)}")
    lines.append(f"- **SOLL monatlich:** {format_currency(result.monthly_rent_projected)}")
    lines.append(f"- **IST jaehrlich:** {format_currency(result.annual_rent_actual)}")
    lines.append("")

    lines.append("## Einheiten")
    lines.append(f"- **Gesamt:** {result.total_units} ({result.occupied_units} belegt, {result.vacant_units} leer)")
    lines.append(f"- **Leerstandsquote:** {format_percent(result.vacancy_rate)}")
    lines.append(f"- **Flaeche:** {result.total_area_sqm:,.0f} m²")
    lines.append("")

    lines.append("## Finanzierung")
    lines.append(f"- **Restschuld:** {format_currency(result.total_outstanding_debt)}")
    lines.append(f"- **Kapitaldienst monatl.:** {format_currency(result.monthly_debt_service)}")
    lines.append(f"- **davon Zinsen:** {format_currency(result.monthly_interest)}")
    lines.append(f"- **davon Tilgung:** {format_currency(result.monthly_principal)}")
    lines.append("")

    lines.append("## Kennzahlen")
    if result.dscr:
        lines.append(f"- **DSCR:** {result.dscr:.2f}")
    if result.ltv:
        lines.append(f"- **LTV:** {format_percent(result.ltv)}")
    if result.cap_rate:
        lines.append(f"- **Cap Rate:** {format_percent(result.cap_rate)}")
    lines.append(f"- **Cashflow monatl.:** {format_currency(result.cashflow_monthly)}")
    lines.append(f"- **Cashflow jaehrl.:** {format_currency(result.cashflow_annual)}")

    return "\n".join(lines)


# =============================================================================
# Tool Definitions with FastMCP
# =============================================================================

@mcp.tool(
    name="mfiles_list_portfolios",
    annotations={
        "title": "Liste aller Portfolios",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_list_portfolios(params: ListPortfoliosInput) -> str:
    """Listet alle Portfolios der Birnbaum Group mit Liegenschaftszaehlung.

    Wann verwenden: Uebersicht aller Portfolios, Einstiegspunkt fuer Portfolio-Analysen

    Returns:
        Portfolio-Namen, Anzahl Liegenschaften pro Portfolio, Gesamtzahlen
    """
    client = await get_client()
    portfolios = await client.get_all_portfolios()

    total_properties = 0
    portfolio_infos = []

    for p in portfolios:
        props = await client.get_portfolio_properties(p['name'])
        count = len(props)
        total_properties += count
        portfolio_infos.append(PortfolioInfo(name=p['name'], property_count=count))

    result = PortfolioList(
        portfolios=portfolio_infos,
        total_portfolios=len(portfolio_infos),
        total_properties=total_properties
    )

    if params.response_format == ResponseFormat.MARKDOWN:
        return to_markdown_portfolios(result)
    return result.model_dump_json(indent=2)


@mcp.tool(
    name="mfiles_get_portfolio_properties",
    annotations={
        "title": "Liegenschaften eines Portfolios",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_get_portfolio_properties(params: GetPortfolioPropertiesInput) -> str:
    """Alle Liegenschaften eines Portfolios mit Basiskennzahlen.

    Wann verwenden: Portfolio-Uebersicht, Liegenschaften vergleichen, Problemfaelle identifizieren

    Returns:
        Liste aller Liegenschaften mit Miete, DSCR, Leerstand, Einheitenzahl
    """
    client = await get_client()
    properties = await client.get_portfolio_properties(params.portfolio_name)

    if not properties:
        return f"Keine Liegenschaften fuer Portfolio '{params.portfolio_name}' gefunden."

    # Parallel fetch
    async def fetch_property_basics(prop):
        units = await client.get_property_units(prop['id'])
        mortgages = await client.get_property_mortgages(prop['id'], prop['name'])
        return prop, units, mortgages

    tasks = [fetch_property_basics(prop) for prop in properties]
    results = await asyncio.gather(*tasks)

    property_summaries = []
    total_rent = 0.0
    total_rent_projected = 0.0
    vacancy_rates = []

    for prop, units, mortgages in results:
        unit_metrics = aggregate_unit_metrics(units)
        _, _, debt_service = calculate_debt_service(mortgages, monthly=True)

        dscr = None
        if debt_service > 0 and unit_metrics['monthly_rent_projected'] > 0:
            dscr = unit_metrics['monthly_rent_projected'] / debt_service

        summary = PropertySummary(
            id=prop['id'],
            name=prop['name'],
            monthly_rent=unit_metrics['monthly_rent_actual'],
            monthly_rent_projected=unit_metrics['monthly_rent_projected'],
            vacancy_rate=unit_metrics['vacancy_rate'],
            dscr=round(dscr, 2) if dscr else None,
            unit_count=unit_metrics['total_units'],
            vacant_unit_count=unit_metrics['vacant_units']
        )
        property_summaries.append(summary)

        total_rent += unit_metrics['monthly_rent_actual']
        total_rent_projected += unit_metrics['monthly_rent_projected']
        if unit_metrics['total_units'] > 0:
            vacancy_rates.append(unit_metrics['vacancy_rate'])

    avg_vacancy = sum(vacancy_rates) / len(vacancy_rates) if vacancy_rates else 0.0

    result = PortfolioProperties(
        portfolio_name=params.portfolio_name,
        properties=property_summaries,
        total_monthly_rent=total_rent,
        total_monthly_rent_projected=total_rent_projected,
        average_vacancy_rate=avg_vacancy
    )

    if params.response_format == ResponseFormat.MARKDOWN:
        return to_markdown_properties(result)
    return result.model_dump_json(indent=2)


@mcp.tool(
    name="mfiles_get_units",
    annotations={
        "title": "Einheiten einer Liegenschaft",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_get_units(params: GetUnitsInput) -> str:
    """Einheiten einer Liegenschaft mit Details nach Kategorien (Rent Roll).

    Wann verwenden: Rent Roll erstellen, Einheiten-Details, Mieter-Uebersicht

    Returns:
        Kategorisierte Listen (Haupteinheiten, Stellplaetze, Keller, E-Bike)
    """
    client = await get_client()

    prop_id, prop_name = await resolve_property(params.property_id, params.property_name)
    if prop_id is None:
        return "Liegenschaft nicht gefunden. Bitte property_id oder property_name angeben."

    units = await client.get_property_units(prop_id)

    main_units_list = []
    cellar_units_list = []
    parking_units_list = []
    bike_units_list = []
    all_units_list = []

    for u in units:
        category = classify_unit_for_metrics(u)
        if category == 'excluded':
            continue

        unit_info = UnitInfo(
            id=u.get('id', 0),
            unit_number=u.get('unit_number', ''),
            unit_name=u.get('unit_name', '') or u.get('bezeichnung', ''),
            unit_type=u.get('unit_type', ''),
            unit_category=category,
            status=u.get('status', ''),
            net_rent=u.get('net_rent', 0) or 0,
            net_rent_projected=u.get('net_rent_projected', 0) or 0,
            area_sqm=u.get('area_sqm', 0) or 0,
            tenant=u.get('tenant', ''),
            is_vacant=u.get('is_vacant', False),
            is_parking=(category == 'parking'),
            is_cellar=(category == 'cellar'),
            is_bike=(category == 'bike')
        )

        all_units_list.append(unit_info)

        if category == 'main':
            main_units_list.append(unit_info)
        elif category == 'cellar':
            cellar_units_list.append(unit_info)
        elif category == 'parking':
            parking_units_list.append(unit_info)
        elif category == 'bike':
            bike_units_list.append(unit_info)

    metrics = aggregate_unit_metrics(units)
    by_cat = metrics.get('by_category', {})

    summary = {}
    for cat_name, cat_data in by_cat.items():
        summary[cat_name] = CategorySummary(
            total=cat_data.get('total', 0),
            occupied=cat_data.get('occupied', 0),
            vacant=cat_data.get('vacant', 0),
            vacancy_rate=cat_data.get('vacancy_rate', 0),
            rent_actual=cat_data.get('rent_actual', 0),
            rent_projected=cat_data.get('rent_projected', 0),
            area_sqm=cat_data.get('area_sqm', 0)
        )

    parking_rent = metrics.get('parking_rent_actual', 0)
    parking_occupied = metrics.get('parking_occupied', 0)
    parking_note = ""
    if parking_rent > 0:
        parking_note = f"enthaelt {parking_rent:.2f} EUR fuer {parking_occupied} vermietete Stellplaetze"

    result = PropertyUnits(
        property_id=prop_id,
        property_name=prop_name,
        main_units=main_units_list,
        cellar_units=cellar_units_list,
        parking_units=parking_units_list,
        bike_units=bike_units_list,
        units=all_units_list,
        summary=summary,
        total_units=metrics['total_units'],
        total_parking=metrics['total_parking'],
        total_cellar=len(cellar_units_list),
        total_bike=len(bike_units_list),
        total_rent=metrics['monthly_rent_actual'],
        total_rent_projected=metrics['monthly_rent_projected'],
        total_area_sqm=metrics['total_area_sqm'],
        vacancy_rate=metrics['vacancy_rate'],
        parking_rent=parking_rent,
        parking_rent_note=parking_note
    )

    return result.model_dump_json(indent=2)


@mcp.tool(
    name="mfiles_get_mortgages",
    annotations={
        "title": "Darlehen einer Liegenschaft",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_get_mortgages(params: GetMortgagesInput) -> str:
    """Aktive Darlehen einer Liegenschaft mit Konditionen.

    Wann verwenden: Finanzierungsuebersicht, Refinanzierung planen, Kapitaldienst berechnen

    Returns:
        Liste aller Darlehen mit Bank, Restschuld, Zinssatz, Tilgung, Zinsbindung
    """
    client = await get_client()

    prop_id, prop_name = await resolve_property(params.property_id, params.property_name)
    if prop_id is None:
        return "Liegenschaft nicht gefunden."

    mortgages = await client.get_property_mortgages(prop_id, prop_name)

    mortgage_infos = []
    total_outstanding = 0.0
    total_monthly_interest = 0.0
    total_monthly_payment = 0.0

    for m in mortgages:
        balance = m.get('outstanding_balance', 0)
        rate = m.get('interest_rate', 0)
        monthly_interest = balance * (rate / 100) / 12 if rate > 0 else 0

        payment = m.get('payment_amount', 0)
        interval = m.get('payment_interval', '').lower()
        if 'monat' in interval:
            monthly_payment = payment
        elif 'quartal' in interval:
            monthly_payment = payment / 3
        elif 'halbjahr' in interval or 'halbjaehr' in interval:
            monthly_payment = payment / 6
        elif 'jahr' in interval or 'jaehr' in interval:
            monthly_payment = payment / 12
        else:
            monthly_payment = payment

        info = MortgageInfo(
            id=m.get('id', 0),
            bank=m.get('bank', ''),
            contract_number=m.get('contract_number', ''),
            loan_type=m.get('loan_type', ''),
            loan_amount=m.get('loan_amount', 0),
            outstanding_balance=balance,
            balance_date=m.get('balance_date'),
            interest_rate=rate,
            amortization_rate=m.get('amortization_rate', 0),
            monthly_payment=monthly_payment,
            payment_interval=m.get('payment_interval', ''),
            debit_date=m.get('debit_date', ''),
            start_date=m.get('start_date'),
            end_date=m.get('end_date'),
            fixed_rate_until=m.get('fixed_rate_until'),
            linked_properties=m.get('linked_properties', [])
        )
        mortgage_infos.append(info)

        total_outstanding += balance
        total_monthly_interest += monthly_interest
        total_monthly_payment += monthly_payment

    result = PropertyMortgages(
        property_id=prop_id,
        property_name=prop_name,
        mortgages=mortgage_infos,
        total_outstanding=total_outstanding,
        total_monthly_interest=total_monthly_interest,
        total_monthly_payment=total_monthly_payment
    )

    return result.model_dump_json(indent=2)


@mcp.tool(
    name="mfiles_get_metrics",
    annotations={
        "title": "Kennzahlen einer Liegenschaft",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_get_metrics(params: GetMetricsInput) -> str:
    """Berechnet umfassende Immobilien-Kennzahlen fuer eine Liegenschaft.

    Wann verwenden: Finanzanalyse, Kreditwuerdigkeit pruefen, Investment-Entscheidungen

    Returns:
        Miete, Leerstand, Schulden, Kennzahlen (DSCR, LTV, Cap Rate, ICR, NOI, Cashflow)
    """
    client = await get_client()

    prop_id, prop_name = await resolve_property(params.property_id, params.property_name)
    if prop_id is None:
        return "Liegenschaft nicht gefunden."

    units = await client.get_property_units(prop_id)
    mortgages = await client.get_property_mortgages(prop_id, prop_name)

    metrics = calculate_property_metrics(units, mortgages, params.verkehrswert)

    result = PropertyMetrics(
        property_id=prop_id,
        property_name=prop_name,
        monthly_rent_actual=metrics['monthly_rent_actual'],
        monthly_rent_projected=metrics['monthly_rent_projected'],
        annual_rent_actual=metrics['annual_rent_actual'],
        annual_rent_projected=metrics['annual_rent_projected'],
        total_units=metrics['total_units'],
        occupied_units=metrics['occupied_units'],
        vacant_units=metrics['vacant_units'],
        vacancy_rate=round(metrics['vacancy_rate'], 2),
        total_area_sqm=metrics['total_area_sqm'],
        total_outstanding_debt=metrics['total_outstanding_debt'],
        monthly_interest=round(metrics['monthly_interest'], 2),
        monthly_principal=round(metrics['monthly_principal'], 2),
        monthly_debt_service=round(metrics['monthly_debt_service'], 2),
        annual_interest=round(metrics.get('annual_interest', 0), 2),
        annual_principal=round(metrics.get('annual_principal', 0), 2),
        annual_debt_service=round(metrics['annual_debt_service'], 2),
        dscr=round(metrics['dscr'], 2) if metrics['dscr'] else None,
        ltv=round(metrics['ltv'], 2) if metrics['ltv'] else None,
        cap_rate=round(metrics['cap_rate'], 2) if metrics['cap_rate'] else None,
        interest_coverage_ratio=round(metrics['interest_coverage_ratio'], 2) if metrics.get('interest_coverage_ratio') else None,
        cashflow_ratio=round(metrics['cashflow_ratio'], 2) if metrics.get('cashflow_ratio') else None,
        debt_ratio=round(metrics['debt_ratio'], 2) if metrics.get('debt_ratio') else None,
        noi_monthly=round(metrics['noi_monthly'], 2),
        noi_annual=round(metrics['noi_annual'], 2),
        surplus_before_principal_monthly=round(metrics.get('surplus_before_principal_monthly', 0), 2),
        surplus_before_principal_annual=round(metrics.get('surplus_before_principal_annual', 0), 2),
        cashflow_monthly=round(metrics['cashflow_monthly'], 2),
        cashflow_annual=round(metrics['cashflow_annual'], 2),
        final_surplus_annual=round(metrics.get('final_surplus_annual', 0), 2),
        debt_per_sqm=round(metrics['debt_per_sqm'], 2) if metrics.get('debt_per_sqm') else None,
        wohn_monthly=metrics.get('wohn_monthly', 0),
        wohn_annual=metrics.get('wohn_annual', 0),
        wohn_area=metrics.get('wohn_area', 0),
        wohn_per_sqm=metrics.get('wohn_per_sqm', 0),
        gewerbe_monthly=metrics.get('gewerbe_monthly', 0),
        gewerbe_annual=metrics.get('gewerbe_annual', 0),
        gewerbe_area=metrics.get('gewerbe_area', 0),
        gewerbe_per_sqm=metrics.get('gewerbe_per_sqm', 0),
        parking_monthly=metrics.get('parking_monthly', 0),
        parking_annual=metrics.get('parking_annual', 0),
        parking_count=metrics.get('parking_count', 0),
        ebike_total=metrics.get('ebike_total', 0),
        ebike_empty_count=metrics.get('ebike_empty_count', 0),
        ebike_occupied_count=metrics.get('ebike_occupied_count', 0),
        ebike_projected_rent=round(metrics.get('ebike_projected_rent', 0), 2),
        ebike_occupied_rent=round(metrics.get('ebike_occupied_rent', 0), 2),
        total_rent_with_projection_monthly=round(metrics.get('total_rent_with_projection_monthly', 0), 2),
        total_rent_with_projection_annual=round(metrics.get('total_rent_with_projection_annual', 0), 2),
        potential_rent_vacant_monthly=round(metrics.get('potential_rent_vacant_monthly', 0), 2),
        potential_rent_vacant_annual=round(metrics.get('potential_rent_vacant_annual', 0), 2),
        noi_projected_monthly=round(metrics.get('noi_projected_monthly', 0), 2),
        noi_projected_annual=round(metrics.get('noi_projected_annual', 0), 2),
        surplus_before_principal_projected_monthly=round(metrics.get('surplus_before_principal_projected_monthly', 0), 2),
        surplus_before_principal_projected_annual=round(metrics.get('surplus_before_principal_projected_annual', 0), 2),
        cashflow_projected_monthly=round(metrics.get('cashflow_projected_monthly', 0), 2),
        cashflow_projected_annual=round(metrics.get('cashflow_projected_annual', 0), 2),
        dscr_projected=round(metrics['dscr_projected'], 2) if metrics.get('dscr_projected') else None,
        interest_coverage_ratio_projected=round(metrics['interest_coverage_ratio_projected'], 2) if metrics.get('interest_coverage_ratio_projected') else None,
        cashflow_ratio_projected=round(metrics['cashflow_ratio_projected'], 2) if metrics.get('cashflow_ratio_projected') else None,
        verkehrswert=params.verkehrswert,
        verkehrswert_source="user provided" if params.verkehrswert else "not provided"
    )

    if params.response_format == ResponseFormat.MARKDOWN:
        return to_markdown_metrics(result)
    return result.model_dump_json(indent=2)


@mcp.tool(
    name="mfiles_simulate_scenario",
    annotations={
        "title": "Was-waere-wenn Simulation",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_simulate_scenario(params: SimulateScenarioInput) -> str:
    """Was-waere-wenn-Simulation fuer Refinanzierung, Mieterhoehung oder Leerstandsaenderung.

    Wann verwenden: Sensitivitaetsanalyse, Refinanzierung planen, Auswirkungen pruefen

    Returns:
        Vorher/Nachher-Vergleich mit Delta-Werten fuer Miete, Kapitaldienst, Cashflow, DSCR
    """
    client = await get_client()

    prop_id, prop_name = await resolve_property(params.property_id, params.property_name)
    if prop_id is None:
        return "Liegenschaft nicht gefunden."

    units = await client.get_property_units(prop_id)
    mortgages = await client.get_property_mortgages(prop_id, prop_name)

    current_metrics = calculate_property_metrics(units, mortgages, params.new_verkehrswert)

    scenario_result = simulate_scenario(
        current_metrics,
        new_loan_amount=params.new_loan_amount,
        new_interest_rate=params.new_interest_rate,
        rent_change_pct=params.rent_change_pct,
        vacancy_change_pct=params.vacancy_change_pct,
        new_verkehrswert=params.new_verkehrswert
    )

    def to_delta(d: dict) -> ScenarioDelta:
        return ScenarioDelta(
            before=round(d['before'], 2),
            after=round(d['after'], 2),
            delta=round(d['delta'], 2),
            delta_pct=round(d['delta_pct'], 2) if d.get('delta_pct') is not None else None
        )

    result = ScenarioResult(
        property_id=prop_id,
        property_name=prop_name,
        scenario_description=scenario_result['scenario_description'],
        monthly_rent=to_delta(scenario_result['monthly_rent']),
        monthly_debt_service=to_delta(scenario_result['monthly_debt_service']),
        monthly_cashflow=to_delta(scenario_result['monthly_cashflow']),
        dscr=to_delta(scenario_result['dscr']),
        ltv=to_delta(scenario_result['ltv']) if scenario_result.get('ltv') else None,
        warnings=scenario_result['warnings'],
        is_viable=scenario_result['is_viable']
    )

    return result.model_dump_json(indent=2)


@mcp.tool(
    name="mfiles_search",
    annotations={
        "title": "Liegenschaft suchen",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_search(params: SearchInput) -> str:
    """Sucht Liegenschaften nach Name mit Fuzzy-Matching.

    Wann verwenden: Liegenschaft finden wenn Name nicht exakt bekannt, ID ermitteln

    Returns:
        Treffer mit ID, Name, Portfolio, Relevanz-Score (Top 10)
    """
    client = await get_client()

    if params.portfolio:
        properties = await client.get_portfolio_properties(params.portfolio)
    else:
        properties = await client.get_all_properties()

    matches = fuzzy_search(params.query, properties, key='name')

    results = []
    for m in matches[:10]:
        portfolio_name = ""
        if not params.portfolio:
            portfolio_name = await client.get_property_portfolio(m['id'])

        results.append(SearchResult(
            id=m['id'],
            name=m['name'],
            portfolio=portfolio_name or params.portfolio or "",
            relevance_score=m['relevance_score']
        ))

    result = SearchResults(
        query=params.query,
        results=results,
        total_found=len(matches)
    )

    return result.model_dump_json(indent=2)


@mcp.tool(
    name="mfiles_get_tenants",
    annotations={
        "title": "Mieterliste",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_get_tenants(params: GetTenantsInput) -> str:
    """Mieterliste einer Liegenschaft mit Vertragsdetails.

    Wann verwenden: Rent Roll erstellen, Mieterstruktur analysieren

    Returns:
        Kategorisierte Mieterlisten mit Details (Miete, Flaeche, Vertrag)
    """
    client = await get_client()

    prop_id, prop_name = await resolve_property(params.property_id, params.property_name)
    if prop_id is None:
        return "Liegenschaft nicht gefunden."

    units = await client.get_property_units(prop_id)

    main_tenants = []
    cellar_tenants = []
    parking_tenants = []
    bike_tenants = []
    all_tenants = []

    total_rent = 0.0
    main_occupied = 0
    main_total = 0
    residential_area = 0.0
    commercial_area = 0.0

    for u in units:
        category = classify_unit_for_metrics(u)
        if category == 'excluded':
            continue

        if category == 'main':
            main_total += 1

        tenant_name = u.get('tenant', '')
        is_vacant = u.get('is_vacant', False)

        if not is_vacant and tenant_name:
            rent = u.get('net_rent', 0) or 0
            area = u.get('area_sqm', 0) or 0
            bk = u.get('betriebskosten', 0) or 0
            hk = u.get('heizkosten', 0) or 0
            brutto = u.get('bruttomiete', 0) or u.get('warmmiete', 0) or (rent + bk + hk)

            rent_per_sqm = u.get('rent_per_sqm', 0)
            if not rent_per_sqm and area > 0:
                rent_per_sqm = round(rent / area, 2)

            tenant_info = TenantInfo(
                name=tenant_name,
                unit_id=u.get('id', 0),
                unit_number=u.get('unit_number', ''),
                unit_name=u.get('unit_name', ''),
                unit_type=u.get('unit_type', ''),
                unit_category=category,
                monthly_rent=rent,
                betriebskosten=bk,
                heizkosten=hk,
                bruttomiete=brutto,
                rent_per_sqm=rent_per_sqm,
                area_sqm=area,
                status=u.get('status', ''),
                lease_start=u.get('lease_start'),
                lease_end=u.get('lease_end'),
                has_option=u.get('has_option', False),
                option_details=u.get('option_details', '')
            )

            all_tenants.append(tenant_info)

            if category == 'main':
                main_tenants.append(tenant_info)
                main_occupied += 1
                total_rent += rent

                unit_type = u.get('unit_type', '').lower()
                if 'wohn' in unit_type:
                    residential_area += area
                elif 'gewerbe' in unit_type or 'laden' in unit_type or 'buero' in unit_type:
                    commercial_area += area
                else:
                    residential_area += area

            elif category == 'cellar':
                cellar_tenants.append(tenant_info)
            elif category == 'parking':
                parking_tenants.append(tenant_info)
            elif category == 'bike':
                bike_tenants.append(tenant_info)

    occupancy_rate = (main_occupied / main_total * 100) if main_total > 0 else 0

    result = PropertyTenants(
        property_id=prop_id,
        property_name=prop_name,
        main_tenants=main_tenants,
        cellar_tenants=cellar_tenants,
        parking_tenants=parking_tenants,
        bike_tenants=bike_tenants,
        tenants=all_tenants,
        total_tenants=len(main_tenants),
        total_monthly_rent=total_rent,
        occupancy_rate=round(occupancy_rate, 2),
        residential_area=residential_area,
        commercial_area=commercial_area,
        total_area=residential_area + commercial_area
    )

    return result.model_dump_json(indent=2)


@mcp.tool(
    name="mfiles_get_vacancy",
    annotations={
        "title": "Leerstandsanalyse",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_get_vacancy(params: GetVacancyInput) -> str:
    """Detaillierte Leerstandsanalyse einer Liegenschaft.

    Wann verwenden: Leerstand analysieren, Vermietungspotenzial ermitteln

    Returns:
        Liste leerer Einheiten, Aufschluesselung nach Status/Kategorie/Nutzungsart
    """
    client = await get_client()

    prop_id, prop_name = await resolve_property(params.property_id, params.property_name)
    if prop_id is None:
        return "Liegenschaft nicht gefunden."

    units = await client.get_property_units(prop_id)
    analysis = analyze_vacancy(units)

    vacant_unit_infos = [
        VacantUnitInfo(
            id=v['id'],
            unit_number=v['unit_number'],
            unit_name=v.get('unit_name', ''),
            unit_type=v['unit_type'],
            unit_category=v.get('category', 'main'),
            status=v['status'],
            projected_rent=v['projected_rent'],
            area_sqm=v['area_sqm'],
            rent_per_sqm=v['rent_per_sqm']
        )
        for v in analysis['vacant_units']
    ]

    by_category = {}
    for cat_name, cat_data in analysis.get('by_category', {}).items():
        cat_units = [
            VacantUnitInfo(
                id=v['id'],
                unit_number=v['unit_number'],
                unit_name=v.get('unit_name', ''),
                unit_type=v['unit_type'],
                unit_category=cat_name,
                status=v['status'],
                projected_rent=v['projected_rent'],
                area_sqm=v['area_sqm'],
                rent_per_sqm=v['rent_per_sqm']
            )
            for v in cat_data.get('units', [])
        ]
        by_category[cat_name] = VacancyCategorySummary(
            vacant_count=cat_data.get('vacant_count', 0),
            total=cat_data.get('total', 0),
            vacancy_rate=cat_data.get('vacancy_rate', 0),
            potential_monthly_rent=cat_data.get('potential_monthly_rent', 0),
            potential_annual_rent=cat_data.get('potential_annual_rent', 0),
            units=cat_units
        )

    by_unit_type = {}
    for type_name, type_data in analysis.get('by_unit_type', {}).items():
        by_unit_type[type_name] = UnitTypeSummary(
            vacant_count=type_data.get('vacant_count', 0),
            total=type_data.get('total', 0),
            vacancy_rate=type_data.get('vacancy_rate', 0),
            potential_monthly_rent=type_data.get('potential_monthly_rent', 0),
            potential_annual_rent=type_data.get('potential_annual_rent', 0),
            area=type_data.get('area', 0)
        )

    result = VacancyAnalysis(
        property_id=prop_id,
        property_name=prop_name,
        vacant_units=vacant_unit_infos,
        total_units=analysis['total_units'],
        vacant_count=analysis['vacant_count'],
        vacancy_rate=round(analysis['vacancy_rate'], 2),
        potential_monthly_rent=analysis['potential_monthly_rent'],
        potential_annual_rent=analysis['potential_annual_rent'],
        rent_loss_monthly=analysis['rent_loss_monthly'],
        rent_loss_annual=analysis['rent_loss_annual'],
        by_status=analysis['by_status'],
        by_workflow_status=analysis.get('by_workflow_status', {}),
        by_category=by_category,
        by_unit_type=by_unit_type,
        total_potential_monthly=analysis.get('total_potential_monthly', 0),
        total_potential_annual=analysis.get('total_potential_annual', 0)
    )

    return result.model_dump_json(indent=2)


@mcp.tool(
    name="mfiles_compare",
    annotations={
        "title": "Liegenschaften vergleichen",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_compare(params: ComparePropertiesInput) -> str:
    """Vergleicht mehrere Liegenschaften nebeneinander.

    Wann verwenden: Portfolio-Ranking, Liegenschaften vergleichen

    Returns:
        Kennzahlen pro Liegenschaft plus Highlights (beste DSCR, etc.)
    """
    client = await get_client()

    resolved = []
    for pid in (params.property_ids or []):
        prop_id, prop_name = await resolve_property(pid, None)
        if prop_id:
            resolved.append((prop_id, prop_name))

    for pname in (params.property_names or []):
        prop_id, prop_name = await resolve_property(None, pname)
        if prop_id and prop_id not in [r[0] for r in resolved]:
            resolved.append((prop_id, prop_name))

    if not resolved:
        return "Keine Liegenschaften gefunden."

    async def fetch_for_comparison(prop_id, prop_name):
        units = await client.get_property_units(prop_id)
        mortgages = await client.get_property_mortgages(prop_id, prop_name)
        return prop_id, prop_name, units, mortgages

    tasks = [fetch_for_comparison(prop_id, prop_name) for prop_id, prop_name in resolved]
    results = await asyncio.gather(*tasks)

    property_metrics = []
    verkehrswerte = params.verkehrswerte or {}

    for prop_id, prop_name, units, mortgages in results:
        vw = verkehrswerte.get(str(prop_id)) or verkehrswerte.get(prop_id)
        metrics = calculate_property_metrics(units, mortgages, vw)

        property_metrics.append(PropertyMetrics(
            property_id=prop_id,
            property_name=prop_name,
            monthly_rent_actual=metrics['monthly_rent_actual'],
            monthly_rent_projected=metrics['monthly_rent_projected'],
            annual_rent_actual=metrics['annual_rent_actual'],
            annual_rent_projected=metrics['annual_rent_projected'],
            total_units=metrics['total_units'],
            occupied_units=metrics['occupied_units'],
            vacant_units=metrics['vacant_units'],
            vacancy_rate=round(metrics['vacancy_rate'], 2),
            total_area_sqm=metrics['total_area_sqm'],
            total_outstanding_debt=metrics['total_outstanding_debt'],
            monthly_debt_service=round(metrics['monthly_debt_service'], 2),
            annual_debt_service=round(metrics['annual_debt_service'], 2),
            dscr=round(metrics['dscr'], 2) if metrics['dscr'] else None,
            ltv=round(metrics['ltv'], 2) if metrics['ltv'] else None,
            cap_rate=round(metrics['cap_rate'], 2) if metrics['cap_rate'] else None,
            noi_monthly=round(metrics['noi_monthly'], 2),
            cashflow_monthly=round(metrics['cashflow_monthly'], 2),
            cashflow_annual=round(metrics['cashflow_annual'], 2),
            verkehrswert=vw
        ))

    best_dscr = max(property_metrics, key=lambda x: x.dscr or 0).property_name if property_metrics else None
    best_vacancy = min(property_metrics, key=lambda x: x.vacancy_rate).property_name if property_metrics else None
    best_cashflow = max(property_metrics, key=lambda x: x.cashflow_monthly).property_name if property_metrics else None

    total_rent = sum(p.monthly_rent_projected for p in property_metrics)
    total_debt = sum(p.total_outstanding_debt for p in property_metrics)
    avg_vacancy = sum(p.vacancy_rate for p in property_metrics) / len(property_metrics) if property_metrics else 0

    result = PropertyComparison(
        properties=property_metrics,
        best_dscr=best_dscr,
        best_vacancy=best_vacancy,
        best_cashflow=best_cashflow,
        total_portfolio_rent=total_rent,
        total_portfolio_debt=total_debt,
        average_vacancy_rate=round(avg_vacancy, 2)
    )

    return result.model_dump_json(indent=2)


@mcp.tool(
    name="mfiles_refinancing_scenarios",
    annotations={
        "title": "Refinanzierungsszenarien",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_refinancing_scenarios(params: RefinancingScenariosInput) -> str:
    """Simuliert Refinanzierungsszenarien bei Zinsbindungsende.

    Wann verwenden: Refinanzierung planen, Zinsrisiko analysieren

    Returns:
        Szenarien pro Darlehen mit neuem Kapitaldienst, DSCR, Cashflow-Delta
    """
    client = await get_client()

    prop_id, prop_name = await resolve_property(params.property_id, params.property_name)
    if prop_id is None:
        return "Liegenschaft nicht gefunden."

    units = await client.get_property_units(prop_id)
    mortgages = await client.get_property_mortgages(prop_id, prop_name)

    if not mortgages:
        return f"Keine aktiven Darlehen fuer '{prop_name}' gefunden."

    unit_metrics = aggregate_unit_metrics(units)
    monthly_noi = unit_metrics['monthly_rent_projected']

    mortgage_analyses = []
    current_total_payment = 0.0

    for m in mortgages:
        scenarios = calculate_refinancing_scenarios(m, monthly_noi, params.rate_scenarios)

        balance = m.get('outstanding_balance', 0)
        rate = m.get('interest_rate', 0)
        amort = m.get('amortization_rate', 2.0)
        current_payment = balance * (rate / 100) / 12 + balance * (amort / 100) / 12
        current_total_payment += current_payment

        fixed_until = m.get('fixed_rate_until')
        days = calculate_days_until(fixed_until)
        months = max(0, days // 30)

        mortgage_analyses.append(MortgageRefinancing(
            mortgage_id=m.get('id', 0),
            bank=m.get('bank', ''),
            outstanding_balance=balance,
            current_interest_rate=rate,
            current_monthly_payment=round(current_payment, 2),
            fixed_rate_until=fixed_until,
            months_until_refinancing=months,
            scenarios=[RefinancingScenario(**s) for s in scenarios]
        ))

    _, _, current_debt_service = calculate_debt_service(mortgages, monthly=True)
    current_dscr = monthly_noi / current_debt_service if current_debt_service > 0 else None

    result = PropertyRefinancingAnalysis(
        property_id=prop_id,
        property_name=prop_name,
        mortgages=mortgage_analyses,
        current_total_payment=round(current_total_payment, 2),
        current_dscr=round(current_dscr, 2) if current_dscr else None
    )

    return result.model_dump_json(indent=2)


@mcp.tool(
    name="mfiles_portfolio_summary",
    annotations={
        "title": "Portfolio-Zusammenfassung",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_portfolio_summary(params: PortfolioSummaryInput) -> str:
    """Aggregierte Kennzahlen fuer ein gesamtes Portfolio.

    Wann verwenden: Portfolio-Uebersicht, Gesamt-Performance, Management-Report

    Returns:
        Gesamtmiete, Gesamtschulden, Cashflow, Durchschnitte, Breakdown pro Liegenschaft
    """
    client = await get_client()

    properties = await client.get_portfolio_properties(params.portfolio_name)
    if not properties:
        return f"Keine Liegenschaften fuer Portfolio '{params.portfolio_name}' gefunden."

    async def fetch_property_data(prop):
        units = await client.get_property_units(prop['id'])
        mortgages = await client.get_property_mortgages(prop['id'], prop['name'])
        return prop, units, mortgages

    tasks = [fetch_property_data(prop) for prop in properties]
    results = await asyncio.gather(*tasks)

    properties_data = []
    property_summaries = []

    for prop, units, mortgages in results:
        metrics = calculate_property_metrics(units, mortgages)

        properties_data.append({
            'property': prop,
            'units': units,
            'mortgages': mortgages,
            'metrics': metrics
        })

        property_summaries.append(PropertySummary(
            id=prop['id'],
            name=prop['name'],
            monthly_rent=metrics['monthly_rent_actual'],
            monthly_rent_projected=metrics['monthly_rent_projected'],
            vacancy_rate=round(metrics['vacancy_rate'], 2),
            dscr=round(metrics['dscr'], 2) if metrics['dscr'] else None,
            unit_count=metrics['total_units'],
            vacant_unit_count=metrics['vacant_units']
        ))

    agg = aggregate_portfolio_metrics(properties_data)

    result = PortfolioSummaryModel(
        portfolio_name=params.portfolio_name,
        property_count=len(properties),
        total_units=agg['total_units'],
        occupied_units=agg['occupied_units'],
        vacant_units=agg['vacant_units'],
        vacancy_rate=agg['vacancy_rate'],
        total_monthly_rent_actual=agg['total_monthly_rent_actual'],
        total_monthly_rent_projected=agg['total_monthly_rent_projected'],
        total_annual_rent_actual=agg['total_annual_rent_actual'],
        total_annual_rent_projected=agg['total_annual_rent_projected'],
        total_outstanding_debt=agg['total_outstanding_debt'],
        total_monthly_interest=agg.get('total_monthly_interest', 0),
        total_monthly_principal=agg.get('total_monthly_principal', 0),
        total_monthly_debt_service=agg['total_monthly_debt_service'],
        total_annual_interest=agg.get('total_annual_interest', 0),
        total_annual_principal=agg.get('total_annual_principal', 0),
        total_annual_debt_service=agg['total_annual_debt_service'],
        surplus_before_principal_monthly=agg.get('surplus_before_principal_monthly', 0),
        surplus_before_principal_annual=agg.get('surplus_before_principal_annual', 0),
        total_monthly_cashflow=agg['total_monthly_cashflow'],
        total_annual_cashflow=agg['total_annual_cashflow'],
        average_dscr=agg['average_dscr'],
        weighted_average_interest_rate=agg['weighted_average_interest_rate'],
        total_mortgages=agg.get('total_mortgages', 0),
        interest_coverage_ratio=agg.get('interest_coverage_ratio'),
        cashflow_ratio=agg.get('cashflow_ratio'),
        debt_ratio=agg.get('debt_ratio'),
        property_breakdown=property_summaries
    )

    return result.model_dump_json(indent=2)


@mcp.tool(
    name="mfiles_expiring_leases",
    annotations={
        "title": "Auslaufende Mietvertraege",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_expiring_leases(params: ExpiringLeasesInput) -> str:
    """Findet auslaufende/gekuendigte Mietvertraege.

    Wann verwenden: Kuendigungsrisiko analysieren, Nachvermietung planen

    Returns:
        Liste gekuendigter Einheiten mit Miete, Gesamtmiete at risk
    """
    client = await get_client()

    if params.property_id or params.property_name:
        prop_id, prop_name = await resolve_property(params.property_id, params.property_name)
        if prop_id is None:
            return "Liegenschaft nicht gefunden."
        properties = [{'id': prop_id, 'name': prop_name}]
    elif params.portfolio_name:
        properties = await client.get_portfolio_properties(params.portfolio_name)
    else:
        properties = await client.get_all_properties()

    expiring = []
    total_rent_at_risk = 0.0

    for prop in properties:
        units = await client.get_property_units(prop['id'])
        for u in units:
            if u.get('is_parking', False):
                continue

            status = u.get('status', '').lower()
            if status == 'gekuendigt' or status == 'gekündigt':
                rent = u.get('net_rent', 0) or u.get('net_rent_projected', 0) or 0
                expiring.append(ExpiringItem(
                    id=u.get('id', 0),
                    name=u.get('unit_number', ''),
                    property_id=prop['id'],
                    property_name=prop['name'],
                    expiry_date="Gekuendigt",
                    days_until_expiry=0,
                    monthly_amount=rent
                ))
                total_rent_at_risk += rent

    result = ExpiringLeases(
        property_id=params.property_id,
        property_name=params.property_name,
        portfolio_name=params.portfolio_name,
        months_ahead=params.months_ahead,
        expiring_leases=expiring,
        total_count=len(expiring),
        total_monthly_rent_at_risk=total_rent_at_risk
    )

    return result.model_dump_json(indent=2)


@mcp.tool(
    name="mfiles_upcoming_refinancing",
    annotations={
        "title": "Anstehende Refinanzierungen",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_upcoming_refinancing(params: UpcomingRefinancingInput) -> str:
    """Findet Darlehen mit baldiger Zinsbindungsende.

    Wann verwenden: Refinanzierungsbedarf ermitteln, Zinsrisiko-Fruehwarnung

    Returns:
        Liste der Darlehen mit Bank, Restschuld, Zinsbindungsende, Tage bis Ablauf
    """
    client = await get_client()
    days_ahead = params.months_ahead * 30

    if params.property_id or params.property_name:
        prop_id, prop_name = await resolve_property(params.property_id, params.property_name)
        if prop_id is None:
            return "Liegenschaft nicht gefunden."
        all_mortgages = await client.get_property_mortgages(prop_id, prop_name)
    elif params.portfolio_name:
        properties = await client.get_portfolio_properties(params.portfolio_name)
        all_mortgages = []
        for prop in properties:
            mortgages = await client.get_property_mortgages(prop['id'], prop['name'])
            for m in mortgages:
                m['_property_id'] = prop['id']
                m['_property_name'] = prop['name']
            all_mortgages.extend(mortgages)
    else:
        all_mortgages = await client.get_all_mortgages()

    upcoming = []
    total_balance = 0.0
    total_payment = 0.0

    for m in all_mortgages:
        fixed_until = m.get('fixed_rate_until')
        if not fixed_until:
            continue

        days = calculate_days_until(fixed_until)
        if 0 <= days <= days_ahead:
            balance = m.get('outstanding_balance', 0)
            rate = m.get('interest_rate', 0)
            amort = m.get('amortization_rate', 2.0)
            monthly_payment = balance * (rate / 100) / 12 + balance * (amort / 100) / 12

            upcoming.append(ExpiringItem(
                id=m.get('id', 0),
                name=m.get('bank', 'Darlehen'),
                property_id=m.get('_property_id', 0),
                property_name=m.get('_property_name', ''),
                expiry_date=fixed_until,
                days_until_expiry=days,
                monthly_amount=monthly_payment
            ))
            total_balance += balance
            total_payment += monthly_payment

    upcoming.sort(key=lambda x: x.days_until_expiry)

    result = UpcomingRefinancing(
        property_id=params.property_id,
        property_name=params.property_name,
        portfolio_name=params.portfolio_name,
        months_ahead=params.months_ahead,
        mortgages=upcoming,
        total_count=len(upcoming),
        total_outstanding_balance=total_balance,
        total_monthly_payment_at_risk=total_payment
    )

    return result.model_dump_json(indent=2)


@mcp.tool(
    name="mfiles_get_invoices",
    annotations={
        "title": "Rechnungen (Placeholder)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_get_invoices(params: GetInvoicesInput) -> str:
    """Rechnungen/Kosten einer Liegenschaft (falls in M-Files verfuegbar).

    Hinweis: Diese Funktion erfordert Dokument-Verknuepfung in M-Files.
    """
    prop_id, prop_name = await resolve_property(params.property_id, params.property_name)
    if prop_id is None:
        return "Liegenschaft nicht gefunden."

    return json.dumps({
        "property_id": prop_id,
        "property_name": prop_name,
        "note": "Rechnungen erfordern Dokumenten-Verknuepfung in M-Files. Diese Funktion ist vorbereitet.",
        "alternative": "Nutzen Sie die bestehenden Skripte oder den /immobilien-analyst Skill."
    }, indent=2)


@mcp.tool(
    name="mfiles_get_unit_docs",
    annotations={
        "title": "Einheiten-Dokumente",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_get_unit_docs(params: GetUnitDocsInput) -> str:
    """Dokumente einer Einheit abrufen (Mietvertraege, Protokolle etc.).

    Wann verwenden: Mietvertrag einsehen, Dokumente zu Einheit finden
    """
    client = await get_client()

    unit_id = params.unit_id

    if params.unit_name and not unit_id:
        prop_id, prop_name = await resolve_property(params.property_id, params.property_name)
        if prop_id is None:
            return "Liegenschaft nicht gefunden. Bitte property_id oder property_name angeben."

        units = await client.get_property_units(prop_id)
        for u in units:
            if params.unit_name.lower() in str(u.get('unit_number', '')).lower() or \
               params.unit_name.lower() in str(u.get('unit_name', '')).lower():
                unit_id = u.get('id')
                break

        if not unit_id:
            return f"Einheit '{params.unit_name}' nicht gefunden."

    if not unit_id:
        return "Bitte unit_id oder unit_name (mit property_id/name) angeben."

    unit_info = await client._get_unit_details(unit_id)
    unit_number = unit_info.get('unit_number', '') if unit_info else ''
    unit_name_full = unit_info.get('unit_name', '') if unit_info else ''
    tenant = unit_info.get('tenant', '') if unit_info else ''

    direct_files = await client.get_unit_files(unit_id)
    contract_docs = await client.get_unit_contract_documents(unit_id)

    all_docs = []

    for f in direct_files:
        all_docs.append(DocumentInfo(
            file_id=f['file_id'],
            name=f['name'],
            extension=f['extension'],
            size_bytes=f.get('size_bytes', 0),
            object_type=132,
            object_id=unit_id,
            version=f.get('version', 0),
            created_date=f.get('created_date'),
            modified_date=f.get('modified_date')
        ))

    for f in contract_docs:
        all_docs.append(DocumentInfo(
            file_id=f['file_id'],
            name=f['name'],
            extension=f['extension'],
            size_bytes=f.get('size_bytes', 0),
            object_type=f.get('object_type', 0),
            object_id=f.get('object_id', 0),
            version=f.get('version', 0),
            created_date=f.get('created_date'),
            modified_date=f.get('modified_date'),
            contract_id=f.get('contract_id'),
            contract_name=f.get('contract_name')
        ))

    result = UnitDocuments(
        unit_id=unit_id,
        unit_name=unit_name_full,
        unit_number=unit_number,
        tenant=tenant,
        documents=all_docs,
        total_count=len(all_docs)
    )

    return result.model_dump_json(indent=2)


@mcp.tool(
    name="mfiles_get_property_docs",
    annotations={
        "title": "Liegenschafts-Dokumente",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_get_property_docs(params: GetPropertyDocsInput) -> str:
    """Alle Dokumente einer Liegenschaft und ihrer Einheiten.

    Wann verwenden: Dokumenten-Uebersicht, alle Mietvertraege einer Liegenschaft
    """
    client = await get_client()

    prop_id, prop_name = await resolve_property(params.property_id, params.property_name)
    if prop_id is None:
        return "Liegenschaft nicht gefunden."

    property_files = await client.get_property_files(prop_id)
    property_doc_infos = [
        DocumentInfo(
            file_id=f['file_id'],
            name=f['name'],
            extension=f['extension'],
            size_bytes=f.get('size_bytes', 0),
            object_type=130,
            object_id=prop_id,
            version=f.get('version', 0),
            created_date=f.get('created_date'),
            modified_date=f.get('modified_date')
        )
        for f in property_files
    ]

    unit_docs_list = []
    total_unit_docs = 0

    if params.include_unit_docs:
        units = await client.get_property_units(prop_id)
        for u in units:
            unit_id = u.get('id')
            if not unit_id:
                continue

            unit_files = await client.get_unit_files(unit_id)
            contract_docs = await client.get_unit_contract_documents(unit_id)

            all_unit_docs = []

            for f in unit_files:
                all_unit_docs.append(DocumentInfo(
                    file_id=f['file_id'],
                    name=f['name'],
                    extension=f['extension'],
                    size_bytes=f.get('size_bytes', 0),
                    object_type=132,
                    object_id=unit_id,
                    version=f.get('version', 0),
                    created_date=f.get('created_date'),
                    modified_date=f.get('modified_date')
                ))

            for f in contract_docs:
                all_unit_docs.append(DocumentInfo(
                    file_id=f['file_id'],
                    name=f['name'],
                    extension=f['extension'],
                    size_bytes=f.get('size_bytes', 0),
                    object_type=f.get('object_type', 0),
                    object_id=f.get('object_id', 0),
                    version=f.get('version', 0),
                    created_date=f.get('created_date'),
                    modified_date=f.get('modified_date'),
                    contract_id=f.get('contract_id'),
                    contract_name=f.get('contract_name')
                ))

            if all_unit_docs:
                unit_docs_list.append(UnitDocuments(
                    unit_id=unit_id,
                    unit_name=u.get('unit_name', ''),
                    unit_number=u.get('unit_number', ''),
                    tenant=u.get('tenant', ''),
                    documents=all_unit_docs,
                    total_count=len(all_unit_docs)
                ))
                total_unit_docs += len(all_unit_docs)

    result = PropertyDocuments(
        property_id=prop_id,
        property_name=prop_name,
        property_documents=property_doc_infos,
        unit_documents=unit_docs_list,
        total_property_docs=len(property_doc_infos),
        total_unit_docs=total_unit_docs
    )

    return result.model_dump_json(indent=2)


@mcp.tool(
    name="mfiles_download_doc",
    annotations={
        "title": "Dokument herunterladen",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True
    }
)
async def mfiles_download_doc(params: DownloadDocInput) -> str:
    """Laedt ein Dokument aus M-Files herunter und extrahiert Text.

    Wann verwenden: Mietvertrag lesen, Dokument-Inhalt analysieren
    """
    client = await get_client()

    content, file_info = await client.download_file(
        params.object_type, params.object_id, params.file_id
    )

    if content is None:
        result = DocumentContent(
            file_id=params.file_id,
            file_name="unknown",
            file_extension="",
            success=False,
            error_message=file_info.get('error', 'Download failed')
        )
        return result.model_dump_json(indent=2)

    filename = file_info.get('filename', f'file_{params.file_id}')
    extension = filename.split('.')[-1].lower() if '.' in filename else ''
    content_type = file_info.get('content_type', '')

    text_content = None
    saved_path = None

    if params.extract_text and (extension == 'pdf' or 'pdf' in content_type):
        try:
            try:
                import PyPDF2
                import io
                pdf_reader = PyPDF2.PdfReader(io.BytesIO(content))
                text_parts = []
                for page in pdf_reader.pages:
                    text_parts.append(page.extract_text() or '')
                text_content = '\n\n'.join(text_parts)
            except ImportError:
                try:
                    import pdfplumber
                    import io
                    with pdfplumber.open(io.BytesIO(content)) as pdf:
                        text_parts = []
                        for page in pdf.pages:
                            text_parts.append(page.extract_text() or '')
                        text_content = '\n\n'.join(text_parts)
                except ImportError:
                    temp_dir = tempfile.gettempdir()
                    saved_path = os.path.join(temp_dir, filename)
                    with open(saved_path, 'wb') as f:
                        f.write(content)
                    text_content = f"[PDF heruntergeladen nach {saved_path} - PyPDF2/pdfplumber nicht installiert]"
        except Exception as e:
            text_content = f"[Fehler bei PDF-Extraktion: {str(e)}]"

    elif extension in ['txt', 'csv', 'xml', 'json', 'html', 'htm']:
        try:
            text_content = content.decode('utf-8')
        except UnicodeDecodeError:
            try:
                text_content = content.decode('latin-1')
            except Exception:
                text_content = "[Konnte Textdatei nicht dekodieren]"

    else:
        temp_dir = tempfile.gettempdir()
        saved_path = os.path.join(temp_dir, filename)
        with open(saved_path, 'wb') as f:
            f.write(content)
        text_content = f"[Datei gespeichert unter: {saved_path}]"

    result = DocumentContent(
        file_id=params.file_id,
        file_name=filename,
        file_extension=extension,
        file_size=len(content),
        content_type=content_type,
        text_content=text_content,
        saved_path=saved_path,
        success=True
    )

    return result.model_dump_json(indent=2)


# =============================================================================
# Tool 19: Unit Version History
# =============================================================================

@mcp.tool(
    name="mfiles_get_unit_history",
    annotations={
        "title": "Einheiten-Versionshistorie",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_get_unit_history(params: GetUnitHistoryInput) -> str:
    """Versionshistorie einer Einheit abrufen - zeigt Mieter-, Status- und Mietaenderungen ueber alle Versionen.

    Wann verwenden: Vorherigen Mieter ermitteln, Kuendigungsdatum finden, Status-Aenderungen nachverfolgen
    (vermietet → gekuendigt → leer), Mietentwicklung analysieren.

    Gibt eine Timeline aller Aenderungen zurueck inkl. Mieter, Einheitenstatus, Miete und Workflow-Status.
    """
    client = await get_client()

    # Resolve unit_id (same pattern as mfiles_get_unit_docs)
    unit_id = params.unit_id

    if params.unit_name and not unit_id:
        prop_id, prop_name = await resolve_property(params.property_id, params.property_name)
        if prop_id is None:
            return "Liegenschaft nicht gefunden. Bitte property_id oder property_name angeben."

        units = await client.get_property_units(prop_id)
        for u in units:
            if params.unit_name.lower() in str(u.get('unit_number', '')).lower() or \
               params.unit_name.lower() in str(u.get('unit_name', '')).lower():
                unit_id = u.get('id')
                break

        if not unit_id:
            return f"Einheit '{params.unit_name}' nicht gefunden."

    if not unit_id:
        return "Bitte unit_id oder unit_name (mit property_id/name) angeben."

    # Get current unit info for context
    unit_info = await client._get_unit_details(unit_id)
    unit_name = unit_info.get('unit_name', '') if unit_info else ''
    unit_number = unit_info.get('unit_number', '') if unit_info else ''
    current_status = unit_info.get('status', '') if unit_info else ''
    current_tenant = unit_info.get('tenant', '') if unit_info else ''

    # Get version history
    history_data = await client.get_unit_version_history(unit_id)

    # Build version entries
    version_entries = []
    for v in history_data.get('versions', []):
        version_entries.append(UnitVersionEntry(
            version=v.get('version', 0),
            modified_date=v.get('modified_date'),
            modified_by=v.get('modified_by', ''),
            tenant=v.get('tenant', ''),
            status=v.get('status', ''),
            workflow_status=v.get('workflow_status', ''),
            net_rent=v.get('net_rent', 0.0),
            net_rent_projected=v.get('net_rent_projected', 0.0),
            changes=v.get('changes', []),
        ))

    result = UnitVersionHistory(
        unit_id=unit_id,
        unit_name=unit_name,
        unit_number=unit_number,
        current_status=current_status,
        current_tenant=current_tenant,
        total_versions=history_data.get('total_versions', 0),
        versions=version_entries,
        status_timeline=history_data.get('status_timeline', []),
    )

    return result.model_dump_json(indent=2)


# =============================================================================
# Tools 20-23: Object Type Discovery & Vorgänge
# =============================================================================

@mcp.tool(
    name="mfiles_discover_object_types",
    annotations={
        "title": "M-Files Objekttypen entdecken",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_discover_object_types(params: DiscoverObjectTypesInput) -> str:
    """Listet alle M-Files Objekttypen mit IDs auf.

    Wann verwenden: Herausfinden welche Objekttypen im Vault existieren,
    IDs fuer Vorgaenge/andere Typen ermitteln

    Returns:
        Liste aller Objekttypen mit ID, Name, Name Plural
    """
    client = await get_client()
    object_types = await client.get_object_types()

    type_infos = [
        ObjectTypeInfo(
            id=ot['id'],
            name=ot['name'],
            name_plural=ot.get('name_plural', ''),
            real_object_type=ot.get('real_object_type', True),
        )
        for ot in object_types
    ]

    result = ObjectTypeList(
        object_types=type_infos,
        total_count=len(type_infos),
    )

    if params.response_format == ResponseFormat.MARKDOWN:
        lines = ["# M-Files Objekttypen", ""]
        lines.append(f"**Gesamt:** {result.total_count} Objekttypen")
        lines.append("")
        lines.append("| ID | Name | Name (Plural) |")
        lines.append("|----|------|---------------|")
        for ot in result.object_types:
            lines.append(f"| {ot.id} | {ot.name} | {ot.name_plural} |")
        return "\n".join(lines)

    return result.model_dump_json(indent=2)


class VaultStructureInput(BaseModel):
    """Input for mfiles_vault_structure."""
    resource: str = Field(
        description=(
            "Vault structure resource to query. Options:\n"
            "  'workflows' - All workflows\n"
            "  'workflows/{id}/states' - States for a specific workflow\n"
            "  'workflows/{id}/statetransitions' - Allowed transitions for a workflow\n"
            "  'workflows/{id}/statetransitions?currentstate={state_id}' - Transitions from a specific state\n"
            "  'classes' - All object classes (optional: ?objtype={id})\n"
            "  'properties' - All property definitions\n"
            "  'properties/{id}' - Single property definition\n"
            "  'valuelists' - All value lists\n"
            "  'valuelists/{id}/items' - Items in a value list\n"
            "  'objecttypes' - All object types\n"
            "  'objecttypes/{id}/classes' - Classes for an object type"
        )
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.JSON,
        description="Output format: 'markdown' for human-readable or 'json' for raw API response"
    )


@mcp.tool(
    name="mfiles_vault_structure",
    annotations={
        "title": "M-Files Vault-Struktur abfragen",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_vault_structure(params: VaultStructureInput) -> str:
    """Fragt die M-Files Vault-Struktur ab: Workflows, States, Transitions, Klassen, Properties.

    Wann verwenden: Workflow-Strukturen verstehen, erlaubte State-Transitions ermitteln,
    Property-Definitionen nachschlagen, Klassen eines Objekt-Typs auflisten.

    Beispiele:
      resource='workflows' -> Alle Workflows mit IDs
      resource='workflows/109/states' -> Alle States des Mietermeldungs-Workflows
      resource='workflows/109/statetransitions?currentstate=185' -> Erlaubte Transitions ab "in Pruefung"
      resource='classes?objtype=139' -> Alle Klassen fuer Vorgaenge
      resource='properties/44' -> Property-Definition fuer "Assigned To"

    Returns:
        JSON oder Markdown der Vault-Struktur
    """
    client = await get_client()
    resource = params.resource

    # Value list items use /valuelists/{id}/items (no /structure/ prefix per official API docs)
    # But /structure/valuelists (listing all) does use the /structure/ prefix
    if resource.startswith('valuelists/') and '/items' in resource:
        endpoint = f'/{resource}'
    else:
        endpoint = f'/structure/{resource}'

    data = await client.get(endpoint)

    # Fallback: try alternate path if first attempt failed
    if data is None:
        alt_endpoint = f'/{resource}' if endpoint.startswith('/structure/') else f'/structure/{resource}'
        data = await client.get(alt_endpoint)

    if data is None:
        return json.dumps({"error": f"Keine Daten fuer {endpoint}. HTTP-Fehler - siehe Server-Logs."})

    if params.response_format == ResponseFormat.MARKDOWN:
        if isinstance(data, list):
            if len(data) == 0:
                return f"Keine Ergebnisse fuer `/structure/{params.resource}`"
            # Auto-format based on first item's keys
            keys = list(data[0].keys()) if data else []
            lines = [f"# Vault Structure: {params.resource}", ""]
            lines.append(f"**Ergebnisse:** {len(data)}")
            lines.append("")
            if keys:
                lines.append("| " + " | ".join(str(k) for k in keys) + " |")
                lines.append("| " + " | ".join("---" for _ in keys) + " |")
                for item in data:
                    lines.append("| " + " | ".join(str(item.get(k, '')) for k in keys) + " |")
            return "\n".join(lines)
        else:
            return f"# Vault Structure: {params.resource}\n\n```json\n{json.dumps(data, indent=2, ensure_ascii=False)}\n```"

    return json.dumps(data, indent=2, ensure_ascii=False)


@mcp.tool(
    name="mfiles_list_vorgaenge",
    annotations={
        "title": "Vorgaenge auflisten",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_list_vorgaenge(params: ListVorgaengeInput) -> str:
    """Listet alle Vorgaenge (Mietermeldung, Sanierung, Rechtsstreitigkeiten etc.) auf.

    Wann verwenden: Ueberblick ueber aktive Vorgaenge, Vorgaenge einer Liegenschaft finden,
    Status von Mietermeldungen/Sanierungen/Rechtsstreitigkeiten pruefen

    Returns:
        Liste der Vorgaenge mit Typ (Klasse), Status, verknuepfte Liegenschaften/Einheiten
    """
    client = await get_client()
    vorgaenge = await client.get_all_vorgaenge(
        property_filter=params.property_filter,
        status_id=params.status_id,
        class_id=params.class_id,
        limit=params.limit,
    )

    summaries = [
        VorgangSummary(
            id=v['id'],
            name=v.get('name', ''),
            class_name=v.get('class_name', ''),
            status=v.get('status', ''),
            workflow_status=v.get('workflow_status', ''),
            linked_properties=v.get('linked_properties', []),
            linked_units=v.get('linked_units', []),
            linked_companies=v.get('linked_companies', []),
            created_date=v.get('created_date'),
            modified_date=v.get('modified_date'),
        )
        for v in vorgaenge
    ]

    result = VorgaengeList(
        vorgaenge=summaries,
        total_count=len(summaries),
        property_filter=params.property_filter,
    )

    if params.response_format == ResponseFormat.MARKDOWN:
        lines = ["# Vorgänge", ""]
        if params.property_filter:
            lines.append(f"**Filter:** {params.property_filter}")
        lines.append(f"**Gesamt:** {result.total_count} Vorgänge")
        lines.append("")
        lines.append("| ID | Name | Klasse | Status | Liegenschaften | Einheiten |")
        lines.append("|----|------|--------|--------|----------------|-----------|")
        for v in result.vorgaenge:
            props = ", ".join(v.linked_properties) if v.linked_properties else "-"
            units = ", ".join(v.linked_units) if v.linked_units else "-"
            lines.append(f"| {v.id} | {v.name} | {v.class_name} | {v.workflow_status or v.status} | {props} | {units} |")
        return "\n".join(lines)

    return result.model_dump_json(indent=2)


@mcp.tool(
    name="mfiles_get_vorgang_details",
    annotations={
        "title": "Vorgang-Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_get_vorgang_details(params: GetVorgangDetailsInput) -> str:
    """Vollstaendige Metadaten eines Vorgangs inkl. aller Properties und verknuepfter Objekte.

    Wann verwenden: Details eines Vorgangs einsehen, verknuepfte Liegenschaften/Einheiten/Firmen
    ermitteln, Status und Workflow pruefen, optionale Dokumente abrufen

    Returns:
        Alle Properties, verknuepfte Objekte, Workflow-Status, optional Dokumente
    """
    client = await get_client()
    details = await client.get_vorgang_details(
        params.vorgang_id,
        include_documents=params.include_documents
    )

    if not details:
        return f"Vorgang {params.vorgang_id} nicht gefunden."

    doc_infos = []
    for d in details.get('documents', []):
        doc_infos.append(DocumentInfo(
            file_id=d['file_id'],
            name=d['name'],
            extension=d['extension'],
            size_bytes=d.get('size_bytes', 0),
            object_type=d.get('object_type', 0),
            object_id=d.get('object_id', 0),
            version=d.get('version', 0),
        ))

    linked_objs = [
        LinkedObjectInfo(
            object_type=lo['object_type'],
            id=lo.get('id'),
            name=lo.get('name', ''),
            property_name=lo.get('property_name', ''),
        )
        for lo in details.get('linked_objects', [])
    ]

    result = VorgangDetails(
        id=details['id'],
        object_type=details['object_type'],
        name=details['name'],
        class_name=details.get('class_name', ''),
        status=details.get('status', ''),
        workflow_status=details.get('workflow_status', ''),
        all_properties=details.get('all_properties', {}),
        linked_properties=details.get('linked_properties', []),
        linked_units=details.get('linked_units', []),
        linked_companies=details.get('linked_companies', []),
        linked_objects=linked_objs,
        created_date=details.get('created_date'),
        modified_date=details.get('modified_date'),
        created_by=details.get('created_by', ''),
        modified_by=details.get('modified_by', ''),
        documents=doc_infos,
        document_count=len(doc_infos),
    )

    if params.response_format == ResponseFormat.MARKDOWN:
        lines = [f"# Vorgang: {result.name}", ""]
        lines.append(f"- **ID:** {result.id}")
        lines.append(f"- **Klasse:** {result.class_name}")
        lines.append(f"- **Workflow-Status:** {result.workflow_status}")
        lines.append(f"- **Erstellt:** {result.created_date or '-'} von {result.created_by}")
        lines.append(f"- **Geaendert:** {result.modified_date or '-'} von {result.modified_by}")
        lines.append("")
        if result.linked_properties:
            lines.append(f"**Liegenschaften:** {', '.join(result.linked_properties)}")
        if result.linked_units:
            lines.append(f"**Einheiten:** {', '.join(result.linked_units)}")
        if result.linked_companies:
            lines.append(f"**Firmen:** {', '.join(result.linked_companies)}")
        lines.append("")
        lines.append("## Alle Properties")
        for k, v in result.all_properties.items():
            if v:
                lines.append(f"- **{k}:** {v}")
        if result.documents:
            lines.append("")
            lines.append(f"## Dokumente ({result.document_count})")
            for d in result.documents:
                lines.append(f"- {d.name}.{d.extension} (ID: {d.file_id}, Typ: {d.object_type}/{d.object_id})")
        return "\n".join(lines)

    return result.model_dump_json(indent=2)


@mcp.tool(
    name="mfiles_get_vorgang_documents",
    annotations={
        "title": "Vorgang-Dokumente herunterladen",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True
    }
)
async def mfiles_get_vorgang_documents(params: GetVorgangDocumentsInput) -> str:
    """Laedt alle Dokumente eines Vorgangs herunter und extrahiert Text (PDF/Text).

    Wann verwenden: Inhalt eines Vorgangs verstehen, alle Dokumente einer Mietermeldung/
    Sanierung/Rechtsstreitigkeit lesen

    Returns:
        Liste aller Dokumente mit extrahiertem Textinhalt
    """
    client = await get_client()

    # Get Vorgang name for context
    type_id = await client._get_vorgang_type_id()
    vorgang_name = ""
    if type_id:
        props = await client.get(f"/objects/{type_id}/{params.vorgang_id}/latest/properties")
        if props:
            for prop in props:
                if prop.get('PropertyDef') == 0:
                    vorgang_name = prop.get('TypedValue', {}).get('DisplayValue', '')
                    break

    raw_docs = await client.get_vorgang_documents(params.vorgang_id)

    doc_results = []
    for doc in raw_docs:
        doc_result = VorgangDocumentWithContent(
            file_id=doc['file_id'],
            name=doc['name'],
            extension=doc['extension'],
            size_bytes=doc.get('size_bytes', 0),
            object_type=doc['object_type'],
            object_id=doc['object_id'],
            source=doc.get('source', ''),
        )

        if params.extract_text:
            content, file_info = await client.download_file(
                doc['object_type'], doc['object_id'], doc['file_id']
            )
            if content is None:
                doc_result.success = False
                doc_result.error_message = file_info.get('error', 'Download failed')
            else:
                ext = doc.get('extension', '').lower()
                if ext == 'pdf':
                    try:
                        try:
                            import PyPDF2
                            import io
                            pdf_reader = PyPDF2.PdfReader(io.BytesIO(content))
                            text_parts = []
                            for page in pdf_reader.pages:
                                text_parts.append(page.extract_text() or '')
                            doc_result.text_content = '\n\n'.join(text_parts)
                        except ImportError:
                            try:
                                import pdfplumber
                                import io
                                with pdfplumber.open(io.BytesIO(content)) as pdf:
                                    text_parts = []
                                    for page in pdf.pages:
                                        text_parts.append(page.extract_text() or '')
                                    doc_result.text_content = '\n\n'.join(text_parts)
                            except ImportError:
                                doc_result.text_content = "[PDF - PyPDF2/pdfplumber nicht installiert]"
                    except Exception as e:
                        doc_result.text_content = f"[Fehler bei PDF-Extraktion: {str(e)}]"
                elif ext in ['txt', 'csv', 'xml', 'json', 'html', 'htm']:
                    try:
                        doc_result.text_content = content.decode('utf-8')
                    except UnicodeDecodeError:
                        try:
                            doc_result.text_content = content.decode('latin-1')
                        except Exception:
                            doc_result.text_content = "[Konnte Textdatei nicht dekodieren]"
                elif ext == 'msg':
                    # Outlook Message files. Most Mietermeldungen arrive as
                    # forwarded emails - without this branch the body stays
                    # binary and Hermes can't do a semantic recap.
                    try:
                        import extract_msg
                        import io
                        msg = extract_msg.openMsg(io.BytesIO(content))
                        parts = []
                        if msg.sender:
                            parts.append(f"Von: {msg.sender}")
                        if msg.to:
                            parts.append(f"An: {msg.to}")
                        if msg.cc:
                            parts.append(f"Cc: {msg.cc}")
                        if msg.date:
                            parts.append(f"Datum: {msg.date}")
                        if msg.subject:
                            parts.append(f"Betreff: {msg.subject}")
                        body = msg.body or ""
                        if not body and getattr(msg, 'htmlBody', None):
                            # Fallback: strip HTML very crudely
                            import re
                            body = re.sub(r'<[^>]+>', '', msg.htmlBody or '')
                        parts.append("")
                        parts.append(body.strip())
                        doc_result.text_content = "\n".join(parts)
                    except ImportError:
                        doc_result.text_content = "[MSG - extract-msg nicht installiert]"
                    except Exception as e:
                        doc_result.text_content = f"[Fehler bei MSG-Extraktion: {e}]"
                else:
                    doc_result.text_content = f"[Binaerdatei: {doc['name']}.{ext}, {len(content)} bytes]"

        doc_results.append(doc_result)

    result = VorgangDocuments(
        vorgang_id=params.vorgang_id,
        vorgang_name=vorgang_name,
        documents=doc_results,
        total_count=len(doc_results),
    )

    return result.model_dump_json(indent=2)


# =============================================================================
# WRITE TOOLS — Status Changes & Comments
# =============================================================================

OBJECT_TYPE_VORGANG = 139  # Vorgaenge
OBJECT_TYPE_DOKUMENT = 0   # Dokumente (Angebote sind Dokumente)
WORKFLOW_SANIERUNG = 110
WORKFLOW_ANGEBOTSPRUEFUNG = 113


@mcp.tool(
    name="mfiles_set_vorgang_status",
    annotations={
        "title": "Mietermeldungs-Status setzen",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_set_vorgang_status(params: SetVorgangStatusInput) -> str:
    """Setzt den Workflow-Status eines Mietermeldungs-Vorgangs in M-Files.

    Wann verwenden: Wenn Ari einen Vorgang als berechtigt/unberechtigt/erledigt etc.
    markieren will. IMMER erst Bestaetigung zeigen (Titel + Liegenschaft), dann setzen.

    Status-Aliases:
      berechtigt (186), unberechtigt (188), in-pruefung (185), in-behebung (187),
      erledigt (189), in-abrechnung (204), nachfrage (212), aufgeschoben (339)
    """
    status_key = params.status.lower().strip()
    if status_key not in MIETERMELDUNG_STATUS_MAP:
        return json.dumps({
            "error": f"Unbekannter Status: '{status_key}'",
            "erlaubt": list(MIETERMELDUNG_STATUS_MAP.keys())
        }, ensure_ascii=False)

    state_id = MIETERMELDUNG_STATUS_MAP[status_key]
    client = await get_client()
    title = await client.get_object_title(OBJECT_TYPE_VORGANG, params.vorgang_id)
    status_result = await client.set_workflow_status(OBJECT_TYPE_VORGANG, params.vorgang_id, state_id)

    if not status_result['ok']:
        return json.dumps({
            "error": f"Fehler beim Setzen des Status auf Vorgang {params.vorgang_id}",
            "vorgang": title,
            "details": status_result.get('error', 'Unbekannter Fehler'),
            "status_code": status_result.get('status_code')
        }, ensure_ascii=False)

    result = {
        "success": True,
        "vorgang_id": params.vorgang_id,
        "vorgang_titel": title,
        "neuer_status": status_key,
        "state_id": state_id
    }

    if params.kommentar:
        ok2 = await client.add_comment(OBJECT_TYPE_VORGANG, params.vorgang_id, params.kommentar)
        result["kommentar_gesetzt"] = ok2
        if not ok2:
            result["warnung"] = "Status gesetzt, aber Kommentar fehlgeschlagen"

    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool(
    name="mfiles_set_angebot_status",
    annotations={
        "title": "Angebot-Status setzen",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_set_angebot_status(params: SetAngebotStatusInput) -> str:
    """Setzt den Status eines Angebots im Workflow 'Angebotspruefung' (ID 113).

    Wann verwenden: Wenn Ari ein Angebot annehmen, ablehnen oder nachverhandeln will.
    IMMER erst Bestaetigung zeigen (Titel + Firma + Liegenschaft), dann setzen.

    Status-Aliases:
      angenommen (208), abgelehnt (207), nachverhandeln (206)
    """
    status_key = params.status.lower().strip()
    if status_key not in ANGEBOT_STATUS_MAP:
        return json.dumps({
            "error": f"Unbekannter Status: '{status_key}'",
            "erlaubt": list(ANGEBOT_STATUS_MAP.keys())
        }, ensure_ascii=False)

    state_id = ANGEBOT_STATUS_MAP[status_key]
    client = await get_client()
    title = await client.get_object_title(OBJECT_TYPE_DOKUMENT, params.angebot_id)
    status_result = await client.set_workflow_status(
        OBJECT_TYPE_DOKUMENT, params.angebot_id, state_id,
        workflow_id=WORKFLOW_ANGEBOTSPRUEFUNG
    )

    if not status_result['ok']:
        return json.dumps({
            "error": f"Fehler beim Setzen des Status auf Angebot {params.angebot_id}",
            "angebot": title,
            "details": status_result.get('error', 'Unbekannter Fehler'),
            "status_code": status_result.get('status_code')
        }, ensure_ascii=False)

    result = {
        "success": True,
        "angebot_id": params.angebot_id,
        "angebot_titel": title,
        "neuer_status": status_key,
        "state_id": state_id
    }

    if params.kommentar:
        ok2 = await client.add_comment(OBJECT_TYPE_DOKUMENT, params.angebot_id, params.kommentar)
        result["kommentar_gesetzt"] = ok2

    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool(
    name="mfiles_set_sanierung_status",
    annotations={
        "title": "Sanierungsvorgang-Status setzen",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def mfiles_set_sanierung_status(params: SetSanierungStatusInput) -> str:
    """Setzt den Workflow-Status eines Sanierungsvorgangs (Workflow: Sanierung, ID 110).

    Wann verwenden: Wenn Ari einen Sanierungsvorgang in den naechsten Status bringen will.
    IMMER erst Bestaetigung zeigen (Titel + Liegenschaft + was wird geaendert), dann setzen.

    Status-Aliases:
      vergabe (225), durchfuehrung (193), abnahme (230), abrechnung (204),
      nachfrage (212), abgeschlossen (231), ausschreibung (226),
      ausschreibung-schwarzbaum (224)
    """
    status_key = params.status.lower().strip()
    if status_key not in SANIERUNG_STATUS_MAP:
        return json.dumps({
            "error": f"Unbekannter Status: '{status_key}'",
            "erlaubt": list(SANIERUNG_STATUS_MAP.keys())
        }, ensure_ascii=False)

    state_id = SANIERUNG_STATUS_MAP[status_key]
    client = await get_client()
    title = await client.get_object_title(OBJECT_TYPE_VORGANG, params.vorgang_id)
    status_result = await client.set_workflow_status(
        OBJECT_TYPE_VORGANG, params.vorgang_id, state_id,
        workflow_id=WORKFLOW_SANIERUNG
    )

    if not status_result['ok']:
        return json.dumps({
            "error": f"Fehler beim Setzen des Status auf Sanierungsvorgang {params.vorgang_id}",
            "vorgang": title,
            "details": status_result.get('error', 'Unbekannter Fehler'),
            "status_code": status_result.get('status_code')
        }, ensure_ascii=False)

    result = {
        "success": True,
        "vorgang_id": params.vorgang_id,
        "vorgang_titel": title,
        "neuer_status": status_key,
        "state_id": state_id
    }

    if params.kommentar:
        ok2 = await client.add_comment(OBJECT_TYPE_VORGANG, params.vorgang_id, params.kommentar)
        result["kommentar_gesetzt"] = ok2

    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool(
    name="mfiles_add_vorgang_comment",
    annotations={
        "title": "Kommentar zu Vorgang hinzufuegen",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True
    }
)
async def mfiles_add_vorgang_comment(params: AddVorgangCommentInput) -> str:
    """Fuegt einen Kommentar zu einem M-Files Vorgang hinzu.

    Wann verwenden: Wenn Ari einen Kommentar oder eine Notiz zu einem Vorgang
    hinterlassen will, ohne den Status zu aendern.

    Standard object_type ist 139 (Vorgaenge). Fuer Dokumente/Angebote: 0.
    """
    client = await get_client()
    title = await client.get_object_title(params.object_type, params.vorgang_id)
    ok = await client.add_comment(params.object_type, params.vorgang_id, params.kommentar)

    if ok:
        return json.dumps({
            "success": True,
            "vorgang_id": params.vorgang_id,
            "vorgang_titel": title,
            "kommentar": params.kommentar
        }, ensure_ascii=False, indent=2)
    else:
        return json.dumps({
            "error": f"Kommentar konnte nicht gesetzt werden auf Vorgang {params.vorgang_id}",
            "vorgang": title
        }, ensure_ascii=False)



# =============================================================================
# Tool 30: View Items
# =============================================================================

@mcp.tool(
    annotations={"readOnlyHint": True, "openWorldHint": True}
)
async def mfiles_get_view_items(params: GetViewItemsInput) -> str:
    """
    Alle Objekte aus einer M-Files View laden.

    Wann verwenden:
    - Wenn eine bestimmte View abgefragt werden soll (z.B. View 117 = Mietermeldungen unerledigt)
    - Wenn Objekte nach einer in M-Files definierten Filterung abgerufen werden sollen
    - Fuer Reports die auf gespeicherten Views basieren

    Gibt alle Items mit ihren Properties zurueck, paginiert falls noetig.
    """
    client = await get_client()
    view_data = await client.get_view_items(params.view_id, limit=params.limit)

    items = view_data.get('items', [])
    results = []

    if params.include_properties:
        sem = asyncio.Semaphore(10)
        async def fetch_props(item):
            async with sem:
                obj = item.get('ObjectVersion', {})
                obj_ver = obj.get('ObjVer', {})
                oid = obj_ver.get('ID')
                otype = obj_ver.get('Type')
                if not oid or otype is None:
                    return None
                try:
                    props = await client.get(f'/objects/{otype}/{oid}/latest/properties') or []
                    pd = {}
                    for p in props:
                        prop_def = p.get('PropertyDef')
                        v = p.get('Value', {})
                        display = v.get('DisplayValue', '')
                        if not display and v.get('Lookup'):
                            display = v['Lookup'].get('DisplayValue', '')
                        if not display and v.get('Lookups'):
                            display = '; '.join(
                                l.get('DisplayValue', '') for l in v['Lookups'] if l.get('DisplayValue')
                            )
                        if not display and v.get('Value') is not None:
                            display = str(v['Value'])
                        pd[str(prop_def)] = display
                    return {
                        'id': oid,
                        'type': otype,
                        'title': obj.get('Title', ''),
                        'last_modified': obj.get('LastModifiedUtc', ''),
                        'created': pd.get('20', ''),
                        'properties': pd
                    }
                except Exception as e:
                    logger.error(f"Error fetching props for {otype}/{oid}: {e}")
                    return {
                        'id': oid,
                        'type': otype,
                        'title': obj.get('Title', ''),
                        'properties': {}
                    }

        results = await asyncio.gather(*[fetch_props(item) for item in items])
        results = [r for r in results if r is not None]
    else:
        for item in items:
            obj = item.get('ObjectVersion', {})
            obj_ver = obj.get('ObjVer', {})
            results.append({
                'id': obj_ver.get('ID'),
                'type': obj_ver.get('Type'),
                'title': obj.get('Title', ''),
                'last_modified': obj.get('LastModifiedUtc', ''),
            })

    output = {
        'view_id': params.view_id,
        'total': len(results),
        'more_results': view_data.get('more_results', False),
        'items': results
    }

    if params.format == "markdown":
        lines = [f"# View {params.view_id} - {len(results)} Objekte\n"]
        for r in results:
            lines.append(f"## [{r['id']}] {r['title']}")
            if r.get('properties'):
                for k, v in r['properties'].items():
                    if v:
                        lines.append(f"- PropDef {k}: {v}")
            lines.append("")
        return "\n".join(lines)

    return json.dumps(output, ensure_ascii=False, indent=2)


# =============================================================================
# =============================================================================
# Batched bulk-recap tool. Motivation: a conversational "recap all open
# Mietermeldungen" without batching forces Hermes to issue one MCP call
# per Vorgang (list + details + docs). With 17 Vorgaenge that becomes
# 30+ LLM roundtrips, and ChatGPT Plus rate-limits at ~40/3h. This tool
# returns EVERYTHING needed for a recap (list + properties + linked
# Liegenschaft/Einheit + Einzugsdatum + Dokumente + extracted MSG/PDF
# text) in a SINGLE response. Hermes then spends ~2 LLM calls to format
# a recap over 17 Vorgaenge instead of 30+.
#
# Mirrors the flow of mietermeldungsvorgaenge_bundle/mietermeldungen_recap.py
# with its asyncio.gather-per-batch optimisation.
# =============================================================================


class VorgaengeRecapBundleInput(BaseModel):
    """Input for mfiles_vorgaenge_recap_bundle."""
    model_config = ConfigDict(str_strip_whitespace=True)

    status_id: int = Field(
        ...,
        description=(
            "M-Files Workflow-Status (PropertyDef 39). Haeufige Werte: "
            "185=Mietermeldung in Pruefung, 205=Angebot zu pruefen, "
            "186=berechtigt, 188=unberechtigt. Pflicht - ohne Filter 1000+ Vorgaenge."
        )
    )
    class_id: Optional[int] = Field(
        default=None,
        description="Optional: nur Vorgaenge dieser Klasse (PropertyDef 100, z.B. 17=Angebot)."
    )
    limit: int = Field(default=50, ge=1, le=200, description="Max Vorgaenge (1-200).")
    fetch_docs: bool = Field(
        default=True,
        description="Ob Dokument-Inhalte mitgeliefert werden. False ist schneller wenn nur Liste gebraucht."
    )
    max_docs_per_vorgang: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Obergrenze Dokumente pro Vorgang (Schutz gegen Vorgaenge mit 100+ Attachments)."
    )


@mcp.tool(
    name="mfiles_vorgaenge_recap_bundle",
    annotations={
        "title": "Vorgaenge Bulk-Recap mit Dokumenten",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def mfiles_vorgaenge_recap_bundle(params: VorgaengeRecapBundleInput) -> str:
    """Bulk-Recap aller Vorgaenge mit einem Status in EINEM Tool-Call.

    Wann verwenden: Wenn Ari einen inhaltlichen Recap einer Gruppe Vorgaenge
    will ("recap der offenen Mietermeldungen", "fass die Angebote zusammen").
    Ersetzt list_vorgaenge + 17x get_vorgang_details + 17x get_vorgang_documents.
    Spart massiv LLM-calls und faellt nicht ins ChatGPT-Plus-Rate-Limit.

    Returns:
        JSON mit fields: status_id, count, vorgaenge[] (jeweils mit title, class,
        description, deadline, journal, assigned_to, linked_properties[],
        linked_units[], documents[] mit extracted text).
    """
    client = await get_client()
    type_id = await client._get_vorgang_type_id()
    if type_id is None:
        return json.dumps({"error": "Vorgang-Typ-ID nicht ermittelt"})

    query_parts = [f"limit={params.limit}", f"p39={params.status_id}"]
    if params.class_id is not None:
        query_parts.append(f"p100={params.class_id}")
    raw = await client.get(f"/objects/{type_id}?{'&'.join(query_parts)}")
    items = raw.get("Items", []) if isinstance(raw, dict) else (raw or [])
    if not items:
        return json.dumps({
            "status_id": params.status_id,
            "class_id": params.class_id,
            "count": 0,
            "vorgaenge": [],
        }, indent=2)

    # Parallel: properties for every Vorgang in one gather.
    ids = [it.get("ObjVer", {}).get("ID") or it.get("ID") for it in items]
    ids = [i for i in ids if i]
    prop_tasks = [client.get(f"/objects/{type_id}/{vid}/latest/properties") for vid in ids]
    props_all = await asyncio.gather(*prop_tasks, return_exceptions=True)

    def _parse(props, vid):
        info = {
            "id": vid, "title": "?", "class_name": "", "description": "",
            "deadline": "", "journal": "", "workflow_status": "",
            "assigned_to": [], "linked_properties": [], "linked_units": [],
            "linked_unit_ids": [], "created_date": None, "modified_date": None,
        }
        for prop in (props or []):
            pid = prop.get("PropertyDef")
            tv = prop.get("TypedValue", {}) or {}
            dv = tv.get("DisplayValue", "")
            if pid == 0 and dv:
                info["title"] = dv
            elif pid == 100:
                lk = tv.get("Lookup") or {}
                info["class_name"] = lk.get("DisplayValue", dv) if lk else dv
            elif pid == 39:
                lk = tv.get("Lookup") or {}
                info["workflow_status"] = lk.get("DisplayValue", dv) if lk else dv
            elif pid == 41:
                info["description"] = dv
            elif pid == 42:
                info["deadline"] = dv
            elif pid == 44:
                lks = tv.get("Lookups") or []
                info["assigned_to"] = [l.get("DisplayValue", "") for l in lks if l.get("DisplayValue")] or ([dv] if dv else [])
            elif pid == 1471:
                info["journal"] = dv
            for lk in (tv.get("Lookups") or []):
                ot, dn, oid = lk.get("ObjectType"), lk.get("DisplayValue", ""), lk.get("Item")
                if ot == 130 and dn:
                    info["linked_properties"].append(dn)
                elif ot == 132 and dn:
                    info["linked_units"].append(dn)
                    if oid:
                        info["linked_unit_ids"].append(oid)
            lk_s = tv.get("Lookup") or {}
            if lk_s:
                ot, dn, oid = lk_s.get("ObjectType"), lk_s.get("DisplayValue", ""), lk_s.get("Item")
                if ot == 130 and dn:
                    info["linked_properties"].append(dn)
                elif ot == 132 and dn:
                    info["linked_units"].append(dn)
                    if oid:
                        info["linked_unit_ids"].append(oid)
        return info

    vorgaenge: List[Dict[str, Any]] = []
    for vid, props, item in zip(ids, props_all, items):
        if isinstance(props, Exception):
            continue
        info = _parse(props, vid)
        if info["title"] == "?":
            info["title"] = item.get("Title", f"Vorgang {vid}")
        vorgaenge.append(info)

    # Parallel: docs + extracted text for every Vorgang.
    if params.fetch_docs:
        doc_tasks = [
            _fetch_vorgang_docs_with_text(client, v["id"], type_id, params.max_docs_per_vorgang)
            for v in vorgaenge
        ]
        docs_all = await asyncio.gather(*doc_tasks, return_exceptions=True)
        for v, docs in zip(vorgaenge, docs_all):
            v["documents"] = [] if isinstance(docs, Exception) else docs

    payload = {
        "status_id": params.status_id,
        "class_id": params.class_id,
        "count": len(vorgaenge),
        "vorgaenge": vorgaenge,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


# =============================================================================
# Batched decide/action tool. Motivation: after Ari looks at a recap he
# usually dispatches multiple decisions at once - "5244 unberechtigt,
# 5235 in-behebung, 5240 erledigt". Without a batch tool Hermes fires
# one set_vorgang_status call per decision (+ optional add_vorgang_comment),
# each triggering an LLM roundtrip. 3 decisions = 6 tool calls and 6+ LLM
# turns. This tool accepts the whole list and executes it server-side in
# one gather. 3 decisions -> 1 MCP call, 1-2 LLM turns.
# =============================================================================


_STATUS_MAPS_COMBINED = {
    **{f"mietermeldung.{k}": v for k, v in MIETERMELDUNG_STATUS_MAP.items()},
    **{f"angebot.{k}": v for k, v in ANGEBOT_STATUS_MAP.items()},
    **{f"sanierung.{k}": v for k, v in SANIERUNG_STATUS_MAP.items()},
    **MIETERMELDUNG_STATUS_MAP,
    **ANGEBOT_STATUS_MAP,
    **SANIERUNG_STATUS_MAP,
}


def _resolve_status(status: Any) -> Optional[int]:
    """Accepts either a status_id (int) or a status name string and
    resolves it to the M-Files state id.

    Supports short names ("unberechtigt" -> 188) via the three status maps
    defined in models.py as well as namespaced names ("angebot.angenommen"
    -> 208) for disambiguation when an ambiguous label could map to
    different ids.
    """
    if isinstance(status, int):
        return status
    if isinstance(status, str):
        key = status.strip().lower().replace(" ", "-").replace("_", "-")
        if key.isdigit():
            return int(key)
        return _STATUS_MAPS_COMBINED.get(key)
    return None


class VorgangDecision(BaseModel):
    """Single decision inside a batch."""
    model_config = ConfigDict(str_strip_whitespace=True)

    vorgang_id: int = Field(..., description="M-Files ID des Vorgangs.")
    status: Any = Field(
        ...,
        description=(
            "Zielstatus. Entweder numerische PropertyDef-39-ID (z.B. 188) oder "
            "Name-String (z.B. 'unberechtigt', 'in-behebung', 'angenommen', "
            "'abgelehnt'). Bei mehrdeutigen Namen: 'angebot.angenommen' oder "
            "'mietermeldung.unberechtigt' etc."
        ),
    )
    comment: Optional[str] = Field(
        default=None,
        description="Optionaler Kommentar der vor dem Status-Push an den Vorgang angehaengt wird."
    )
    object_type: int = Field(
        default=139,
        description="M-Files Objekttyp. Default 139 (Vorgaenge). Aenderung selten noetig."
    )
    workflow_id: Optional[int] = Field(
        default=None,
        description="Optional. Wenn None: Client liest den aktuellen Workflow des Objekts - das ist der empfohlene Default."
    )


class VorgaengeDecideBatchInput(BaseModel):
    """Input for mfiles_vorgaenge_decide_batch."""
    model_config = ConfigDict(str_strip_whitespace=True)

    decisions: List[VorgangDecision] = Field(
        ...,
        description="Liste der auszufuehrenden Entscheidungen. Alle parallel via asyncio.gather."
    )
    dry_run: bool = Field(
        default=False,
        description="Wenn True: nur validieren (Status-Namen aufloesen, IDs pruefen), aber nicht setzen. Fuer Preview-Then-Confirm Flow nuetzlich - Hermes kann Ari den geplanten Run zeigen bevor er tatsaechlich an M-Files geht."
    )


@mcp.tool(
    name="mfiles_vorgaenge_decide_batch",
    annotations={
        "title": "Mehrere Vorgaenge entscheiden (Batch)",
        "readOnlyHint": False,
        "destructiveHint": False,  # Status-change, reversible
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def mfiles_vorgaenge_decide_batch(params: VorgaengeDecideBatchInput) -> str:
    """Setzt Workflow-Status und optional Kommentare fuer mehrere Vorgaenge in EINEM Aufruf.

    Wann verwenden: nach einem Recap, wenn Ari mehrere Entscheidungen
    hintereinander mitteilt ("5244 unberechtigt, 5235 in-behebung Kommentar
    Handwerker beauftragt, 5240 erledigt"). Hermes parst das in eine
    decisions-Liste und ruft diesen EINEN Tool auf. Alle Status-Pushes +
    Kommentare laufen parallel server-side.

    **Preview-Then-Confirm (SOUL-Pflicht)**: Vor diesem Aufruf **IMMER**
    `dry_run=True` setzen und Ari den aufgeloesten Plan zeigen. Erst nach
    explizitem OK den echten Run (`dry_run=False`).

    Returns:
        JSON mit `results` (pro decision: ok, final_status_id, error?)
        und `total`, `succeeded`, `failed`, `dry_run`.
    """
    client = await get_client()
    results: List[Dict[str, Any]] = []

    async def _one(d: VorgangDecision) -> Dict[str, Any]:
        state_id = _resolve_status(d.status)
        if state_id is None:
            return {
                "vorgang_id": d.vorgang_id,
                "requested_status": d.status,
                "ok": False,
                "error": f"Unbekannter Status '{d.status}'. Bekannte: {sorted(_STATUS_MAPS_COMBINED.keys())[:20]}...",
            }
        if params.dry_run:
            return {
                "vorgang_id": d.vorgang_id,
                "requested_status": d.status,
                "resolved_status_id": state_id,
                "comment_preview": d.comment,
                "ok": True,
                "dry_run": True,
            }

        # Real run: optional comment BEFORE state change (comments stay
        # readable in the old-state context if the state transition would
        # strip PropertyDef 33).
        comment_ok = True
        comment_error: Optional[str] = None
        if d.comment:
            try:
                comment_ok = await client.add_comment(d.object_type, d.vorgang_id, d.comment)
            except Exception as e:
                comment_ok = False
                comment_error = str(e)

        status_result = await client.set_workflow_status(
            d.object_type, d.vorgang_id, state_id, workflow_id=d.workflow_id
        )
        return {
            "vorgang_id": d.vorgang_id,
            "requested_status": d.status,
            "resolved_status_id": state_id,
            "ok": bool(status_result.get("ok")),
            "error": status_result.get("error"),
            "comment_set": comment_ok,
            "comment_error": comment_error,
        }

    results = list(await asyncio.gather(*[_one(d) for d in params.decisions], return_exceptions=False))

    payload = {
        "total": len(results),
        "succeeded": sum(1 for r in results if r.get("ok")),
        "failed": sum(1 for r in results if not r.get("ok")),
        "dry_run": params.dry_run,
        "results": results,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    mcp.run()
