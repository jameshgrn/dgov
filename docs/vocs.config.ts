import { defineConfig } from 'vocs'

export default defineConfig({
  title: 'dgov',
  basePath: '/dgov',
  sidebar: [
    {
      text: 'Getting Started',
      link: '/getting-started',
    },
    {
      text: 'Concepts',
      link: '/concepts',
    },
    {
      text: 'LLM Providers',
      link: '/llm-providers',
    },
    {
      text: 'CLI Reference',
      link: '/cli-reference',
    },
    {
      text: 'Plan Reference',
      link: '/plan-reference',
    },
  ],
})
