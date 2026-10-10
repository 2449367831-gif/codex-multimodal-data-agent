from __future__ import annotations

import json
import os
import unicodedata
from pathlib import Path

import pymysql


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / "config" / "pickup.env"
OUTPUT_FILE = ROOT / "outputs" / "019f8e3d-f760-7d30-9f83-2221c7bf45a2" / "sku_name_query.json"


REQUESTED_SKUS = [
    "5001070081210220",
    "XHHYSZB1KG",
    "5001010012120600",
    "5001070075000220",
    "5001060024000800",
    "HJXH-MLYCZ280g",
    "XHZMQDSX260G",
    "XH-ZRXGZJJ500G",
    "HJXH-XJDJP100g",
    "XH-KTXZB300g",
    "XH-CYB900G",
    "QY-YWKC500G",
    "XHHRZDNY500G",
    "XH-ZRYMZJJ500G",
    "HJXH-XPZXYT500g",
    "QY-HJXHDTY500G",
    "QY-WGFZ200GW",
    "QY-HJXHKZCJP1000G",
    "HJXH-LSZSB400G",
    "HJXH-MSLL450G",
    "XHDHMHZMTY420G",
    "HJXH-TLJ280g",
    "HJXH-RSHFLBZ280g",
    "QY-XHPJFZ200G",
    "QY-YMKC500G",
    "QY-HJKC508G",
    "QY-SHJYZ",
    "QY-XHPG500G",
    "XH-WCXGB300g",
    "XHDHMHSTY420G",
    "XH-SZB1KG",
    "XH-HSZB600G",
    "QY-HJKC500G",
    "XH-KTZZB300g",
    "HJXH-LSZSB800G",
    "XH-SZB003",
    "XH-SZB800G",
    "XH-XRCXL360g",
    "QY-HJXHPSDTP600G",
    "HJXH-XRXLB1KG",
    "HDH-HJWKC500g",
    "XHKZCJP500G",
    "XH-XCJMH500g",
    "HJXH-SLJ258g",
    "HJXH-XGXRSM1KG",
    "HJXH-ZRXGZJ1KG",
    "QY-YQNN200213",
    "HJXH-XGXRSM500g",
    "MLB-ZQNP5T",
    "QY-YWKC508G",
    "HJXH-HJJ280g",
    "HJXHHY400G",
    "QY-HJXHPSDTP480G",
    "HJXH-ZRYMZJ1KG",
    "XH-MXGFWJRJXLW140G",
    "XHSSSCCSCW400g",
    "XH-MSLLNL100g",
    "QY-XHTLNP100G",
    "QY-HJXHJMH1000G",
    "XH-MXGFWJRJARLW140G",
    "XHJTSCY500G",
    "HJXH-9CNRPS300g",
    "QY-HJXHZQNPTZ1KG",
    "XH-SZB006",
    "XHJCJDBPBZ500G",
    "XHNXMT240g",
    "XH-JDGB1.8KG",
    "HJXH-HRZDNY1kg",
    "XHDSYXMZC125G",
    "5001010070000125",
    "XHZRMGCGKYW450g",
    "XHXJJPAELW80g",
    "XHZRMGCGKXLW450g",
    "5001060011120700",
    "XHLBSRW150g",
    "XHZRDCSJ500G",
    "XHZRXGSJ500G",
    "XH-7CNRPS180g",
    "HJXHJDKS 180G",
    "XH-PGPS180g",
    "XH-7CNLPS180g",
    "HJXH-PGPS180g",
    "XHXJJPHJW80g",
    "XJJP80g*4",
    "XHXLFSBPBZ420G",
    "XHXRGTB300G",
    "HJXH-WDFYWRC500g",
    "XHZRDCSJ1KG",
    "XHZRXGSJ1KG",
    "XHXRB800G",
    "XHMGCSM1.1KG",
    "XHMZSJB500G",
    "XHLBSRW300g",
    "XHXRCYW400G",
    "XHWCR200g",
    "XHXRCHJW400G",
    "XHHJWKC250G",
    "XHMSZSJ270G",
    "XHMSHDJ270G",
    "MLBHBJNYDTP480G",
    "MLBHBJDTY500G",
    "MLBHZRCYW400g",
    "XHHMSM400G",
    "XHSZTS360G",
    "CZSC100g",
    "XHMJHYP250G",
    "XHJCHZ780G",
    "HJXHDHMHZMTY500G",
    "HDHXHPG500g",
    "XHHYSZB1KG",
    "XHLTFWKC400g",
    "XHZSJHS150G",
    "XHZQXLNP150g",
    "XHZRPG200g",
    "XHZRPG500g",
    "XHXJJPAELW500g",
    "5001010011120800G",
    "XHLXHTB420G",
    "XHZSJXY150G",
    "XHJTSCY450gX",
    "5001030060000500",
    "5001020050000450",
    "XHAELCG320g",
    "5001020051120400",
    "HJXH-7CNLPS180g",
    "XHZSNRJ120g",
    "5001010011120600",
    "5001070071100200",
    "XHJCJDZPXB540G",
    "5001070014210448",
    "XHSGLMXLB200G（LSHM）",
    "XHDDKCYW500g（LSHM）",
    "XHDDKCHJW500g（LSHM）",
    "5001070074000200",
    "XHZRYMZJJ1KG",
    "XHXGXRSM1.1KG",
    "XHAELCZ300g",
    "5001020040000220",
    "XHXYY500G",
    "5001030040000500",
    "XHJCJDBPBZ420G",
    "XHJCJDBPBZ500G（LSHM）",
    "XHHJNRYM720G",
    "XHXRCHJW400gX",
    "XHNYDTP300G",
    "XHMZSJB600G",
    "5001020043120180",
    "XHDYY500G",
    "MLBXJJRPG500g",
    "XHAGSNRB100g",
    "5003030011121000",
    "XHXRCYW400gX",
    "5001020040000950",
    "5001070014000500",
    "XHQCXGBPBZ420G",
    "5001070015220600",
    "XHNRXB520g",
    "5001010071000125",
    "5001060103000500",
    "XHZRXGSJ500G（LSHM）",
    "5001020141000160",
    "5001030080000500",
    "5001010014000500",
    "5001070073100200",
    "5001020140000200",
    "XHZRDCSJ500G（LSHM）",
    "5001030060001000",
    "XHDKLSDSM450G",
    "5001020110000300",
    "5001060141000400",
    "XHXJJPHJW500g",
    "HJXH-FQSS280g",
    "5001070072100200",
    "5001070016000360",
    "XHXTSLJ280G",
    "HJXH-WDFHJWRC500g",
    "5001020085000500",
    "ZH202211241423",
    "XHZRSCXB520g",
    "5001010012120800G",
    "5001080020000100",
    "5001080045000280",
    "5001060069000200",
    "5001070013000400",
    "5001060068000200",
    "5001060111000362",
    "5001030200000500",
    "5001010100000500",
    "5001070402000100",
    "5001070400000100",
    "5001060026001000",
    "5001070700000252",
]


def load_env(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def normalized(value: str, remove_spaces: bool = False) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    if remove_spaces:
        text = "".join(text.split())
    return text


def main() -> None:
    load_env(ENV_FILE)
    connection = pymysql.connect(
        host=os.environ["PICKUP_DB_HOST"],
        port=int(os.environ.get("PICKUP_DB_PORT", "9030")),
        user=os.environ["PICKUP_DB_USER"],
        password=os.environ["PICKUP_DB_PASSWORD"],
        database=os.environ.get("PICKUP_DB_NAME", "ywzt_ods"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=20,
        read_timeout=120,
        write_timeout=30,
    )
    unique_skus = list(dict.fromkeys(REQUESTED_SKUS))
    placeholders = ",".join(["%s"] * len(unique_skus))
    sql = f"""
        SELECT sku_code, MAX(sku_name) AS sku_name, 'master_sku' AS source
        FROM master_sku
        WHERE sku_code IN ({placeholders})
        GROUP BY sku_code
        UNION ALL
        SELECT sku_code, MAX(sku_name) AS sku_name, 'master_sku_unit' AS source
        FROM master_sku_unit
        WHERE sku_code IN ({placeholders})
        GROUP BY sku_code
        UNION ALL
        SELECT sku_code, MAX(sku_name) AS sku_name, 'scm_warehous_manage' AS source
        FROM scm_warehous_manage
        WHERE sku_code IN ({placeholders})
        GROUP BY sku_code
        UNION ALL
        SELECT sku_code, MAX(sku_name) AS sku_name, 'scm_warehous_manage_3d' AS source
        FROM scm_warehous_manage_3d
        WHERE sku_code IN ({placeholders})
        GROUP BY sku_code
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, unique_skus * 4)
            source_rows = list(cursor.fetchall())
    finally:
        connection.close()

    by_exact: dict[str, list[dict]] = {}
    by_normalized: dict[str, list[dict]] = {}
    by_compact: dict[str, list[dict]] = {}
    for row in source_rows:
        code = str(row.get("sku_code") or "").strip()
        name = str(row.get("sku_name") or "").strip()
        item = {"sku_code": code, "sku_name": name, "source": row.get("source")}
        by_exact.setdefault(code, []).append(item)
        by_normalized.setdefault(normalized(code), []).append(item)
        by_compact.setdefault(normalized(code, True), []).append(item)

    def choose(candidates: list[dict]) -> dict | None:
        if not candidates:
            return None
        ranked = sorted(
            candidates,
            key=lambda row: (
                {
                    "master_sku": 0,
                    "master_sku_unit": 1,
                    "scm_warehous_manage": 2,
                    "scm_warehous_manage_3d": 3,
                }.get(str(row.get("source") or ""), 9),
                0 if row.get("sku_name") else 1,
                str(row.get("sku_name") or ""),
            ),
        )
        return ranked[0]

    output_rows = []
    for index, requested in enumerate(REQUESTED_SKUS, start=1):
        match = choose(by_exact.get(requested, []))
        match_type = "exact"
        if not match:
            match = choose(by_normalized.get(normalized(requested), []))
            match_type = "normalized"
        if not match:
            compact_candidates = by_compact.get(normalized(requested, True), [])
            distinct_codes = {row["sku_code"] for row in compact_candidates}
            if len(distinct_codes) == 1:
                match = choose(compact_candidates)
                match_type = "space_normalized"
        output_rows.append(
            {
                "index": index,
                "requested_sku": requested,
                "matched_sku": match.get("sku_code") if match else "",
                "sku_name": match.get("sku_name") if match else "",
                "match_status": "matched" if match and match.get("sku_name") else "unmatched",
                "match_type": match_type if match else "none",
                "source": match.get("source") if match else "",
            }
        )

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    result = {
        "requested_count": len(REQUESTED_SKUS),
        "unique_requested_count": len(unique_skus),
        "duplicate_count": len(REQUESTED_SKUS) - len(unique_skus),
        "matched_count": sum(row["match_status"] == "matched" for row in output_rows),
        "unmatched_count": sum(row["match_status"] == "unmatched" for row in output_rows),
        "rows": output_rows,
    }
    OUTPUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "rows"}, ensure_ascii=False))
    print(str(OUTPUT_FILE))


if __name__ == "__main__":
    main()
