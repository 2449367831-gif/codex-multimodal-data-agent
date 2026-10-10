"""Extract only reviewed local finished-goods definitions, leaving other scope intact."""

from pathlib import Path
import re

import sqlglot
from sqlglot import exp
import yaml


def finished_goods_definition(question: str, *, contract_root: Path):
    if "成品定义" not in question and "例外" not in question:
        return question, ()
    text = question
    definition = re.search(r"成品定义为\s*([^，,。\n]+)[，,。]?", text)
    if definition:
        expression = definition[1].strip().strip("`")
        try:
            tree = sqlglot.parse_one(expression, read="doris")
        except sqlglot.errors.ParseError:
            return None
        if not (isinstance(tree, exp.EQ) and isinstance(tree.this, exp.Column)
                and tree.this.name == "sku_group" and tree.this.table == "master_sku"
                and isinstance(tree.expression, exp.Literal) and tree.expression.this == "50"):
            return None
        text = text[:definition.start()] + text[definition.end():]
    elif "成品定义" in text:
        return None
    exceptions = re.search(r"(?:同时)?保留\s*SKU\s*`?([0-9]+)`?\s*和\s*`?([0-9]+)`?\s*两个例外[，,。]?", text, re.I)
    if exceptions:
        try:
            contract = yaml.safe_load((contract_root / "metrics/sales.finished_goods_exceptions.yaml").read_text(encoding="utf-8"))
            approved_codes = tuple(contract["filters"]["exception_skus"])
            approved = contract["status"] == "active" and contract["approval"]["state"] == "approved"
        except (OSError, KeyError, TypeError, yaml.YAMLError):
            return None
        if not approved or set(exceptions.groups()) != set(approved_codes) or "成品" not in text:
            return None
        text = text[:exceptions.start()] + text[exceptions.end():]
    else:
        approved_codes = ()
    if "例外" in text or "保留" in text:
        return None
    return text, approved_codes
