"""展示已经完成的离线策略比较。请求不会触发回测或行情请求。"""

import html


LABELS = {
    "baseline": "原四信号规则对照",
    "setups": "趋势突破＋止跌确认",
    "setups_sector": "加入行业与个股相对强弱排序",
    "setups_sector_exit": "加入结构失效与时间退出",
}


def render(result):
    def pct(value):
        return "—" if value is None else f"{value * 100:.2f}%"

    esc = lambda value: html.escape(str(value))
    coverage = result["coverage"]
    rows = []
    for name, arm in result["arms"].items():
        sample = arm["out_of_sample"]
        book = arm["out_of_sample_portfolio"]
        decision = arm.get("promotion")
        state = "对照" if decision is None else "通过" if decision["approved"] else "证据不足，未启用"
        rows.append(
            f"<tr><td>{esc(LABELS[name])}</td><td>{sample['selected']}</td><td>{sample['closed']}</td>"
            f"<td>{sample['signal_dates_with_closed_orders']}</td><td>{pct(sample['mean_net_return'])}</td>"
            f"<td>{pct(book['net_return'])}</td><td>{pct(book['max_drawdown'])}</td><td>{state}</td></tr>"
        )
    reasons = []
    for name, arm in result["arms"].items():
        decision = arm.get("promotion")
        if decision:
            reasons.append(f"<p>{esc(LABELS[name])} <code>{esc(', '.join(decision['reasons']))}</code></p>")
    status = "已通过验收" if result["approved_strategies"] else "本轮未批准新规则进入正式选股"
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>短期策略回测比较</title>
<style>body{{max-width:1280px;margin:24px auto;padding:0 18px;background:#0f1115;color:#e8ecf3;font:15px/1.7 system-ui}}
a{{color:#76baff}}table{{border-collapse:collapse;width:100%;white-space:nowrap}}td,th{{padding:10px;border-bottom:1px solid #363d48;text-align:right}}td:first-child,th:first-child{{text-align:left}}.wrap{{overflow:auto}}code{{overflow-wrap:anywhere;white-space:normal}}p{{max-width:1000px}}</style>
<nav><a href="/">信号</a> · <a href="/summary">当日总结</a> · <a href="/tiers">梯队回测</a></nav>
<h1>短期策略回测比较</h1><p><strong>{status}</strong></p>
<p>数据截至 {esc(result['asof'])}。回放信号区间 {esc(coverage['start'])} 至 {esc(coverage['end'])}；
样本外区间 {esc(coverage['test_start'])} 至 {esc(coverage['test_end'])}，共 {coverage['test_sessions']} 个市场信号日。</p>
<p>原四信号对照由放量上攻、MACD/KDJ金叉、收破布林下轨的并集重建；既有AI榜单的真实发布结果另列于完整报告。四组使用相同手续费与滑点、下一交易日开盘入场、五个交易日主持有期。组合初始资金80万元，最多8只，每笔预算10万元；
已持有或现金不足时跳过，剩余资金留现金。表内单笔统计只包含可核验的完成买卖，未成交及缺数详见明细。</p>
<div class="wrap"><table><thead><tr><th>方案</th><th>入选次数</th><th>完成买卖</th><th>有完成样本的信号日</th>
<th>单笔平均净收益</th><th>样本外资金收益</th><th>组合最大回撤</th><th>验收</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p>当前行业标签与股票状态缺少历史快照；更早价格包含复权历史，可靠成交快照区间较短。
金额与回撤来自本轮受限数据回放，缺失成交、估值与价格基础限制均保留在报告。历史信号是规则重建，未写成当时发布的AI名单。</p>
<p>预先固定的上线门槛包括至少60个有完成买卖的样本外信号日、100笔成交、90%可核验覆盖；
按连续五个市场日分块重采样并校正三组比较，净收益改善区间下界须为正，同时通过组合回撤与双倍滑点测试。</p>
<p><a href="/api/strategy-review">完整报告 JSON</a> · <a href="/api/strategy-review/orders">逐笔信号与成交依据 JSON</a></p>
<details><summary>各方案未通过的条件</summary>{''.join(reasons)}</details>
<p>回测版本 {esc(result['version'])}，生成时间 {esc(result['generated_at'])}。</p></html>"""
