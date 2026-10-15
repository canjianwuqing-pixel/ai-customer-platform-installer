"use strict";
// No tokens, document content or credentials enter storage, URLs or logs.
const form = document.getElementById("knowledge-form");
let dirty = form && form.dataset.dirty === "true";
const picker = document.getElementById("service-picker");
if (picker) {
  const loaded = picker.value;
  picker.addEventListener("change", () => {
    if (!picker.value || (dirty && !window.confirm("当前正文尚未保存，切换实例将放弃这些修改。是否继续？"))) {
      picker.value = loaded;
      return;
    }
    dirty = false;
    picker.form.requestSubmit();
  });
}
if (form) {
  form.querySelector("textarea").addEventListener("input", () => { dirty = true; });
  const confirmClear = document.getElementById("confirm-clear");
  confirmClear.addEventListener("change", () => {
    document.getElementById("clear-knowledge").disabled = !confirmClear.checked;
  });
  form.addEventListener("submit", () => { dirty = false; });
  window.addEventListener("beforeunload", (event) => {
    if (dirty) { event.preventDefault(); event.returnValue = ""; }
  });
}
