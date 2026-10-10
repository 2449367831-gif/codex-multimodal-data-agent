"""Expose reconciled name provenance without joining names to quantities."""

from ..sku_name_quality import compile_sku_name_quality, reconcile_sku_names
from ..semantic_types import SemanticAnswer, SemanticRuntimeError, _display_table
from .base import MetricHandler


class SkuNameQualityHandler(MetricHandler):
    def run(self, request):
        sql=compile_sku_name_quality(start=request.start,end=request.end,scope=request.scope)
        # The compiler scopes facts before observing their names. Reapplying
        # product-name filters here would hide conflicting master observations.
        raw=self.execution._query_checked(sql,purpose='SKU name provenance and actual outbound',business=True)
        if len(raw)>500:
            raise SemanticRuntimeError('名称检查结果超过上限，未展示部分结果',reason='result_truncated')
        second=self.execution._query_checked(sql,purpose='SKU name provenance stability verification',business=True)
        if raw != second:
            raise SemanticRuntimeError('查询期间名称或出库数据发生变化，暂未展示结果',reason='sku_names_changed')
        try:
            checked=reconcile_sku_names(raw)
        except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
            raise SemanticRuntimeError('名称及出库数量未通过核对，暂未展示结果',reason='sku_names_invalid') from exc
        rows=[]
        for item in checked:
            row={key:item[key] for key in ('cargo_owner','sku_code','sku_name','quantity','outbound_count','shipment_name','order_name')}
            row.update(master_name_variants=item['master_variants'],
                       missing_name_rows=sum(item[f'{source}_missing'] for source in ('master','shipment','order')),
                       missing_name_sources=sum(item[f'{source}_rows']==0 for source in ('master','shipment','order')),
                       name_variants=item['all_name_variants'],
                       name_status='；'.join([('主数据名称' if item['name_source']=='master_sku' else '显示编码'),
                           *(['名称不一致'] if item['name_conflict'] else []),*(['存在缺失'] if item['name_missing'] else [])]))
            rows.append(row)
        window='全部时间' if request.start is None else f'{request.start} 00:00 至 {request.end} 00:00（不含结束时点）'
        notes=[
            '展示名称采用当前商品主数据中的唯一名称；缺失或冲突时显示编码。出库或订单名称无法唯一确定时留空，不代表记录不存在。商品按编码关联，不因名称不同拆分数量。',
            '空名称记录数汇总三处来源的空名称记录；缺失来源数表示整处来源未匹配。不同名称数按三处来源去重。订单名称检查包含未删除记录的各状态，不表示订单请求量。',
            '出库数量按实际销售出库口径核对；名称为当前记录，不是历史名称快照。两次读取一致不等于原子快照，最近出库时间不代表同步鲜度。',
        ]
        table=_display_table(table_id='sales_sku_name_quality',title='SKU名称与实际出库核对',caption=window,
            columns=[{'key':key,'kind':'number' if key in ('quantity','outbound_count','master_name_variants','missing_name_rows','missing_name_sources','name_variants') else 'text'} for key in (
                'cargo_owner','sku_code','sku_name','quantity','outbound_count','shipment_name','order_name',
                'master_name_variants','missing_name_rows','missing_name_sources','name_variants','name_status')],
            rows=rows,empty_label='查询成功，当前范围没有有效销售出库记录')
        table['notes']=notes
        if raw:
            table['summary']=[{'label':'实际销售出库','value':str(raw[0]['total_quantity']),'unit':'EA'},
                              {'label':'出库单数','value':str(raw[0]['total_outbound_count']),'unit':'单'}]
        text=f'已核对{len(rows)}个货主与商品分组；名称不一致{sum(i["name_conflict"] for i in checked)}组，名称或来源缺失{sum(i["name_missing"] for i in checked)}组。' if rows else '查询成功，当前范围没有有效销售出库记录。'
        return SemanticAnswer(text,table)
