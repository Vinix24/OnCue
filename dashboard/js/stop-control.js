document.getElementById('stop-server-btn').addEventListener('click', async () => {
  if (!confirm(window.t('header.stop_server_confirm'))) return;
  try {
    await window.copilotAuthReady;
    await fetch('/api/shutdown', {
      method: 'POST',
      headers: { ...window.copilotAuthHeaders() },
    });
  } catch (_e) {
    // expected: server stops before the response arrives
  }
  const title = window.t('header.stop_server_done_title');
  const message = window.t('header.stop_server_done_message');
  document.body.innerHTML =
    '<div style="padding:60px;text-align:center;font-family:system-ui;">' +
    `<h1>${title}</h1>` +
    `<p>${message}</p>` +
    '</div>';
});
