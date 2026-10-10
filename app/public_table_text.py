"""Shared screening for model result headers and public table text."""

import re


UNAPPROVED_CONTRACT_MARKERS = (
    "尚待业务审批", "尚待审批", "待业务审批", "待审批", "尚未审批发布",
    "尚未发布", "未审批发布", "candidate contract", "unapproved contract",
)


def answer_contains_unapproved_contract_marker(answer):
    if not isinstance(answer, str):
        return False
    normalized = re.sub(r"\s+", "", answer.casefold())
    return any(re.sub(r"\s+", "", marker.casefold()) in normalized
               for marker in UNAPPROVED_CONTRACT_MARKERS)


PUBLIC_TABLE_UNSAFE_TEXT = re.compile(
    r"(?:```|<\s*(?:script|iframe|object|embed)\b|"
    r"\b(?:select|insert|update|delete|drop|truncate|alter|create|merge|with|from|join|where|union|having|group\s+by|order\s+by|information_schema)\b|"
    r"\b(?:sql|bindings?|credentials?|password|secret|access[_ -]?token|api[_ -]?key|webhook)\b|"
    r"(?:查询语句|SQL语句|数据库凭据|审计内容|执行日志|原始数据行|原始结果行|字段列表|表结构)|"
    r"\b[A-Za-z][A-Za-z0-9_]*\.[A-Za-z][A-Za-z0-9_]*\b|`)",
    re.IGNORECASE,
)
_BUSINESS_LABEL_TOKENS = re.compile(r"SKU|DMS|EA|TOP|2C|2B", re.IGNORECASE)
_STANDALONE_BUSINESS_LABELS = {"sku": "SKU", "2c/2b": "2C/2B"}
_PRIVATE_LABEL_TEXT = re.compile(
    r"凭据|密码|密钥|令牌|口令|账号|账户|授权|审计|日志|堆栈|校验|验证|诊断|"
    r"字段|表名|(?<!仓)库名|数据库|原始行|执行明细|内部|元数据|提示词|指令|忽略|系统消息"
)


def business_column_label(value: str) -> str | None:
    """Screen a label; it does not establish the column's business semantics."""
    if not isinstance(value, str):
        return None
    label = value.strip()
    if label.casefold() in _STANDALONE_BUSINESS_LABELS:
        return _STANDALONE_BUSINESS_LABELS[label.casefold()]
    if (not label or len(label) > 40 or not re.search(r"[\u4e00-\u9fff]", label)
            or PUBLIC_TABLE_UNSAFE_TEXT.search(label) or _PRIVATE_LABEL_TEXT.search(label)):
        return None
    residue = _BUSINESS_LABEL_TOKENS.sub("", label)
    if re.search(r"[^\u4e00-\u9fff0-9 ()（）%％/·-]", residue):
        return None
    stack = []
    for char in label:
        if char in "(（":
            stack.append(char)
        elif char in ")）":
            if not stack or stack.pop() != {")": "(", "）": "（"}[char]:
                return None
    return None if stack else label


def business_result_title(value: str) -> str | None:
    """Titles may include business identifiers, not only column vocabulary."""
    if not isinstance(value, str):
        return None
    title = value.strip()
    if (not title or len(title) > 120
            or PUBLIC_TABLE_UNSAFE_TEXT.search(title) or _PRIVATE_LABEL_TEXT.search(title)
            or re.search(r"[^\u4e00-\u9fffA-Za-z0-9 ()（）%％/·-]", title)):
        return None
    return title
