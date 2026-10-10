from pathlib import Path
import os,json,math
from datetime import date
import pymysql
from PIL import Image,ImageDraw,ImageFont
ROOT=Path.cwd(); OUT=ROOT/'outputs'/'在售SKU表_20260817'; OUT.mkdir(parents=True,exist_ok=True)
for s in (ROOT/'build'/'lingshi_dashboard_full_clean_20260722142304'/'config'/'pickup.env').read_text(encoding='utf-8').splitlines():
    if '=' in s and not s.lstrip().startswith('#'):
        k,v=s.split('=',1);os.environ[k.strip()]=v.strip().strip('"').strip("'")
c=pymysql.connect(host=os.environ['PICKUP_DB_HOST'],port=int(os.environ.get('PICKUP_DB_PORT','9030')),user=os.environ['PICKUP_DB_USER'],password=os.environ['PICKUP_DB_PASSWORD'],database='ywzt_ods',charset='utf8mb4',cursorclass=pymysql.cursors.DictCursor,read_timeout=180)
sql="""WITH sold AS (SELECT d.sku_code,MAX(h.ship_at) last_ship_at,SUM(d.ship_qty) ship_qty_90d FROM scm_sales_shipment_header h JOIN scm_sales_shipment_detail d ON d.outbound_code=h.outbound_code WHERE h.ship_at>=CURDATE()-INTERVAL 90 DAY AND h.ship_at<CURDATE() AND h.outbound_code LIKE 'HJXH%' AND h.status IN(700,900,701,901) AND(h.ref_order_type IN('JYCK','HHCK','BFCK','QTCK') OR h.ref_order_type LIKE 'SO%') AND IFNULL(h._fdl_marked_deleted,'false')='false' AND IFNULL(d._fdl_marked_deleted,'false')='false' GROUP BY d.sku_code),box AS(SELECT sku_code,MAX(ea_convert_quantity) box_spec FROM master_sku_unit WHERE unit=40 AND IFNULL(ea_convert_quantity,0)>0 GROUP BY sku_code) SELECT s.sku_code,MAX(m.sku_name) sku_name,MAX(m.sku_sale_status) sku_sale_status,MAX(m.sku_launch_date) sku_launch_date,b.box_spec,s.last_ship_at,s.ship_qty_90d FROM sold s JOIN master_sku m ON m.sku_code=s.sku_code LEFT JOIN box b ON b.sku_code=s.sku_code WHERE m.sku_group='50' GROUP BY s.sku_code,b.box_spec,s.last_ship_at,s.ship_qty_90d ORDER BY MAX(m.sku_name),s.sku_code"""
with c.cursor() as cur:cur.execute(sql);rows=cur.fetchall()
c.close()
for r in rows:
 r['last_ship_at']=r['last_ship_at'].strftime('%Y-%m-%d') if r['last_ship_at'] else '';r['box_spec']='' if r['box_spec'] is None else '%g'%float(r['box_spec']);r['ship_qty_90d']=int(r['ship_qty_90d'] or 0);r['sku_sale_status']=str(r['sku_sale_status'] or '')
fp=r'C:\Windows\Fonts\msyh.ttc';font=ImageFont.truetype(fp,25);bold=ImageFont.truetype(fp,31);small=ImageFont.truetype(fp,21)
W,margin,title_h,head_h,row_h=2000,55,138,58,48; cols=[80,340,760,180,160,190,180];hdr=['序号','SKU编码','商品名称','销售状态','箱规（EA/箱）','最近出库','近90天出库EA'];per=31;files=[]
for pi,start in enumerate(range(0,len(rows),per),1):
 page=rows[start:start+per];im=Image.new('RGB',(W,title_h+head_h+len(page)*row_h+65),'#F8FAFC');d=ImageDraw.Draw(im);d.rectangle((0,0,W,title_h),fill='#0F3D5E');d.text((margin,25),'在售 SKU 清单',font=bold,fill='white');d.text((margin,75),f'口径：近90天（截至 {date.today()}）存在有效成品出库；共 {len(rows)} 个 SKU  |  第 {pi}/{math.ceil(len(rows)/per)} 页',font=small,fill='#D7E7F5');y=title_h;x=margin
 for w,h in zip(cols,hdr):d.rectangle((x,y,x+w,y+head_h),fill='#176B87');d.text((x+10,y+15),h,font=font,fill='white');x+=w
 y+=head_h
 for k,r in enumerate(page,start+1):
  x=margin;bg='#FFFFFF' if k%2 else '#EAF3F7';vals=[str(k),r['sku_code'],r['sku_name'] or '',r['sku_sale_status'],r['box_spec'],r['last_ship_at'],f"{r['ship_qty_90d']:,}"]
  for w,val in zip(cols,vals):
   d.rectangle((x,y,x+w,y+row_h),fill=bg,outline='#D3E1E8');txt=str(val)
   while d.textbbox((0,0),txt,font=small)[2]>w-18 and len(txt)>3:txt=txt[:-2]+'…'
   d.text((x+9,y+13),txt,font=small,fill='#163A4D');x+=w
  y+=row_h
 f=OUT/f'在售SKU表_{pi:02d}.png';im.save(f);files.append(str(f))
(OUT/'在售SKU表_数据.json').write_text(json.dumps({'generated_at':str(date.today()),'definition':'近90天有效出库的 sku_group=50 成品 SKU','count':len(rows),'rows':rows},ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'count':len(rows),'pages':len(files),'files':files},ensure_ascii=False))
