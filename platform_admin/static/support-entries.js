(() => {
  const status = document.querySelector('#copy-status');
  document.querySelectorAll('[data-copy]').forEach(button => {
    button.addEventListener('click', async () => {
      const url = new URL(button.dataset.copy, location.origin).href;
      try {
        await navigator.clipboard.writeText(url);
        status.textContent = '客服链接已复制。';
      } catch (_) {
        status.textContent = '浏览器不允许自动复制，请复制此链接：' + url;
      }
    });
  });
})();
