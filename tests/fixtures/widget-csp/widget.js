// NOT a replacement Widget. Exercise the exact upstream dependencies and native
// endpoint builders in Chromium without requiring a running Chatwoot database.
import { createApp, h, ref, onMounted } from 'vue';
import { createI18n } from 'vue-i18n';
import { getContrastingTextColor } from '@chatwoot/utils';
import endpoints from '/contract/api/endPoints';

const i18n = createI18n({ legacy: false, locale: 'en', messages: {
  en: { heading: '在线客服', message: '消息', send: '发送' },
} });
async function call(endpoint, method = 'GET') {
  const response = await fetch(endpoint.url, { method,
    headers: { 'Content-Type': 'application/json', 'X-Auth-Token': window.authToken },
    body: method === 'POST' ? JSON.stringify(endpoint.params) : undefined });
  if (!response.ok) throw new Error('widget_http_' + response.status);
  return response.json();
}
const app = createApp({
  setup() {
    const messages = ref([]), input = ref(''), ready = ref(false);
    onMounted(async () => {
      messages.value = (await call(endpoints.getConversation({}))).payload;
      ready.value = true;
    });
    async function send(event) {
      event.preventDefault();
      messages.value = (await call(endpoints.createConversation({ message: input.value }), 'POST')).messages;
      input.value = '';
    }
    return () => h('section', { 'data-ready': String(ready.value),
      style: { color: getContrastingTextColor('#ffffff') } }, [
      h('h1', i18n.global.t('heading')),
      h('div', { id: 'history' }, messages.value.map(message => h('p', message.content))),
      h('form', { onSubmit: send }, [h('label', [i18n.global.t('message'),
        h('textarea', { value: input.value, required: true, onInput: event => { input.value = event.target.value; } })]),
        h('button', { type: 'submit' }, i18n.global.t('send'))]),
    ]);
  },
});
app.use(i18n);
window.onload = () => {
  window.WOOT_WIDGET = app.mount('#app');
};
