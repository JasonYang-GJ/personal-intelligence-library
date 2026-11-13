chrome.runtime.sendMessage({op: 'status'}).then(data => {
  const labels = {disabled:'等待开启验证',reading:'正在后台读取',waiting:'等待下一次后台继续',complete:'本轮读取结束',failed:'验证需要处理'};
  const s = data.status;
  document.getElementById('status').textContent = s ? `${labels[s.phase] || s.phase}：${s.detail}（${new Date(s.at).toLocaleString()}）` : '扩展已加载，正在建立本机连接。';
}).catch(() => {document.getElementById('status').textContent = '暂时无法取得状态，请回到项目聊天查看。';});
