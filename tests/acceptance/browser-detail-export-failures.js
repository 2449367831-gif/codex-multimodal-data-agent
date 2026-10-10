async (page) => {
  const check=(value,message)=>{if(!value)throw Error(message);};
  const mode=value=>page.evaluate(mode=>fetch('/v1/fixture',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})}),value);
  await page.locator('#questionInput').fill('近14天实际销售出库明细');
  await page.getByRole('button',{name:'提交取数问题',exact:true}).click();
  await mode('bad-hash');
  await page.getByRole('button',{name:'导出全部明细',exact:true}).click();
  await page.getByText('本批未通过完整性校验，未继续写入。可重试续传。',{exact:true}).waitFor();
  check(await page.getByText('文件已保存 0 / 5 行',{exact:true}).count()===1,'bad hash advanced checkpoint');
  await mode('interrupt');
  await page.getByRole('button',{name:'导出全部明细',exact:true}).click();
  await page.getByText('连接中断，已保存进度保留。服务恢复后可继续导出。',{exact:true}).waitFor();
  await page.evaluate(async()=>{
    const handle=await (await navigator.storage.getDirectory()).getFileHandle('synthetic-export.csv');
    window.originalFixtureFile=await (await handle.getFile()).arrayBuffer();
    const writer=await handle.createWritable({keepExistingData:true});await writer.seek(0);await writer.write('x');await writer.close();
  });
  await page.getByRole('button',{name:'继续导出全部',exact:true}).click();
  await page.getByText('文件内容与已保存进度不一致，已停止续写。原文件未被覆盖。',{exact:true}).waitFor();
  const before=await page.evaluate(()=>fetch('/v1/fixture').then(r=>r.json()));
  check(before.reads.join()==='0,0,1','changed local file triggered more data reads');
  await page.evaluate(async()=>{const handle=await(await navigator.storage.getDirectory()).getFileHandle('synthetic-export.csv');const writer=await handle.createWritable();await writer.write(window.originalFixtureFile);await writer.close();});
  await mode('changed');
  await page.getByRole('button',{name:'继续导出全部',exact:true}).click();
  await page.getByText('数据或口径已变化，不能继续拼接旧文件。请重新查询后导出。',{exact:true}).waitFor();
  check(await page.getByText('文件已保存 2 / 5 行',{exact:true}).count()===1,'source change advanced checkpoint');
  check(!(await page.locator('body').innerText()).includes('INTERNAL MUST NOT DISPLAY'),'internal error displayed');
  check(await page.getByText('完整文件已保存',{exact:true}).count()===0,'failed export marked complete');
  await page.getByRole('button',{name:'重新查询原问题',exact:true}).waitFor();
  return {passed:true,hash_rejected:true,changed_local_file_preserved:true,source_change_stops_append:true};
}
