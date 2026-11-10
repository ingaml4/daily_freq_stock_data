export function formatNumber(value: unknown, digits = 2): string {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number)
    ? number.toLocaleString("zh-CN", {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      })
    : "—";
}

export function formatPercent(
  value: unknown,
  digits = 2,
  decimal = false,
): string {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number)
    ? `${(decimal ? number * 100 : number).toFixed(digits)}%`
    : "—";
}

export function changeClass(value: unknown): string {
  const number = Number(value);
  return Number.isFinite(number) && number !== 0
    ? number > 0
      ? "up"
      : "down"
    : "";
}
