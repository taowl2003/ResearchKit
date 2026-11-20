(() => {
  'use strict';
  const MAX_BYTES = 64 * 1024 * 1024;
  const safeName = value => String(value || 'dataset').replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').replace(/[. ]+$/g, '').slice(0, 120) || 'dataset';
  const heading = value => String(value || '未命名数据集').replace(/[\r\n]+/g, ' ').replace(/[\\`*_{}\[\]<>#]/g, '\\$&');
  function download(blob, name) {
    const url = URL.createObjectURL(blob), link = document.createElement('a');
    link.href = url; link.download = name; document.body.append(link);
    try { link.click(); } finally { link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
  }
  async function build(entries, {combined = false, progress = () => {}} = {}) {
    if (!entries.length) throw new Error('当前筛选结果为空，没有可导出的说明。');
    if (!combined && entries.length !== 1) throw new Error('请选择一个数据集导出 README。');
    const parts = combined ? ['# 数据集说明汇编\n\n', `共 ${entries.length} 个数据集，按导出时的筛选条件汇总。\n\n`] : [];
    let bytes = new Blob(parts).size;
    for (let index = 0; index < entries.length; index += 1) {
      const entry = entries[index]; progress(`正在读取说明 ${index + 1}/${entries.length}：${entry.name || entry.id}`);
      let content;
      if (entry.hasReadme === false) {
        if (!combined) throw new Error('此数据集未提供 README.md。');
        content = '未提供 README.md。\n';
      } else {
        const info = await entry.load();
        if (info?.complete === false || typeof info?.original !== 'string') {
          throw new Error(`“${entry.name || entry.id}”的完整 README 尚未取得。请由教师刷新扫描后重试；本次没有导出部分文件。`);
        }
        content = combined ? String(info.markdown ?? '') || '（README 正文为空）\n' : info.original;
      }
      const section = combined ? [
        `## ${index + 1}. ${heading(entry.name || entry.id)}\n\n`,
        `标识：${heading(entry.id)}\n\n`,
        entry.path ? `目录：${heading(entry.path)}\n\n` : '',
        entry.tags?.length ? `标签：${entry.tags.map(heading).join('、')}\n\n` : '',
        content, '\n\n---\n\n',
      ].join('') : content;
      bytes += new Blob([section]).size;
      if (bytes > MAX_BYTES) throw new Error('说明合计超过 64 MiB，请缩小筛选范围后分批导出；本次没有导出部分文件。');
      parts.push(section);
    }
    return new Blob(parts, {type: 'text/markdown;charset=utf-8'});
  }
  function bind(control, status, loadEntries, {combined = false, filename} = {}) {
    control.onclick = async () => {
      if (control.disabled) return;
      control.disabled = true;
      const progress = value => { status.textContent = value; };
      try {
        progress('正在准备说明…');
        const entries = await loadEntries();
        const blob = await build(entries, {combined, progress});
        download(blob, filename || (combined ? '数据集说明汇编.md' : `${safeName(entries[0].name || entries[0].id)}-README.md`));
        progress(combined ? `已导出 ${entries.length} 个数据集的说明（含全部匹配分页；未提供 README 的数据集会明确标出）。` : '已导出原始 README.md。');
      } catch (error) { progress(`导出失败：${error.message}`); }
      finally { control.disabled = false; }
    };
  }
  window.ResearchKitReadmeExport = {build, bind};
})();
