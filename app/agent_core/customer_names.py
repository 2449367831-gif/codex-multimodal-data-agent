"""Customer labels preserve identity and expose missing or conflicting master data."""

from dataclasses import dataclass

from sqlglot import exp

from .semantic_types import SemanticRuntimeError, _parse_decimal, _parse_source_datetime


def _key(value):
    if not isinstance(value,str) or not value.strip() or len(value)>200 or any(ord(c)<32 for c in value):
        raise ValueError("invalid customer identity or policy value")
    return value.strip()


def _literal(value):
    return exp.Literal.string(value).sql(dialect="doris")


@dataclass(frozen=True, slots=True)
class CustomerNamePolicy:
    price_group: str
    merged_name: str
    exception_codes: tuple[str, ...]

    def __post_init__(self):
        if self.price_group != _key(self.price_group) or self.merged_name != _key(self.merged_name):
            raise ValueError("policy values must be normalized")
        if not isinstance(self.exception_codes,tuple) or not 1 <= len(self.exception_codes) <= 200:
            raise ValueError("explicit customer exceptions required")
        if any(code != _key(code) for code in self.exception_codes) or len(set(self.exception_codes)) != len(self.exception_codes):
            raise ValueError("invalid customer exceptions")

    @classmethod
    def from_contract(cls, contract):
        rule=contract["filters"]["name_merge"]
        return cls(str(rule["price_group"]),str(rule["display_name"]),tuple(rule["exception_codes"]))


def compile_customer_names(policy: CustomerNamePolicy, *, owners=(), customer_keys=None):
    if isinstance(owners,(str,bytes)) or not isinstance(owners,(tuple,list)):
        raise ValueError("owners must be an explicit sequence")
    owner_keys=tuple(dict.fromkeys(_key(owner) for owner in owners))
    if len(owner_keys)>200 or (owner_keys and customer_keys is not None):
        raise ValueError("conflicting or excessive customer scope")
    scope="TRUE"
    if customer_keys is not None:
        if not isinstance(customer_keys,(tuple,list)) or not 1 <= len(customer_keys)<=200:
            raise ValueError("invalid requested customer keys")
        keys=[]
        for key in customer_keys:
            if not isinstance(key,(tuple,list)) or len(key)!=2:
                raise ValueError("invalid requested customer key")
            keys.append((_key(key[0]),_key(key[1])))
        keys=tuple(dict.fromkeys(keys))
        requested=" UNION ALL ".join(f"SELECT {_literal(owner)} AS cargo_owner,{_literal(code)} AS customer_code" for owner,code in keys)
        prefix=f"requested_keys AS ({requested}),"
        scope="EXISTS (SELECT 1 FROM requested_keys k WHERE k.cargo_owner=TRIM(s.company_code) AND k.customer_code=TRIM(s.code))"
        key_query="SELECT cargo_owner,customer_code FROM requested_keys"
    else:
        prefix=""
        if owner_keys:
            scope="TRIM(s.company_code) IN ("+",".join(_literal(owner) for owner in owner_keys)+")"
        key_query="SELECT DISTINCT cargo_owner,customer_code FROM source_rows WHERE cargo_owner IS NOT NULL AND customer_code IS NOT NULL"
    exceptions=",".join(_literal(code) for code in policy.exception_codes)
    merge=f"price_variants=1 AND price_group={_literal(policy.price_group)} AND customer_code NOT IN ({exceptions})"
    return f"""WITH {prefix} source_rows AS (
 SELECT NULLIF(TRIM(s.company_code),'') AS cargo_owner,NULLIF(TRIM(s.code),'') AS customer_code,
   NULLIF(TRIM(s.name),'') AS original_name,NULLIF(TRIM(s.price_group),'') AS price_group,s.updated AS source_updated
 FROM ywzt_ods.master_store s WHERE {scope}
), customer_keys AS ({key_query}), name_stats AS (
 SELECT cargo_owner,customer_code,COUNT(*) AS source_record_count,
   COUNT(DISTINCT original_name) AS name_variants,
   SUM(CASE WHEN original_name IS NULL THEN 1 ELSE 0 END) AS missing_name_rows,
   CASE WHEN COUNT(DISTINCT original_name)=1 THEN MIN(original_name) END AS original_name,
   COUNT(DISTINCT price_group)+MAX(CASE WHEN price_group IS NULL THEN 1 ELSE 0 END) AS price_variants,
   SUM(CASE WHEN price_group IS NULL THEN 1 ELSE 0 END) AS missing_price_rows,
   CASE WHEN COUNT(DISTINCT price_group)+MAX(CASE WHEN price_group IS NULL THEN 1 ELSE 0 END)=1
     THEN MIN(price_group) END AS price_group
 FROM source_rows WHERE cargo_owner IS NOT NULL AND customer_code IS NOT NULL
 GROUP BY cargo_owner,customer_code
), controls AS (
 SELECT (SELECT COUNT(*) FROM customer_keys) AS group_count,
   COALESCE(SUM(CASE WHEN cargo_owner IS NOT NULL AND customer_code IS NOT NULL THEN 1 ELSE 0 END),0) AS total_source_records,
   MAX(source_updated) AS source_latest_updated,
   COALESCE(SUM(CASE WHEN cargo_owner IS NOT NULL AND customer_code IS NOT NULL AND source_updated IS NULL THEN 1 ELSE 0 END),0) AS missing_update_rows,
   COALESCE(SUM(CASE WHEN cargo_owner IS NULL OR customer_code IS NULL THEN 1 ELSE 0 END),0) AS blank_key_count
 FROM source_rows
), controlled AS (
 SELECT k.cargo_owner,k.customer_code,n.original_name,n.price_group,
   COALESCE(n.source_record_count,0) AS source_record_count,
   COALESCE(n.name_variants,0) AS name_variants,COALESCE(n.missing_name_rows,0) AS missing_name_rows,
   COALESCE(n.price_variants,0) AS price_variants,COALESCE(n.missing_price_rows,0) AS missing_price_rows,
   CASE WHEN n.customer_code IS NULL THEN 1 ELSE 0 END AS missing_master,
   CASE WHEN k.customer_code IN ({exceptions}) THEN 1 ELSE 0 END AS is_exception,
   c.group_count,c.total_source_records,c.blank_key_count,c.source_latest_updated,c.missing_update_rows
 FROM controls c LEFT JOIN customer_keys k ON TRUE
 LEFT JOIN name_stats n ON n.cargo_owner=k.cargo_owner AND n.customer_code=k.customer_code
 WHERE k.customer_code IS NOT NULL OR c.blank_key_count>0
)
SELECT *,CASE WHEN {merge} THEN {_literal(policy.merged_name)} ELSE original_name END AS display_name,
 CASE WHEN {merge} THEN 1 ELSE 0 END AS merge_applied
FROM controlled ORDER BY cargo_owner,customer_code LIMIT 501"""


def _count(value):
    number=_parse_decimal(value)
    if number<0 or number!=number.to_integral_value():
        raise ValueError("invalid customer count")
    return int(number)


def reconcile_customer_names(rows,policy: CustomerNamePolicy, *, complete_limit=500):
    if type(complete_limit) is not int or not 1 <= complete_limit <= 100000 or len(rows)>complete_limit:
        raise ValueError("customer result truncated")
    result,seen=[],set()
    total,records_sum,freshness=None,0,None
    count_fields=("source_record_count","name_variants","missing_name_rows","price_variants","missing_price_rows","missing_master","is_exception","merge_applied")
    for row in rows:
        if _count(row["blank_key_count"]):
            raise ValueError("customer source has blank identity keys")
        key=(_key(row.get("cargo_owner")),_key(row.get("customer_code")))
        if key in seen or _count(row["group_count"])!=len(rows):
            raise ValueError("duplicate or incomplete customer keys")
        seen.add(key)
        current_total=_count(row["total_source_records"])
        if total is None:
            total=current_total
        elif total!=current_total:
            raise ValueError("inconsistent customer record controls")
        missing_updates=_count(row["missing_update_rows"])
        latest=row["source_latest_updated"]
        if missing_updates>current_total or (latest is None)!=(missing_updates==current_total):
            raise ValueError("invalid customer freshness controls")
        if latest is not None:
            _parse_source_datetime(latest,timezone_name="Asia/Shanghai")
        current_freshness=(latest,missing_updates)
        if freshness is None:
            freshness=current_freshness
        elif freshness!=current_freshness:
            raise ValueError("inconsistent customer freshness controls")
        counts={field:_count(row[field]) for field in count_fields}
        records,variants,missing=(counts[field] for field in ("source_record_count","name_variants","missing_name_rows"))
        price_variants,missing_price=counts["price_variants"],counts["missing_price_rows"]
        name,price=row["original_name"],row["price_group"]
        if missing>records or variants>records-missing or (variants==0)!=(records==missing):
            raise ValueError("invalid customer name observations")
        if (variants==1 and (not isinstance(name,str) or not name.strip())) or (variants!=1 and name is not None):
            raise ValueError("invalid unique customer name")
        if missing_price>records or price_variants>records-missing_price+int(missing_price>0) or (price_variants==0)!=(records==0):
            raise ValueError("invalid price group observations")
        if records>0 and ((records==missing_price and price_variants!=1) or (0<missing_price<records and price_variants<2)):
            raise ValueError("missing price group was hidden")
        has_unique_price=price_variants==1 and missing_price==0
        if (has_unique_price and (not isinstance(price,str) or not price.strip())) or (not has_unique_price and price is not None):
            raise ValueError("invalid unique price group")
        expected_exception=int(key[1] in policy.exception_codes)
        expected_merge=int(has_unique_price and price==policy.price_group and not expected_exception)
        expected_name=policy.merged_name if expected_merge else name
        if counts["missing_master"]!=int(records==0) or counts["is_exception"]!=expected_exception or counts["merge_applied"]!=expected_merge or row["display_name"]!=expected_name:
            raise ValueError("customer display rule mismatch")
        records_sum+=records
        result.append({"cargo_owner":key[0],"customer_code":key[1],"original_name":name,"price_group":price,
                       "display_name":expected_name,"source_latest_updated":latest,"missing_update_rows":missing_updates,**counts})
    if rows and records_sum!=total:
        raise ValueError("customer source record reconciliation mismatch")
    return result


def retrieve_customer_names(policy, query, *, owners=(), customer_keys=None,
                            page_size=500, max_rows=100000, max_bytes=64*1024*1024, timeout_seconds=180):
    from .sales_pages import retrieve_sales_groups
    sql=compile_customer_names(policy,owners=owners,customer_keys=customer_keys)

    def read_page(page_sql, *, purpose):
        rows=query(page_sql,purpose="customer names: bounded master-data page")
        if any(_count(row["blank_key_count"]) for row in rows):
            raise SemanticRuntimeError("客户资料缺少货主或客户编码，暂未展示不完整结果",reason="customer_identity_invalid")
        return rows

    try:
        rows=retrieve_sales_groups(sql,read_page,page_size=page_size,max_rows=max_rows,max_bytes=max_bytes,timeout_seconds=timeout_seconds)
        return reconcile_customer_names(rows,policy,complete_limit=max_rows)
    except SemanticRuntimeError as exc:
        if exc.reason != "sales_page_invalid":
            raise
        raise SemanticRuntimeError("客户资料分批读取存在缺页或变化，暂未展示结果",reason="customer_page_invalid") from exc
    except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
        raise SemanticRuntimeError("客户名称及资料完整性未通过核对，暂未展示结果",reason="customer_names_invalid") from exc
