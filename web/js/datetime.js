import { $ } from "./dom.js";

// 表单里日期和时间是两个输入框，配置里是单个 ISO 字符串。

export function collectStartTime() {
  const date = $("startDateInput").value;
  const time = $("startTimeInput").value;
  if (!date && !time) return "";
  if (!date || !time) throw new Error("请选择完整的开抢日期和时间");
  return `${date}T${time.length === 5 ? `${time}:00` : time}`;
}

export function applyStartTime(value) {
  if (!value) {
    $("startDateInput").value = "";
    $("startTimeInput").value = "";
    return;
  }
  const [date = "", time = ""] = value.split("T");
  $("startDateInput").value = date;
  $("startTimeInput").value = time.slice(0, 8);
}

export function collectDateTime(dateId, timeId, label, required = false) {
  const date = $(dateId).value;
  const time = $(timeId).value;
  if (!date && !time) {
    if (required) throw new Error(`请选择${label}`);
    return "";
  }
  if (!date || !time) throw new Error(`请选择完整的${label}`);
  return `${date}T${time.length === 5 ? `${time}:00` : time}`;
}

export function applyDateTime(value, dateId, timeId) {
  if (!value) {
    $(dateId).value = "";
    $(timeId).value = "";
    return;
  }
  const [date = "", time = ""] = value.split("T");
  $(dateId).value = date;
  $(timeId).value = time.slice(0, 8);
}
