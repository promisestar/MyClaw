import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      name: 'chat',
      component: () => import('../views/ChatView.vue'),
    },
    {
      path: '/sessions',
      name: 'sessions',
      component: () => import('../views/SessionsView.vue'),
    },
    {
      path: '/skills',
      name: 'skills',
      component: () => import('../views/SkillsView.vue'),
    },
    {
      path: '/skills/:name/edit',
      name: 'skill-editor',
      component: () => import('../views/SkillEditor.vue'),
    },
    {
      path: '/knowledge-base',
      name: 'knowledge-base',
      component: () => import('../views/KnowledgeBaseView.vue'),
    },
    {
      path: '/tool-logs',
      name: 'tool-logs',
      component: () => import('../views/ToolLogsView.vue'),
    },
    {
      path: '/usage',
      name: 'usage',
      component: () => import('../views/UsageView.vue'),
    },
    {
      path: '/automation',
      name: 'automation',
      component: () => import('../views/AutomationView.vue'),
    },
    {
      path: '/memory',
      name: 'memory',
      component: () => import('../views/MemoryView.vue'),
    },
    {
      path: '/config',
      name: 'config',
      component: () => import('../views/ConfigView.vue'),
    },
    {
      path: '/health',
      name: 'health',
      component: () => import('../views/HealthView.vue'),
    },
  ],
})

export default router
