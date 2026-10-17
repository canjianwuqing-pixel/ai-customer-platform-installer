"use strict";
const modelForm = document.getElementById("config-form");
const feedback = document.getElementById("model-feedback");
const modelOptions = document.getElementById("model-options");
modelOptions.addEventListener("change", () => {
  if (modelOptions.value) modelForm.elements.model.value = modelOptions.value;
});
async function modelAction(action, button) {
  button.disabled = true;
  feedback.textContent = "正在连接，请稍候……";
  try {
    const response = await fetch("/admin/models/" + action, {
      method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({csrf: modelForm.elements.csrf.value, profile_id: modelForm.dataset.modelProfile,
        model: modelForm.elements.model.value, api_base_url: modelForm.elements.api_base_url.value,
        api_key_env: modelForm.elements.api_key_env.value, api_key: modelForm.elements.api_key.value,
        supports_vision: modelForm.elements.supports_vision.checked})
    });
    const result = await response.json();
    if (!response.ok) {
      feedback.textContent = result.message || "连接未完成，请检查地址、凭据及所选模型；仍可手动输入模型 ID。";
      return;
    }
    if (action === "discover") {
      modelOptions.replaceChildren(new Option("请选择模型，或在上方手动输入", ""));
      for (const id of result.models) modelOptions.append(new Option(id, id));
      feedback.textContent = result.models.length ? "模型列表已加载；也可手动输入任意受支持的模型 ID。" : "接口未返回模型，可继续手动输入。";
    } else feedback.textContent = modelForm.elements.supports_vision.checked ? "图片连接测试成功，请求使用了当前模型和内置测试图；尚未保存的配置仍需保存。" : "模型连接成功（文字测试），实际请求使用了当前填写的模型。尚未保存的配置仍需点击保存。";
  } catch (_) {
    feedback.textContent = "连接失败或登录已失效，请检查网络后重试；可继续手动配置模型。";
  } finally { button.disabled = false; }
}
document.getElementById("discover-models").addEventListener("click", event => modelAction("discover", event.currentTarget));
document.getElementById("test-model").addEventListener("click", event => modelAction("test", event.currentTarget));
