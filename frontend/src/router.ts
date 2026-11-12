import { createRouter, createWebHistory, type LocationQuery } from "vue-router";

function signalEntry(to: { query: LocationQuery }) {
  const query = { ...to.query };
  const view = typeof query.view === "string" ? query.view : "vol_spike";
  const valid = [
    "vol_spike",
    "macd_golden",
    "kdj_golden",
    "boll_break_lower",
    "reversal",
    "box_range",
  ];
  delete query.view;
  return {
    path: `/signals/${valid.includes(view) ? view : "vol_spike"}`,
    query,
  };
}

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: signalEntry },
    { path: "/index.html", redirect: signalEntry },
    {
      path: "/login",
      component: () => import("./views/LoginView.vue"),
      meta: { standalone: true, title: "登录" },
    },
    {
      path: "/signals/:view?",
      component: () => import("./views/SignalsView.vue"),
      meta: { title: "信号发现" },
    },
    {
      path: "/summary",
      component: () => import("./views/SummaryView.vue"),
      meta: { title: "收盘复盘" },
    },
    {
      path: "/tiers",
      component: () => import("./views/TiersView.vue"),
      meta: { title: "梯队跟踪" },
    },
    {
      path: "/strategy-review",
      component: () => import("./views/StrategyReviewView.vue"),
      meta: { title: "策略验证" },
    },
    {
      path: "/strategy-review/orders",
      component: () => import("./views/StrategyOrdersView.vue"),
      meta: { title: "逐笔回放" },
    },
    { path: "/:pathMatch(.*)*", redirect: "/signals/vol_spike" },
  ],
  scrollBehavior: () => ({ top: 0 }),
});

router.afterEach((to) => {
  document.title = `${String(to.meta.title || "行情工作台")} · 行情工作台`;
});

export default router;
