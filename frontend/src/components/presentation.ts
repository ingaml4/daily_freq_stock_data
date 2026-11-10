import { h } from "vue";

export function numeric(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function number(value: unknown, digits = 2): string {
  const parsed = numeric(value);
  return parsed === null
    ? "—"
    : parsed.toLocaleString("zh-CN", {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      });
}

export function percent(value: unknown, fractional = true, digits = 2): string {
  const parsed = numeric(value);
  return parsed === null
    ? "—"
    : `${(parsed * (fractional ? 100 : 1)).toFixed(digits)}%`;
}

export function change(value: unknown, fractional = true, digits = 2) {
  const parsed = numeric(value);
  return h(
    "span",
    {
      class: [
        "tabular",
        parsed === null ? "" : parsed > 0 ? "up" : parsed < 0 ? "down" : "",
      ],
    },
    percent(value, fractional, digits),
  );
}

export function localTime(value: string | null | undefined): string {
  if (!value) return "尚未记录";
  if (!/[TZ+]/.test(value)) return value;
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return value;
  return parsed.toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour12: false,
  });
}

export function message(error: unknown): string {
  return error instanceof Error ? error.message : "数据加载失败，请稍后重试。";
}

export function stockLink(row: {
  secid: string;
  code?: string;
  name?: string;
  url?: string;
}) {
  const code = row.code || row.secid.split(".").at(-1) || row.secid;
  const prefix = row.secid.startsWith("1.")
    ? "SH"
    : row.secid.startsWith("2.")
      ? "BJ"
      : "SZ";
  return h(
    "a",
    {
      href: row.url || `https://xueqiu.com/S/${prefix}${code}`,
      target: "_blank",
      rel: "noopener noreferrer",
      class: "stock-link",
    },
    [
      h("strong", row.name || code),
      h("span", { class: "stock-code tabular" }, code),
    ],
  );
}
