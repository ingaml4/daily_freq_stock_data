import { createRouter, createWebHistory } from "vue-router";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/signals/vol_spike" },
    { path: "/index.html", redirect: "/signals/vol_spike" },
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
    { path: "/:pathMatch(.*)*", redirect: "/signals/vol_spike" },
  ],
  scrollBehavior: () => ({ top: 0 }),
});

router.afterEach((to) => {
  document.title = `${String(to.meta.title || "行情工作台")} · 行情工作台`;
});

export default router;
