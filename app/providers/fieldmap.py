"""Vendor column heading -> our field name.

A leaf module: it imports nothing from the app except the standard library, so both the
CSV importer (`app/services/prospecting.py`) and the directory scraper
(`app/providers/prospecting.py`) can use it without the provider layer depending on the
service layer.

Why one shared table instead of two: if the scraper kept its own copy of these aliases,
the two would drift and the failure would be silent. A column would stop mapping, import as
None, and the candidate would score low for a reason that looks like bad data rather than a
broken mapping. That exact class of bug ("a rationale that contradicts its own number") has
already cost this project two rounds of debugging.
"""
from __future__ import annotations

import re

# Real exports do not use our column names. "Est Volume TEU", "Annual TEU", "货量" and "teu"
# are the same field; without a mapping the volume silently imports as None, which then
# scores as a low-volume lead -- a wrong answer that looks like a data problem.
HEADER_ALIASES: dict[str, str] = {
    # Identity
    'company': 'company', 'company name': 'company', 'shipper': 'company',
    '公司': 'company', '企业': 'company', '公司名称': 'company', '企业名称': 'company',
    # Exhibitor / member directories label the same column their own way.
    'exhibitor': 'company', 'exhibitor name': 'company', 'member': 'company',
    '参展商': 'company', '展商': 'company', '展商名称': 'company', '会员单位': 'company',
    'country': 'country', 'market': 'country', '国家': 'country', '国家地区': 'country',
    'industry': 'industry', 'sector': 'industry', 'category': 'industry',
    '行业': 'industry', '主营产品': 'industry',
    'est_volume_teu': 'est_volume_teu', 'est volume teu': 'est_volume_teu',
    'est_volume': 'est_volume_teu', 'annual teu': 'est_volume_teu',
    'volume': 'est_volume_teu', 'teu': 'est_volume_teu', '货量': 'est_volume_teu',
    # Reachability
    'email': 'email', 'e-mail': 'email', 'mail': 'email', '邮箱': 'email', '电子邮箱': 'email',
    'contact': 'contact_name', 'contact name': 'contact_name', '联系人': 'contact_name',
    'title': 'contact_title', 'job title': 'contact_title', '职位': 'contact_title',
    'phone': 'phone', 'tel': 'phone', 'telephone': 'phone', '电话': 'phone', '联系电话': 'phone',
    'website': 'website', 'site': 'website', 'url': 'website', '网址': 'website', '官网': 'website',
    'trade_lane': 'trade_lane', 'lane': 'trade_lane', 'route': 'trade_lane', '航线': 'trade_lane',
    'source_ref': 'source_ref', 'id': 'source_ref', 'record id': 'source_ref', '编号': 'source_ref',
    # Customs records have **two** parties on every row, and which one is the lead depends on
    # what you sell. Mapping both to `company` would silently keep whichever column the vendor
    # happened to put second, so they get separate names and the caller picks a side.
    '采购商': 'importer', '进口商': 'importer', '买家': 'importer', '买方': 'importer',
    'importer': 'importer', 'buyer': 'importer', 'consignee': 'importer', '进口商名称': 'importer',
    '出口商': 'exporter', '供应商': 'exporter', '发货人': 'exporter', '卖方': 'exporter',
    'exporter': 'exporter', 'supplier': 'exporter', 'shipper name': 'exporter',
    # Shipment facts. Kept in `raw_payload` so the row stays auditable even though only a
    # handful of them become first-class columns.
    'hs code': 'hs_code', 'hs编码': 'hs_code', '商品编码': 'hs_code', '海关编码': 'hs_code',
    'product': 'product', '商品': 'product', '产品': 'product',
    'amount': 'amount', 'value': 'amount', '金额': 'amount', '美元金额': 'amount',
    'quantity': 'quantity', '数量': 'quantity',
    'port of loading': 'port_of_loading', '起运港': 'port_of_loading', '装货港': 'port_of_loading',
    'port of discharge': 'port_of_discharge', '目的港': 'port_of_discharge', '卸货港': 'port_of_discharge',
    'date': 'record_date', '日期': 'record_date', '报关日期': 'record_date', '提单日期': 'record_date',
}


def field_name(raw: str) -> str:
    """Map a heading to our field name, or return a slug of it.

    Unknown columns fall through as a slug rather than being dropped, so a vendor adding a
    column shows up in the payload instead of disappearing without trace.
    """
    key = (raw or '').strip()
    if not key:
        return ''
    low = key.lower()
    if low in HEADER_ALIASES:
        return HEADER_ALIASES[low]
    normalised = re.sub(r'[^a-z0-9]+', '_', low).strip('_')
    return HEADER_ALIASES.get(normalised, normalised)
